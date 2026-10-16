#!/usr/bin/env python3
"""weather-backfill.py — henter DMI-observationer bagud, måned for måned.

Adskilt fra weather.py med vilje. Backfill'et har andre egenskaber end det
daglige job:

  - det kører i timer, ikke sekunder
  - det skal kunne genoptages efter en afbrydelse
  - det må IKKE røre sync_state (det daglige job ejer vandmærket)
  - det skriver fremskridt i backfill_progress, ikke i sync_runs

Kør:
    python3 weather-backfill.py --from-year 2016 --to-year 2026 --dry-run
    python3 weather-backfill.py --from-year 2016 --to-year 2026
    python3 weather-backfill.py --status        # hvad er gjort indtil nu
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from calendar import monthrange
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo

import psycopg
from dotenv import load_dotenv

load_dotenv("/home/peterfausboll/code/datajob/backend/.env")

CPH = ZoneInfo("Europe/Copenhagen")

DSN = psycopg.conninfo.make_conninfo(
    host=os.environ["PGHOST"],
    port=os.environ.get("PGPORT", "41326"),
    dbname=os.environ.get("PGDATABASE", "Regenda"),
    user=os.environ["PGUSER"],
    password=os.environ["PGPASSWORD"],
)

DMI_URL = "https://opendataapi.dmi.dk/v2/metObs/collections/observation/items"

JOB = "weather_dmi"
KIND = "observation"
SOURCE_ID = "dmi"

# Samme som weather.py — hold dem i sync.
# location_id ER DMI's stationstal. Det er en identifikator, ikke et tal:
# den ledende nul betyder, at den skal gemmes som text, ikke integer.
STATIONS = {
    "06031": "06031",
    "06065": "06065",
    "06109": "06109",
    "06141": "06141",
    "06188": "06188",
    "06197": "06197",
}

# Menneskenavne til weather_locations.display_name
STATION_NAMES = {
    "06031": "Tylstrup",
    "06065": "Års Syd",
    "06109": "Askov",
    "06141": "Abed",
    "06188": "Sjælsmark",
    "06197": "Nexø Vest",
}

PARAM_TO_COLUMN = {
    "temp_mean_past1h": "temperature_c",
    "wind_speed":       "wind_ms",
    "wind_dir":         "wind_dir_deg",
    "humidity":         "humidity_pct",
    "precip_past1h":    "precip_mm",
    "cloud_cover":      "cloud_pct",
    "radia_glob":       "radiation_wm2",
}

# Backfill'et skal være langsomt og forudsigeligt. Rate limit er den
# begrænsende faktor, ikke båndbredde.
MAX_WORKERS = 2
CALL_DELAY = 0.6
HTTP_ATTEMPTS = 6
HTTP_TIMEOUT = 90

# Skriv i bidder — 3 mio. rækker i én transaktion er ikke en god idé.
WRITE_CHUNK = 10_000


# ============================================================ hjælpefunktioner

def log(msg: str) -> None:
    print(f"{datetime.now(CPH):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def to_utc(s: str) -> datetime:
    """ISO fra DMI -> aware datetime i UTC.

    DMI leverer '2026-10-15T13:40:00Z'. Python 3.10's fromisoformat kan
    ikke læse 'Z', så vi oversætter det til '+00:00' først.
    """
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return (dt if dt.tzinfo else dt.replace(tzinfo=CPH)).astimezone(timezone.utc)


def api_key() -> str:
    key = os.environ.get("DMI_API_KEY")
    if not key:
        raise RuntimeError("DMI_API_KEY mangler (skal stå i .env)")
    return key


def month_periods(from_year: int, to_year: int, to_month: int):
    """Yield (first_day, last_day) for hver måned i spændet."""
    for year in range(from_year, to_year + 1):
        last_m = to_month if year == to_year else 12
        for month in range(1, last_m + 1):
            first = date(year, month, 1)
            last = date(year, month, monthrange(year, month)[1])
            yield first, last


# =============================================================== fremskridt

def progress_done(conn) -> set[tuple]:
    """Hvilke (station, parameter, periode) er allerede hentet?"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT station_id, parameter_id, period_from, period_to "
            "  FROM backfill_progress WHERE source_id = %s",
            (SOURCE_ID,),
        )
        return {(r[0], r[1], r[2], r[3]) for r in cur.fetchall()}


