#!/usr/bin/env python3
"""weather.py — henter DMI-vejrdata og lægger dem i DB.

Samme form som job.py: ét vandmærke, én run-log, ét upsert.
Ingen aggregation her — det hører i et view eller i API'et.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo

import psycopg
from dotenv import load_dotenv

load_dotenv("/home/peterfausboll/code/pricejob/backend/.env")

CPH = ZoneInfo("Europe/Copenhagen")

DSN = psycopg.conninfo.make_conninfo(
    host=os.environ["PGHOST"],
    port=os.environ.get("PGPORT", "41326"),
    dbname=os.environ.get("PGDATABASE", "Regenda"),
    user=os.environ["PGUSER"],
    password=os.environ["PGPASSWORD"],
)

JOB = "weather_dmi"
BASE = "https://opendataapi.dmi.dk/v2/"
STATIONS = ['06031','06065','06109','06141','06188','06197']
PAGE_DELAY = 2


def to_utc(s):
    dt = datetime.fromisoformat(s)
    return (dt if dt.tzinfo else dt.replace(tzinfo=CPH)).astimezone(timezone.utc)


def get_watermark(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT watermark_dk FROM sync_state WHERE job = %s", (JOB,))
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def set_watermark(conn, watermark):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_state (job, last_run_at, last_success_at,
                                    watermark_dk, rows_total)
            VALUES (%s, now(), now(), %s, 0)
            ON CONFLICT (job) DO UPDATE
               SET last_run_at     = now(),
                   last_success_at = now(),
                   watermark_dk    = GREATEST(sync_state.watermark_dk,
                                              EXCLUDED.watermark_dk),
                   rows_total      = sync_state.rows_total
            """,
            (JOB, watermark),
        )
    conn.commit()


def start_run(conn, window_from, window_to):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sync_runs (job, status, window_from, window_to) "
            "VALUES (%s, 'running', %s, %s) RETURNING run_id",
            (JOB, window_from, window_to),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def finish_run(conn, run_id, status, rows_fetched=0, rows_written=0,
               error=None, http_status=None, http_message=None,
               error_url=None, error_body=None, attempts=None, elapsed_ms=None):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE sync_runs
               SET finished_at  = now(),
                   status       = %s,
                   rows_fetched = %s,
                   rows_written = %s,
                   error        = %s,
                   http_status  = %s,
                   http_message = %s,
                   error_url    = %s,
                   error_body   = %s,
                   attempts     = %s,
                   elapsed_ms   = %s
             WHERE run_id = %s
            """,
            (status, rows_fetched, rows_written, error,
             http_status, http_message, error_url, error_body,
             attempts, elapsed_ms, run_id),
        )
    conn.commit()


def get_json(url, attempts=5):
    start = time.monotonic()
    for n in range(attempts):
        try:
            with urlopen(url, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 429 and n < attempts - 1:
                time.sleep(PAGE_DELAY)
                continue
            if e.code in (500, 502, 503) and n < attempts - 1:
                time.sleep(2 ** n)
                continue
            raise
        except URLError:
            if n < attempts - 1:
                time.sleep(2 ** n)
                continue
            raise


def fetch_station(station_id, start_iso, end_iso):
    params = {
        "stationId": station_id,
        "from": start_iso,
        "to": end_iso,
    }
    url = f"{BASE}?{urlencode(params)}"
    payload = get_json(url)
    return payload.get("features", payload.get("records", []))


UPSERT_SQL = """
    INSERT INTO wobservations (source_id, location_id, kind, ts_dk, issued_at,
                         temperature_c, wind_ms, wind_dir_deg,
                         humidity_pct, precip_mm, cloud_pct, radiation_wm2, fetched_at)
    VALUES ('dmi', %s, 'observation', %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (source_id, location_id, kind, ts_dk, issued_at) DO UPDATE
       SET temperature_c = EXCLUDED.temperature_c,
           wind_ms       = EXCLUDED.wind_ms,
           wind_dir_deg  = EXCLUDED.wind_dir_deg,
           humidity_pct  = EXCLUDED.humidity_pct,
           precip_mm     = EXCLUDED.precip_mm,
           cloud_pct     = EXCLUDED.cloud_pct,
           radiation_wm2 = EXCLUDED.radiation_wm2,
           fetched_at    = now()
      WHERE wobservations.temperature_c IS DISTINCT FROM EXCLUDED.temperature_c
         OR wobservations.wind_ms       IS DISTINCT FROM EXCLUDED.wind_ms
         OR wobservations.radiation_wm2 IS DISTINCT FROM EXCLUDED.radiation_wm2         
    RETURNING 1
"""


def upsert(conn, rows):
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(UPSERT_SQL, rows)
        changed = len(cur.fetchall()) if cur.description else 0
    conn.commit()
    return changed


def main():
    with psycopg.connect(DSN) as conn:
        watermark = get_watermark(conn)
        if watermark is None:
            watermark = datetime(2026, 1, 1, tzinfo=timezone.utc)

        window_from = watermark - timedelta(days=2)
        window_to = datetime.now(timezone.utc)

        run_id = start_run(conn, window_from, window_to)
        try:
            rows = []
            for station in STATIONS:
                records = fetch_station(
                    station,
                    window_from.astimezone(CPH).strftime("%Y-%m-%dT%H:%M"),
                    window_to.astimezone(CPH).strftime("%Y-%m-%dT%H:%M"),
                )
                for r in records:
                    obs = r.get("properties", r)
                    ts = to_utc(obs["observed"])
                    rows.append((
                        station, ts, ts,               # observation: issued_at = ts
                        obs.get("temperature"),
                        obs.get("windSpeed"),
                        obs.get("windDirection"),
                        obs.get("humidity"),
                        obs.get("precipitation"),
                        obs.get("cloudCover"),
                        obs.get("radiation"),
                    ))
                time.sleep(PAGE_DELAY)

            written = upsert(conn, rows)
            if rows:
                set_watermark(conn, max(r[1] for r in rows))
            finish_run(conn, run_id, "ok", len(rows), written)

        except Exception as exc:
            try:
                finish_run(conn, run_id, "failed", error=repr(exc))
            except Exception as log_exc:
                print(f"KUNNE IKKE LOGGE FEJL: {log_exc!r} — oprindelig: {exc!r}")
            raise


if __name__ == "__main__":
    main()
