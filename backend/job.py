#!/usr/bin/env python3
import os
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import urlopen
from datetime import timedelta
import time
import json
#from google_crc32c import exc
import psycopg
from zoneinfo import ZoneInfo
CPH = ZoneInfo("Europe/Copenhagen")
from urllib.error import HTTPError, URLError
from dotenv import load_dotenv
load_dotenv("/home/peterfausboll/code/pricejob/.env")


AREAS = ["DK1", "DK2", "DE", "SE3", "SE4", "NO2"]
DSN = psycopg.conninfo.make_conninfo(
    host=os.environ["PGHOST"],
    port=os.environ.get("PGPORT", "41326"),
    dbname=os.environ.get("PGDATABASE", "Regenda"),
    user=os.environ["PGUSER"],
    password=os.environ["PGPASSWORD"],
)
BASE = "https://api.energidataservice.dk/dataset/DayAheadPrices"
PAGE = 5000
JOB = "dayahead_prices"
PAGE_DELAY = 11

#Situation	                            Hvad sker	            Hvordan det løser sig
#Energinet nede, kortvarigt	            Kaldet fejler	        Næste cron-kørsel henter fra vandmærket og dækker hullet
#Energinet nede, i dage	                Flere kørsler fejler	Vandmærket står stille; når det kommer op, hentes hele hullet i bidder
#Din server nede	                    Jobbet kører ikke	    Vandmærket står stille; når den kommer op, hentes hele hullet
#Begge oppe, men data ikke offent endnu	Morgendagen mangler	    window_to = nu + 2 dage dækker det; vandmærket flyttes kun til det, der faktisk kom           


def _to_api_time(dt):
    return dt.astimezone(CPH).strftime("%Y-%m-%dT%H:%M")

def _parse_error(exc: HTTPError, url: str, attempts: int,
                 elapsed_ms: int) -> dict:
    """Udtrækker alt brugbart fra et HTTPError-svar."""
    raw = exc.read()
    body_text = raw.decode("utf-8", "replace")

    message, parsed = None, None
    try:
        parsed = json.loads(body_text)
        if isinstance(parsed, dict):
            message = parsed.get("message") or parsed.get("error")
    except json.JSONDecodeError:
        pass

    return {
        "status":   exc.code,
        "message":  (message or body_text[:500]).strip(),
        "url":      url,
        "attempts": attempts,
        "elapsed":  elapsed_ms,
        "body":     parsed if parsed is not None else {"raw": body_text[:4000]},
    }


def _to_utc(time_dk):
    dt = datetime.fromisoformat(time_dk)
    return (dt if dt.tzinfo else dt.replace(tzinfo=CPH)).astimezone(timezone.utc)

def get_watermark(conn) -> datetime | None:
    """Seneste TimeDK vi med sikkerhed har indlæst. None = aldrig kørt."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT watermark_dk FROM sync_state WHERE job = %s", (JOB,)
        )
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
                   watermark_dk    = GREATEST(
                                         sync_state.watermark_dk,
                                         EXCLUDED.watermark_dk
                                     ),
                   rows_total      = sync_state.rows_total
            """,
            (JOB, watermark),
        )
    conn.commit()


