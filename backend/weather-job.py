#!/usr/bin/env python3
"""weather.py — henter DMI-metObs-observationer og lægger dem i DB.

Samme form som job.py: ét vandmærke, én run-log, ét idempotent upsert.
Ingen aggregation her — rå timeværdier i weather, aggregater i et view.

DMI leverer ÉN parameter pr. kald, i long format. Denne fil henter de
parametre, der svarer til weather-tabellens kolonner, og samler dem til
én bred række pr. (station, tidspunkt). Det er den pivotering, der før
blev gjort i pandas — nu sker den i en dict, fordi tabellen ER den brede form.

Kør:
    python3 weather.py                # hent efterslæb + nu
    python3 weather.py --days 7       # hvor mange dage tilbage der maks. hentes
    python3 weather.py --dry-run      # hent og rapportér, skriv ikke til DB
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
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

DMI_URL = "https://opendataapi.dmi.dk/v2/metObs/collections/observation/items"

JOB = "weather_dmi"
KIND = "observation"
SOURCE_ID = "dmi"

# Hvor mange dage tilbage vi *maksimalt* henter, første gang vi kører mod tom DB.
MAX_BACKFILL_DAYS = int(os.environ.get("WEATHER_MAX_BACKFILL_DAYS", "30"))

# Stationer: DMI stationId -> vores location_id.
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

# DMI parameterId -> kolonne i weather. Hver parameter ER en kolonne, ikke en række.
# Verificér navnene mod DMI's parameter-liste før første kørsel.
PARAM_TO_COLUMN = {
    "temp_mean_past1h": "temperature_c", # Latest hour's mean air temperature measured 2 m over terrain, 1h frequency
    "wind_speed":       "wind_ms",       # Latest 10 minutes' mean wind speed measured 10 m over terrain, 10min frequency
    "wind_dir":         "wind_dir_deg",  # Latest 10 minutes' mean wind direction measured 10 m over terrain. 0 means calm (see Codes), 10min frequency
    "humidity":         "humidity_pct",  # Present relative humidity measured 2 m over terrain, 10min frequency
    "precip_past1h":    "precip_mm",     # Accumulated precipitation in the latest hour or the code -0,1, which means "traces of precipitation, less than 0.1 kg/m²". kg/m² is equivalent to mm. (see Codes), hourly frequency
    "cloud_cover":      "cloud_pct",     # The fraction of the sky covered by cloud of any type or height above the ground (see also Codes), 
    "radia_glob":       "radiation_wm2", # Latest 10 minutes global radiation mean intensity, 10 min frequency
}

MAX_WORKERS = 3          # DMI's rate limit er stram: 6 stationer x 7 parametre = 42 kald
HTTP_ATTEMPTS = 5
HTTP_TIMEOUT = 60


# ============================================================ små hjælpefunktioner

def log(msg: str) -> None:
    print(f"{datetime.now(CPH):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def to_utc(s: str) -> datetime:
    """ISO-streng fra DMI -> aware datetime i UTC.

    DMI leverer '2026-10-15T15:00:00Z'. Python 3.10's fromisoformat kan
    IKKE læse 'Z'-suffikset — det kom først i 3.11.
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


# ======================================================================= DB-laget

def get_watermark(conn) -> datetime | None:
    """Seneste ts_dk vi med sikkerhed har indlæst. None = aldrig kørt."""
    with conn.cursor() as cur:
        cur.execute("SELECT watermark_dk FROM sync_state WHERE job = %s", (JOB,))
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def set_watermark(conn, watermark: datetime) -> None:
    """Flytter vandmærket frem — aldrig tilbage."""
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