def mark_done(conn, station_id, parameter_id, p_from, p_to, n_rows) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO backfill_progress
                   (source_id, station_id, parameter_id,
                    period_from, period_to, rows_written)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_id, station_id, parameter_id,
                         period_from, period_to)
            DO UPDATE SET done_at = now(), rows_written = EXCLUDED.rows_written
            """,
            (SOURCE_ID, station_id, parameter_id, p_from, p_to, n_rows),
        )
    conn.commit()


# ==================================================================== HTTP

def get_json(url: str) -> dict:
    """GET med genforsøg. 429 og 5xx prøves igen med ægte backoff."""
    start = time.monotonic()
    last: Exception | None = None
    for n in range(HTTP_ATTEMPTS):
        try:
            with urlopen(url, timeout=HTTP_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            last = e
            # 429: DMI's rate limit. Vent længere for hvert forsøg.
            if e.code == 429 and n < HTTP_ATTEMPTS - 1:
                wait = min(60, 5 * (2 ** n))
                log(f"      429 — venter {wait}s")
                time.sleep(wait)
                continue
            if e.code in (500, 502, 503, 504) and n < HTTP_ATTEMPTS - 1:
                time.sleep(2 ** n)
                continue
            raise
        except URLError as e:
            last = e
            if n < HTTP_ATTEMPTS - 1:
                time.sleep(2 ** n)
                continue
            raise
        except (TimeoutError, OSError) as e:
            last = e
            if n < HTTP_ATTEMPTS - 1:
                time.sleep(2 ** n)
                continue
            raise
    raise RuntimeError(f"gav op efter {HTTP_ATTEMPTS} forsøg: {last!r}")


def fetch_period(api_key: str, station_id: str, parameter_id: str,
                 p_from: date, p_to: date) -> list[tuple[datetime, float]]:
    """Henter ÉN parameter for ÉN station for ÉN måned."""
    dt_str = (f"{p_from.isoformat()}T00:00:00Z/"
              f"{p_to.isoformat()}T23:59:59Z")
    params = {
        "api-key": api_key,
        "datetime": dt_str,
        "stationId": station_id,
        "parameterId": parameter_id,
        "limit": "300000",
    }
    js = get_json(f"{DMI_URL}?{urlencode(params)}")

    out: list[tuple[datetime, float]] = []
    for feat in js.get("features", []):
        p = feat.get("properties", {})
        ts, val = p.get("observed"), p.get("value")
        if ts is None or val is None:
            continue
        # Ingen try/except her. En parsingsfejl skal ses, ikke sluges.
        out.append((to_utc(ts), float(val)))
    return out


# ======================================================================= DB

UPSERT_SQL = """
    INSERT INTO weather (source_id, location_id, kind, ts_dk, issued_at,
                         source_station_id,
                         temperature_c, wind_ms, wind_dir_deg,
                         humidity_pct, precip_mm, cloud_pct, radiation_wm2)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (source_id, location_id, kind, ts_dk, issued_at) DO UPDATE
       SET temperature_c = EXCLUDED.temperature_c,
           wind_ms       = EXCLUDED.wind_ms,
           wind_dir_deg  = EXCLUDED.wind_dir_deg,
           humidity_pct  = EXCLUDED.humidity_pct,
           precip_mm     = EXCLUDED.precip_mm,
           cloud_pct     = EXCLUDED.cloud_pct,
           radiation_wm2 = EXCLUDED.radiation_wm2,
           fetched_at    = now()
      WHERE weather.temperature_c IS DISTINCT FROM EXCLUDED.temperature_c
         OR weather.wind_ms       IS DISTINCT FROM EXCLUDED.wind_ms
         OR weather.wind_dir_deg  IS DISTINCT FROM EXCLUDED.wind_dir_deg
         OR weather.humidity_pct  IS DISTINCT FROM EXCLUDED.humidity_pct
         OR weather.precip_mm     IS DISTINCT FROM EXCLUDED.precip_mm
         OR weather.cloud_pct     IS DISTINCT FROM EXCLUDED.cloud_pct
         OR weather.radiation_wm2 IS DISTINCT FROM EXCLUDED.radiation_wm2
    RETURNING 1