def start_run(conn, window_from: datetime, window_to: datetime) -> int:
    """Åbner en logpost og returnerer dens id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_runs (status, window_from, window_to)
            VALUES ('running', %s, %s)
            RETURNING run_id
            """,
            (window_from, window_to),
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
                   error        = %s
             WHERE run_id = %s
            """,
            (status, rows_fetched, rows_written, error, run_id),
        )
    conn.commit()

class ApiError(RuntimeError):
    def __init__(self, info: dict):
        self.info = info
        super().__init__(f"HTTP {info['status']}: {info['message']}")


def _get_json(url: str, attempts: int = 5) -> dict:
    start = time.monotonic()
    for n in range(attempts):
        try:
            with urlopen(url, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            info = _parse_error(e, url, n + 1,
                                int((time.monotonic() - start) * 1000))
            info["body"] = info.pop("body")
            if e.code == 429 and n < attempts - 1:
                print(f"    429 — venter {int(PAGE_DELAY)}s ({info['message']})")
                time.sleep(PAGE_DELAY)
                continue
            if e.code in (500, 502, 503) and n < attempts - 1:
                time.sleep(2 ** n)
                continue
            raise ApiError(info)
        except URLError as e:
            info = {"status": None, "message": str(e), "url": url,
                    "attempts": n + 1,
                    "elapsed": int((time.monotonic() - start) * 1000),
                    "body": {"raw": repr(e)}}
            if n < attempts - 1:
                time.sleep(2 ** n)
                continue
            raise ApiError(info)



def fetch_window(start_iso, end_iso, areas, page=PAGE):
    """Henter alle rækker i vinduet, side for side."""
    offset, seen = 0, []
    while True:
        params = {
            "start": _to_api_time(start_iso),
            "end": _to_api_time(end_iso)  ,
            "offset": str(offset),
            "limit": str(page),
            "sort": "TimeDK",
            "filter": json.dumps({"PriceArea": areas}),
        }
        url = f"{BASE}?{urlencode(params)}"
        
        payload = _get_json(url)

        records = payload.get("records", [])
        seen.extend(records)
        if len(records) < page:
           break
        offset += page
        time.sleep(PAGE_DELAY)
    return seen




UPSERT_SQL = """
    INSERT INTO prices (area, ts_dk, dkk_per_kwh, eur_per_kwh, fetched_at)
    VALUES (%s, %s, %s, %s, now())
    ON CONFLICT (area, ts_dk) DO UPDATE
       SET dkk_per_kwh = EXCLUDED.dkk_per_kwh,
           eur_per_kwh = EXCLUDED.eur_per_kwh,
           fetched_at  = now()
      WHERE prices.dkk_per_kwh IS DISTINCT FROM EXCLUDED.dkk_per_kwh
         OR prices.eur_per_kwh IS DISTINCT FROM EXCLUDED.eur_per_kwh
    RETURNING 1
"""


def upsert(conn, rows):
    """Indsætter eller opdaterer priser. Returnerer antal faktisk ændrede rækker."""
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(UPSERT_SQL, rows)
        changed = len(cur.fetchall()) if cur.description else 0
    conn.commit()
    return changed


def main():
    with psycopg.connect(DSN) as conn:
        watermark = get_watermark(conn) #or START_DATE
        if watermark is None:
            watermark = datetime(2025, 10, 3, tzinfo=timezone.utc)

        window_from = watermark - timedelta(days=2)

        window_to   = datetime.now(timezone.utc) + timedelta(days=2)

        run_id = start_run(conn, window_from, window_to)
        try:
            records = fetch_window(window_from, window_to, AREAS)   # datetime-objekter
            rows = [
                (r["PriceArea"], _to_utc(r["TimeDK"]),
                round(r["DayAheadPriceDKK"] / 1000, 4),
                round(r["DayAheadPriceEUR"] / 1000, 4))
                for r in records if r.get("PriceArea") in AREAS
            ]
            written = upsert(conn, rows)
            if records:
                set_watermark(conn, max(_to_utc(r["TimeDK"]) for r in records))
            finish_run(conn, run_id, "ok", len(records), written)
            
        except ApiError as exc:
            i = exc.info
            finish_run(conn, run_id, "failed", error=f"HTTP {i['status']}: {i['message']}",
                    http_status=i["status"], http_message=i["message"],
                    error_url=i["url"], error_body=json.dumps(i["body"]),
                    attempts=i["attempts"], elapsed_ms=i["elapsed"])
            raise
        except Exception as exc:
            finish_run(conn, run_id, "failed", error=repr(exc))
            raise

if __name__ == "__main__":
    main()