def start_run(conn, window_from: datetime, window_to: datetime) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_runs (job, status, window_from, window_to)
            VALUES (%s, 'running', %s, %s)
            RETURNING run_id
            """,
            (JOB, window_from, window_to),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def finish_run(conn, run_id, status, rows_fetched=0, rows_written=0,
               error=None, http_status=None, http_message=None,
               error_url=None, error_body=None, attempts=None, elapsed_ms=None):
    """Lukker logposten. Kaldes både ved succes og ved fejl."""
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
           fetched_at    = now()
      WHERE weather.temperature_c IS DISTINCT FROM EXCLUDED.temperature_c
         OR weather.wind_ms       IS DISTINCT FROM EXCLUDED.wind_ms
         OR weather.wind_dir_deg  IS DISTINCT FROM EXCLUDED.wind_dir_deg
         OR weather.humidity_pct  IS DISTINCT FROM EXCLUDED.humidity_pct
         OR weather.precip_mm     IS DISTINCT FROM EXCLUDED.precip_mm
         OR weather.cloud_pct     IS DISTINCT FROM EXCLUDED.cloud_pct
    RETURNING 1
"""


def upsert(conn, rows) -> int:
    """Indsætter eller opdaterer. Returnerer antal ramte rækker.

    BEMÆRK: RETURNING 1 fjernes før executemany. psycopg3 returnerer ikke
    resultatsættet fra RETURNING under executemany, så len(cur.fetchall())
    gav altid 0 — tællingen løj, mens skrivningen virkede fint.
    cur.rowcount virker derimod med executemany.
    """
    if not rows:
        return 0
    sql = UPSERT_SQL.replace("RETURNING 1", "")
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
        changed = cur.rowcount
    conn.commit()
    return changed


# ==================================================================== HTTP-laget