"""


def upsert_chunked(conn, rows, chunk=WRITE_CHUNK) -> int:
    """Skriver i bidder med egen commit pr. bid. Idempotent.

    BEMÆRK: RETURNING 1 fjernes før executemany. psycopg3 returnerer ikke
    resultatsættet fra RETURNING under executemany, så len(cur.fetchall())
    gav altid 0 — tællingen løj, mens skrivningen virkede fint (bekræftet:
    3,37 mio. rækker blev skrevet med 0 rapporteret). cur.rowcount virker.
    """
    if not rows:
        return 0
    sql = UPSERT_SQL.replace("RETURNING 1", "")
    written = 0
    for i in range(0, len(rows), chunk):
        part = rows[i:i + chunk]
        with conn.cursor() as cur:
            cur.executemany(sql, part)
            written += cur.rowcount
        conn.commit()
    return written


def start_run(conn, from_year: int, to_year: int) -> int:
    w_from = datetime(from_year, 1, 1, tzinfo=timezone.utc)
    w_to = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_runs (job, status, window_from, window_to)
            VALUES (%s, 'running', %s, %s) RETURNING run_id
            """,
            (JOB, w_from, w_to),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def finish_run(conn, run_id, status, rows_fetched=0, rows_written=0, error=None):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE sync_runs
               SET finished_at = now(), status = %s,
                   rows_fetched = %s, rows_written = %s, error = %s
             WHERE run_id = %s
            """,
            (status, rows_fetched, rows_written, error, run_id),
        )
    conn.commit()


def set_watermark(conn, watermark: datetime) -> None:
    """Flytter vandmærket frem — aldrig tilbage.

    Kaldes KUN til allersidst i et fuldført backfill. Undervejs må
    sync_state ikke røres: det daglige job ejer vandmærket.
    """
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


def ensure_source_locations(conn, stations) -> None:
    """Sikrer at mappingen DMI-station <-> location_id findes i DB.

    Kaldes ved start, så mappingen er en kendsgerning i databasen og ikke
    bare en Python-dict, der forsvinder når scriptet afsluttes. Skal køre
    FØR weather skrives, fordi weather.location_id har FK til weather_locations.
    """
    with conn.cursor() as cur:
        for station_id, location_id in stations.items():
            cur.execute(
                """
                INSERT INTO weather_locations
                       (location_id, display_name, country, tz)
                VALUES (%s, %s, 'DK', 'Europe/Copenhagen')
                ON CONFLICT (location_id) DO NOTHING
                """,
                (location_id, STATION_NAMES.get(station_id, station_id)),
            )
            cur.execute(
                """
                INSERT INTO weather_source_locations
                       (source_id, location_id, external_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (source_id, location_id)
                DO UPDATE SET external_id = EXCLUDED.external_id
                """,
                (SOURCE_ID, location_id, station_id),
            )
    conn.commit()


# ======================================================================= main

def parse_args():
    ap = argparse.ArgumentParser(description="Backfill DMI-observationer")
    ap.add_argument("--from-year", type=int, default=2016)
    ap.add_argument("--to-year", type=int, default=datetime.now().year)
    ap.add_argument("--to-month", type=int, default=None,
                    help="sidste måned i slutåret (default: forrige måned)")
    ap.add_argument("--stations", default=None,
                    help="kommasepareret liste af stationId (default: alle)")
    ap.add_argument("--dry-run", action="store_true",
                    help="hent og rapportér, skriv ikke til DB")
    ap.add_argument("--status", action="store_true",
                    help="vis hvad der er gjort indtil nu, og afbryd")
    return ap.parse_args()


def show_status(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT station_id, count(*) AS months,
                   min(period_from) AS first_month,
                   max(period_to)   AS last_month,
                   sum(rows_written) AS rows
              FROM backfill_progress
             WHERE source_id = %s
             GROUP BY station_id
             ORDER BY station_id
            """,
            (SOURCE_ID,),
        )
        rows = cur.fetchall()
    if not rows:
        log("intet backfill registreret endnu")
        return 0
    log(f"{'station':>8}  {'mdr':>4}  {'fra':>10}  {'til':>10}  {'rækker':>10}")
    for station_id, months, first_month, last_month, n in rows:
        log(f"{station_id:>8}  {months:>4}  {str(first_month):>10}  "
            f"{str(last_month):>10}  {n:>10}")
    return 0


def main() -> int:
    args = parse_args()
    key = api_key()

    if args.to_month is None:
        today = datetime.now(CPH).date()
        args.to_month = today.month - 1 if today.month > 1 else 12

    stations = STATIONS
    if args.stations:
        wanted = {s.strip() for s in args.stations.split(",")}
        stations = {k: v for k, v in STATIONS.items() if k in wanted}

    periods = list(month_periods(args.from_year, args.to_year, args.to_month))

    log(f"plan: {len(periods)} måneder × {len(stations)} stationer "
        f"× {len(PARAM_TO_COLUMN)} parametre "
        f"= {len(periods) * len(stations) * len(PARAM_TO_COLUMN)} kald")

    with psycopg.connect(DSN) as conn:
        if args.status:
            return show_status(conn)

        if not args.dry_run:
            ensure_source_locations(conn, stations)

        done = set() if args.dry_run else progress_done(conn)
        if done:
            log(f"springer {len(done)} allerede hentede bidder over")

        run_id = None if args.dry_run else start_run(conn, args.from_year, args.to_year)
        total_fetched = total_written = 0

        try:
            for station_id, location_id in stations.items():
                station_rows = 0
                for (p_from, p_to) in periods:
                    # Spring hele måneden over, hvis alle parametre er gjort.
                    pending = [pm for pm in PARAM_TO_COLUMN
                               if (station_id, pm, p_from, p_to) not in done]
                    if not pending:
                        continue

                    by_time: dict[datetime, dict[str, float]] = defaultdict(dict)

                    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                        futures = {
                            ex.submit(fetch_period, key, station_id,
                                      pm, p_from, p_to): pm
                            for pm in pending
                        }
                        for fut in as_completed(futures):
                            pm = futures[fut]
                            try:
                                rows = fut.result()
                            except Exception as exc:            # noqa: BLE001
                                # Én parameter fejler — resten af måneden fortsætter.
                                log(f"      {station_id}/{pm} {p_from}: "
                                    f"FEJLEDE — {exc!r}")
                                continue
                            for ts, val in rows:
                                by_time[ts][PARAM_TO_COLUMN[pm]] = val
                            total_fetched += len(rows)
                            time.sleep(CALL_DELAY)

                    if not by_time:
                        if not args.dry_run:
                            # Marker som gjort alligevel: DMI har ingen data her.
                            for pm in pending:
                                mark_done(conn, station_id, pm, p_from, p_to, 0)
                        continue

                    rows = [
                        (
                            SOURCE_ID, location_id, KIND, ts, ts,
                            station_id,          # proveniens: hvilken DMI-station
                            by_time[ts].get("temperature_c"),
                            by_time[ts].get("wind_ms"),
                            by_time[ts].get("wind_dir_deg"),
                            by_time[ts].get("humidity_pct"),
                            by_time[ts].get("precip_mm"),
                            by_time[ts].get("cloud_pct"),
                            by_time[ts].get("radiation_wm2"),
                        )
                        for ts in sorted(by_time)
                    ]

                    if args.dry_run:
                        station_rows += len(rows)
                        continue

                    written = upsert_chunked(conn, rows)
                    total_written += written
                    station_rows += len(rows)

                    for pm in pending:
                        mark_done(conn, station_id, pm, p_from, p_to, len(rows))

                    log(f"    {station_id} {p_from:%Y-%m}: "
                        f"{len(rows):>6} rækker, {written:>6} ændrede")

                log(f"  {station_id} ({location_id}): {station_rows} rækker i alt")

            if args.dry_run:
                log(f"DRY RUN: ville skrive {total_fetched} rækker — intet skrevet")
                return 0

            # Vandmærket røres først nu, når alt er fuldført.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT max(ts_dk) FROM weather WHERE source_id = %s",
                    (SOURCE_ID,),
                )
                wm = cur.fetchone()[0]
            if wm:
                set_watermark(conn, wm)
                log(f"vandmærke sat til {wm}")

            finish_run(conn, run_id, "ok", total_fetched, total_written)
            log(f"FÆRDIG: hentet {total_fetched}, skrevet {total_written}")
            return 0

        except Exception as exc:
            log(f"FEJL: {exc!r}")
            if run_id is not None:
                try:
                    finish_run(conn, run_id, "failed",
                               total_fetched, total_written, repr(exc))
                except Exception as log_exc:                    # noqa: BLE001
                    log(f"KUNNE IKKE LOGGE FEJL: {log_exc!r} — oprindelig: {exc!r}")
            raise


if __name__ == "__main__":
    sys.exit(main())