def get_json(url: str) -> dict:
    """GET med genforsøg. 429 og 5xx prøves igen med stigende pause."""
    start = time.monotonic()
    last: Exception | None = None
    for n in range(HTTP_ATTEMPTS):
        try:
            with urlopen(url, timeout=HTTP_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            last = e
            if e.code == 429 and n < HTTP_ATTEMPTS - 1:
                log(f"    429 — venter {2 ** n}s")
                time.sleep(2 ** n)
                continue
            if e.code in (500, 502, 503) and n < HTTP_ATTEMPTS - 1:
                time.sleep(2 ** n)
                continue
            raise
        except URLError as e:
            last = e
            if n < HTTP_ATTEMPTS - 1:
                time.sleep(2 ** n)
                continue
            raise
    raise RuntimeError(f"gav op efter {HTTP_ATTEMPTS} forsøg: {last!r}")


def fetch_one(api_key: str, station_id: str, parameter_id: str,
              datetime_str: str) -> list[tuple[datetime, float]]:
    """Henter ÉN parameter for ÉN station. Returnerer [(ts_utc, value)]."""
    params = {
        "api-key": api_key,
        "datetime": datetime_str,
        "stationId": station_id,
        "parameterId": parameter_id,
        "limit": "300000",
    }
    url = f"{DMI_URL}?{urlencode(params)}"
    js = get_json(url)

    out: list[tuple[datetime, float]] = []
    for feat in js.get("features", []):
        p = feat.get("properties", {})
        ts, val = p.get("observed"), p.get("value")
        if ts is None or val is None:
            continue
        try:
            out.append((to_utc(ts), float(val)))
        except (TypeError, ValueError):
            continue
    return out


def fetch_station(api_key: str, station_id: str,
                  datetime_str: str) -> dict[datetime, dict[str, float]]:
    """Henter alle parametre for én station og samler dem pr. tidspunkt.

    Returnerer {ts_utc: {kolonnenavn: værdi}}. Dette er erstatningen for
    pivot_wide + ingest_timeseries_basic: tabellen er allerede den brede form.
    """
    by_time: dict[datetime, dict[str, float]] = defaultdict(dict)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {
            ex.submit(fetch_one, api_key, station_id, param_id, datetime_str): (param_id, col)
            for param_id, col in PARAM_TO_COLUMN.items()
        }
        for fut in as_completed(futures):
            param_id, col = futures[fut]
            try:
                for ts, val in fut.result():
                    by_time[ts][col] = val
            except Exception as exc:                       # noqa: BLE001
                log(f"  {station_id} {param_id}: fejlede — {exc!r}")

    return by_time


def ensure_source_locations(conn, stations) -> None:
    """Sikrer at mappingen DMI-station <-> location_id findes i DB.

    Skal køre FØR weather skrives, fordi weather.location_id har FK til
    weather_locations. Gør mappingen til en kendsgerning i databasen frem
    for en Python-dict, der kun findes så længe scriptet kører.
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


# ======================================================================== main

def parse_args():
    ap = argparse.ArgumentParser(description="Hent DMI-vejrdata til DB")
    ap.add_argument("--dry-run", action="store_true",
                    help="hent og rapportér, men skriv ikke til DB")
    ap.add_argument("--days", type=int, default=MAX_BACKFILL_DAYS,
                    help=f"maks. antal dage tilbage (default {MAX_BACKFILL_DAYS})")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    key = api_key()

    with psycopg.connect(DSN) as conn:
        watermark = get_watermark(conn)
        if watermark is None:
            watermark = datetime.now(timezone.utc) - timedelta(days=args.days)

        # Overlap bagud: fanger DMI's rettelser og efterregistreringer.
        # Harmløst, fordi upsert er idempotent og vandmærket kun går frem.
        floor = max(
            watermark - timedelta(days=2),
            datetime.now(timezone.utc) - timedelta(days=args.days),
        )
        
        ceiling = datetime.now(timezone.utc) + timedelta(days=1)

        # DMI forventer ISO 8601 med offset — brug isoformat(), ikke Z-suffiks.
        dt_str = (f"{floor.astimezone(CPH).strftime('%Y-%m-%d')}T00:00:00Z/"
          f"{ceiling.astimezone(CPH).strftime('%Y-%m-%d')}T23:59:59Z")
        
        log(f"vindue: {floor:%Y-%m-%d %H:%M} .. {ceiling:%Y-%m-%d %H:%M} UTC")

        if not args.dry_run:
            ensure_source_locations(conn, STATIONS)

        run_id = None if args.dry_run else start_run(conn, floor, ceiling)
        rows: list[tuple] = []

        try:
            for station_id, location_id in STATIONS.items():
                by_time = fetch_station(key, station_id, dt_str)
                if not by_time:
                    log(f"  {station_id} ({location_id}): ingen data i vinduet")
                    continue

                for ts in sorted(by_time):
                    v = by_time[ts]
                    rows.append((
                        SOURCE_ID, location_id, KIND, ts,
                        ts,                                  # observation: issued_at = ts_dk
                        station_id,                          # proveniens: hvilken DMI-station
                        v.get("temperature_c"),
                        v.get("wind_ms"),
                        v.get("wind_dir_deg"),
                        v.get("humidity_pct"),
                        v.get("precip_mm"),
                        v.get("cloud_pct"),
                        v.get("radiation_wm2"),
                    ))
                log(f"  {station_id} ({location_id}): {len(by_time)} tidspunkter")

            log(f"hentet {len(rows)} rækker fra {len(STATIONS)} station(er)")

            if args.dry_run:
                log("DRY RUN: intet skrevet til DB")
                return 0

            written = upsert(conn, rows)
            if rows:
                set_watermark(conn, max(r[3] for r in rows))
            finish_run(conn, run_id, "ok", len(rows), written)
            log(f"skrev {written} ændrede rækker (af {len(rows)})")
            return 0
        except Exception as exc:
            log(f"FEJL: {exc!r}")
            if run_id is not None:
                try:
                    finish_run(conn, run_id, "failed", error=repr(exc))
                except Exception as log_exc:                    # noqa: BLE001
                    log(f"KUNNE IKKE LOGGE FEJL: {log_exc!r} — oprindelig: {exc!r}")
            # Bevidst exit 0: DMI-nedetid er forventet drift, ikke en defekt
            # unit. Vandmærket står stille, og næste kørsel dækker hele
            # efterslæbet, fordi vinduet bygges fra vandmærket og ikke fra nu.
            # Med exit != 0 ville systemd melde "failed" hver gang DMI har et
            # hikke, og journalen blev ubrugelig — samme beslutning som i
            # push_prices.py for Netsite-nedetid.
            return 0

if __name__ == "__main__":
    main()
