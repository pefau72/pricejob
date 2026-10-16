#!/usr/bin/env python3
"""
push_weather.py — renderer vejrdata fra PostgreSQL til JSON og uploader til
netsite via SFTP.

Bygget oven på samme struktur som push_prices.py, med vilje: samme robust-
hedsmodel, samme run-log, samme content_hash-dedup. Databasen er sandheden;
webserveren er et cachet spejl af den.

    Kilder til nedetid og hvad der sker:
    ----------------------------------------------------------------------------
    Netsite nede (SFTP svarer ikke / afviser)
        -> intet uploades; intet markeres som sendt
        -> næste kørsel uploader hele efterslæbet

    Egen server nede (jobbet kører ikke)
        -> intet går tabt; når maskinen er oppe, uploades alt hvad der mangler

    Netsite har slettet filer / nyt webhotel
        -> --rebuild pusher alt igen uden at spørge

HVAD DER ER ANDERLEDES END push_prices.py — og hvorfor:

    Priser er pr. DAG, og nøglen er datoen. Vejr er ikke én tidsserie men tre
    slags indhold, der ændrer sig i vidt forskellige tempi:

      kind='normal'    p10/p90-båndet, glattet over elleve år.
                       Ændrer sig næsten aldrig — kun når et nyt år tilføjes.
      kind='season'    fit-koefficienterne (sin/cos). Ændrer sig når fittet
                       genkøres.
      kind='current'   Årets ugemålinger. Ændrer sig hver uge.

    Derfor er pushed_state nøglet på (kind) i stedet for (day). Uden det kunne
    man ikke pushe årets kurve ugentligt uden at gensende båndet hver gang —
    og de grå områder skal netop IKKE sendes så tit.

    content_hash gør resten af sig selv: er båndet uændret, springes det over.

    FORMATET ER TAL-FÆRDIGT. Al trigonometri sker her, ikke i browseren.
    Frontenden indlæser færdige arrays og tegner dem direkte.

Kør:
    python3 push_weather.py                # upload alt der har ændret sig
    python3 push_weather.py --dry-run      # skriv filer lokalt, rør ikke nettet
    python3 push_weather.py --rebuild      # upload ALT uanset pushed_state
    python3 push_weather.py --kind normal  # kun én slags
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

HERE = Path(__file__).parent
CPH = ZoneInfo("Europe/Copenhagen")


def _parse_env_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):]
    if "=" not in line:
        return None
    k, v = line.split("=", 1)
    k, v = k.strip(), v.strip()
    # Fjern citationstegn — OGSÅ apostroffer. En .env-linje som
    #   DMI_API_KEY="'abc'"
    # efterlod apostroffer i værdien og gav stille tomme API-svar.
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return k, v


def load_env_file(path: Path | None = None, override: bool = False) -> None:
    path = path or (HERE / ".env")
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        parsed = _parse_env_line(raw)
        if not parsed:
            continue
        k, v = parsed
        if override or k not in os.environ:
            os.environ[k] = v


# ---------------------------------------------------------------- konfiguration

JOB = "push_weather"

# De tre slags indhold. Rækkefølgen er også upload-rækkefølgen.
KINDS = ("normal", "season", "current")

# Parametre der pushes. cloud_pct er udeladt: kun to af seks stationer
# rapporterer skydække, så et "danskk" skydække ville være et sjællandsk.
# precip_mm: R² = 0,442 — svag men ægte sæson, se weather-schema.sql.
PARAMETERS = ("temperature_c", "radiation_wm2", "wind_ms")

PARAM_META = {
    "temperature_c": {"unit": "°C",   "label_da": "Temperatur",   "label_en": "Temperature"},
    "radiation_wm2": {"unit": "W/m²", "label_da": "Globalstråling", "label_en": "Global radiation"},
    "wind_ms":       {"unit": "m/s",  "label_da": "Vindhastighed", "label_en": "Wind speed"},
}

SFTP_HOST = os.environ.get("SFTP_HOST", "sftp.netsite.dk")
SFTP_PORT = os.environ.get("SFTP_PORT", "2222")
SFTP_USER = os.environ.get("SFTP_USER", "regenda.dk")
SFTP_KEY = os.environ.get("SFTP_KEY")
SFTP_PASSWORD = os.environ.get("SFTP_PASSWORD")

# Vejrdata i en søster-mappe til priserne, så de to datasæt kan rebuildes
# uafhængigt af hinanden.
REMOTE_ROOT = os.environ.get("SFTP_WEATHER_ROOT", "/web/weatherdata")
REMOTE_NORMALS = f"{REMOTE_ROOT}/normals"

# Båndet er en del af "normal"-filen, men lægges i underbiblioteket så
# frontenden kan hente det separat og cache det længe.
LOCAL_DIR = Path(os.environ.get("PUSH_WEATHER_OUT", HERE / "out" / "weather"))

TRANSFER_ATTEMPTS = 3
TRANSFER_BACKOFF = 10
TRANSFER_TIMEOUT = 30


# ------------------------------------------------------------------- skema/DDL
#
# pushed_state er en UDVIDELSE af prisjobbetets tabel: samme tabel, ny kolonne.
# Prisjobbet bruger (day); vejrjobbet bruger (kind). Begge kan skrive i den.
# Det er med vilje - en tabel at kigge i, ikke to.
#
# Migreringen er ikke en ny tabel: pushed_state findes allerede med 'day' som
# PRIMARY KEY fra prisjobbet, og vejrjobbet skal noegle paa 'kind' i SAMME
# tabel. day er PRIMARY KEY og kan ikke vaere NULL, saa vejr-raekker (hvor day
# er NULL) ikke kan indsaettes foer den er sluppet. Vi giver prisjobbet sin
# noegle tilbage som et delvist unikt indeks, saa dets adfaerd er uaendret.
DDL_PUSHED_STATE = """
-- MIGRERING, ikke en ny tabel. Se noten ovenfor.
--
-- TO ting holder day obligatorisk, og begge skal fjernes:
--
--   1. PRIMARY KEY — en PK kan ikke vaere NULL.
--   2. NOT NULL pa kolonnen selv. At droppe PK'en fjerner IKKE NOT NULL;
--      det var praecis den fejl der gav NotNullViolation ved foerste upload.
--
-- Raekkefoelgen betyder noget: DROP NOT NULL foerst naar PK'en er af.
ALTER TABLE pushed_state ADD COLUMN IF NOT EXISTS kind text;

ALTER TABLE pushed_state DROP CONSTRAINT IF EXISTS pushed_state_pkey;

ALTER TABLE pushed_state ALTER COLUMN day DROP NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS pushed_state_day_key
    ON pushed_state (day) WHERE day IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS pushed_state_kind_key
    ON pushed_state (kind) WHERE kind IS NOT NULL;
"""

DDL_PUSH_RUNS = """
CREATE TABLE IF NOT EXISTS push_runs (
    run_id         bigserial PRIMARY KEY,
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    status         text        NOT NULL,
    days_planned   int         NOT NULL DEFAULT 0,
    days_uploaded  int         NOT NULL DEFAULT 0,
    days_skipped   int         NOT NULL DEFAULT 0,
    bytes_uploaded bigint      NOT NULL DEFAULT 0,
    error          text
);
"""


class PushError(RuntimeError):
    """Fejl vi gerne vil have rapportret pænt i push_runs."""


def log(msg: str) -> None:
    print(f"{datetime.now(CPH):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def get_dsn() -> str:
    raw = os.environ.get("DATABASE_URL") or os.environ.get("PG_DSN")
    if raw:
        return raw
    return psycopg.conninfo.make_conninfo(
        host=os.environ["PGHOST"],
        port=os.environ.get("PGPORT", "41326"),
        dbname=os.environ.get("PGDATABASE", "Regenda"),
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
    )


# ==================================================================== DB-laget
# Samme form som push_prices.py, så de to jobs opfører sig ens.

def ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(DDL_PUSHED_STATE)
        cur.execute(DDL_PUSH_RUNS)
    conn.commit()


def db_start_run(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO push_runs (status) VALUES ('running') RETURNING run_id")
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def db_finish_run(conn, run_id: int, status: str, **cols) -> None:
    """
    Lukker logposten.

    VIKTIGT: vi ruller først tilbage. Fejler en tidligere sætning, står
    forbindelsen i en aborteret transaktion, og Postgres afviser ALLE
    efterfølgende sætninger med InFailedSqlTransaction. Uden rollback ville
    vi ikke kunne skrive selve fejlårsagen til push_runs — altså netop den
    post tabellen findes for.
    """
    try:
        conn.rollback()
    except Exception:
        pass

    if not cols:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE push_runs SET finished_at = now(), status = %s WHERE run_id = %s",
                (status, run_id),
            )
        conn.commit()
        return

    sets = ", ".join(f"{k} = %s" for k in cols)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE push_runs SET finished_at = now(), status = %s, {sets} "
            f"WHERE run_id = %s",
            (status, *cols.values(), run_id),
        )
    conn.commit()


def pushed_hashes(conn) -> dict[str, str]:
    """De kind'er vi tidligere har uploadet, med deres indholds-hash."""
    with conn.cursor() as cur:
        cur.execute("SELECT kind, content_hash FROM pushed_state WHERE kind IS NOT NULL")
        return {r[0]: r[1] for r in cur.fetchall()}


def record_pushed(conn, kind: str, rows: int, digest: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pushed_state (kind, pushed_at, rows_pushed, content_hash)
            VALUES (%s, now(), %s, %s)
            ON CONFLICT (kind) WHERE kind IS NOT NULL DO UPDATE
               SET pushed_at    = now(),
                   rows_pushed  = EXCLUDED.rows_pushed,
                   content_hash = EXCLUDED.content_hash
            """,
            (kind, rows, digest),
        )
    conn.commit()


def forget_pushed(conn, kinds: list[str]) -> None:
    if not kinds:
        return
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pushed_state WHERE kind = ANY(%s)", (kinds,))
    conn.commit()


# ==================================================================== data-laget

def fetch_normal(conn) -> dict:
    """Båndet, glattet med sin/cos pr. parameter, uge 1..52.

    Glatningen sker HER, ikke i browseren. Frontenden får færdige tal.
    """
    out = {}
    for param in PARAMETERS:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT wk, w_p10, w_median, w_p90
                  FROM public.weather_national
                 WHERE parameter = %s
                 ORDER BY wk
                """,
                (param,),
            )
            rows = cur.fetchall()
        if not rows:
            continue

        wk = [r[0] for r in rows]
        p10 = [float(r[1]) for r in rows]
        med = [float(r[2]) for r in rows]
        p90 = [float(r[3]) for r in rows]

        c_p10 = _fit_harmonics(wk, p10)
        c_p90 = _fit_harmonics(wk, p90)

        # GULV VED NUL GÆLDER KUN STRÅLING.
        #
        # Første version satte max(0.0, ...) på ALLE parametre for at undgå
        # negative strålingsværdier. Det var en fejl: temperatur og vind kan
        # gå under nul, og for temperatur blev p10-kurven klippet op til 0.0
        # hele vinteren. Resultatet var et bånd med en RET underkant der lå
        # over de rigtige p10-værdier — og i vinterugerne over medianen, hvilket
        # ikke kan lade sig gøre for et p10-bånd.
        floor_at_zero = (param == "radiation_wm2")

        def _clamp(v: float) -> float:
            return max(0.0, v) if floor_at_zero else v

        lo = [_clamp(_eval_harmonics(c_p10, w)) for w in wk]
        hi = [_clamp(_eval_harmonics(c_p90, w)) for w in wk]

        out[param] = {
            "unit": PARAM_META[param]["unit"],
            # band_lo er den usynlige underkant i frontenden; band_thick er
            # tykkelsen ovenpå. Summen er p90. Derfor må band_thick ikke
            # klippes ved nul — den er en TYKKELSE, ikke en temperatur.
            "band_lo": [round(v, 1) for v in lo],
            "band_hi": [round(v, 1) for v in hi],
            "band_thick": [round(hi[i] - lo[i], 1) for i in range(len(wk))],
            "median": [round(v, 1) for v in med],
        }
    return out


def fetch_season(conn) -> dict:
    """Fit-koefficienterne, som frontenden kan bruge til at tegne kurven."""
    out = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT parameter, r2, coef
              FROM public.weather_season_fit
             WHERE location_id = 'DK'
            """
        )
        for param, r2, coef in cur.fetchall():
            idx = PARAMETERS.index(param) if param in PARAMETERS else -1
            if idx < 0:
                continue
            c = json.loads(coef) if isinstance(coef, str) else coef
            out[param] = {
                "unit": PARAM_META[param]["unit"],
                "r2": float(r2),
                "coef": [float(v) for v in c],
            }
    return out


def fetch_current(conn, year: int) -> dict:
    """Årets ugemålinger pr. parameter. Takket, som målt."""
    out = {}
    for param in PARAMETERS:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT extract(week FROM ts_dk AT TIME ZONE 'Europe/Copenhagen')::int AS wk,
                       avg(CASE %s
                             WHEN 'temperature_c' THEN temperature_c
                             WHEN 'radiation_wm2' THEN radiation_wm2
                             WHEN 'wind_ms'       THEN wind_ms
                           END) AS v
                  FROM public.weather_usable
                 WHERE source_id = 'dmi'
                   AND kind = 'observation'
                   AND extract(isoyear FROM ts_dk AT TIME ZONE 'Europe/Copenhagen') = %s
                 GROUP BY wk
                 ORDER BY wk
                """,
                (param, year),
            )
            rows = cur.fetchall()
        if not rows:
            continue
        out[param] = {
            "unit": PARAM_META[param]["unit"],
            "weeks": [int(r[0]) for r in rows],
            "values": [round(float(r[1]), 1) if r[1] is not None else None for r in rows],
        }
    return out


# -------------------------------------------------------- harmonisk fit (delt)
#
# Samme model som weather-harmonic-fit.py: konstantled + to harmoniske,
# ω fastlåst til ét omløb pr. år. Ligger her fordi push-jobbet glatter
# båndet ved afsendelse — frontenden skal ikke regne trigonometri.

def _fit_harmonics(weeks, values, n_harmonics: int = 2):
    n = len(values)
    cols = [[1.0] * n]
    for h in range(1, n_harmonics + 1):
        cols.append([math.sin(2 * math.pi * h * (w - 1) / 52.0) for w in weeks])
        cols.append([math.cos(2 * math.pi * h * (w - 1) / 52.0) for w in weeks])
    k = len(cols)

    A = [[sum(cols[i][m] * cols[j][m] for m in range(n)) for j in range(k)] for i in range(k)]
    b = [sum(cols[i][m] * values[m] for m in range(n)) for i in range(k)]

    for i in range(k):
        p = max(range(i, k), key=lambda r: abs(A[r][i]))
        A[i], A[p] = A[p], A[i]
        b[i], b[p] = b[p], b[i]
        if abs(A[i][i]) < 1e-12:
            continue
        for r in range(i + 1, k):
            f = A[r][i] / A[i][i]
            for c in range(i, k):
                A[r][c] -= f * A[i][c]
            b[r] -= f * b[i]

    x = [0.0] * k
    for i in range(k - 1, -1, -1):
        s = b[i]
        for j in range(i + 1, k):
            s -= A[i][j] * x[j]
        x[i] = s / A[i][i] if abs(A[i][i]) > 1e-12 else 0.0
    return x


def _eval_harmonics(coef, week) -> float:
    t = (week - 1) / 52.0
    y = coef[0]
    for h in range(1, (len(coef) - 1) // 2 + 1):
        y += coef[2 * h - 1] * math.sin(2 * math.pi * h * t)
        y += coef[2 * h] * math.cos(2 * math.pi * h * t)
    return y


# ================================================================= byg filerne

def canonical_bytes(payload: dict) -> bytes:
    """Deterministisk JSON: sorterede nøgler, fast separator, ingen NaN."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def content_digest(payload: dict) -> str:
    """Hash af INDHOLDET, uden tidsstemplet.

    generated_at aendrer sig ved hver koersel. Indgaar det i hashen, bliver
    hashen ny hver gang, og dedup'en virker aldrig — 'uændret' kan ikke
    indtraeffe. push_prices.py loeser det ved at holde tiden udenfor hashen;
    det samme goer vi her.
    """
    body = {k: v for k, v in payload.items() if k != "generated_at"}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def build_files(conn, year: int) -> dict[str, tuple[Path, dict]]:
    """Bygger de tre filer. Returnerer {kind: (sti, payload)}."""
    files: dict[str, tuple[Path, dict]] = {}

    normal = fetch_normal(conn)
    if normal:
        payload = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "weeks": list(range(1, 53)),
            "parameters": normal,
        }
        files["normal"] = (LOCAL_DIR / "normals" / "weather-normal.json", payload)

    season = fetch_season(conn)
    if season:
        payload = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model": "c0 + a1*sin(2pi t) + b1*cos(2pi t) + a2*sin(4pi t) + b2*cos(4pi t), t=(week-1)/52",
            "parameters": season,
        }
        files["season"] = (LOCAL_DIR / "normals" / "weather-season.json", payload)

    current = fetch_current(conn, year)
    if current:
        payload = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "year": year,
            "parameters": current,
        }
        files["current"] = (LOCAL_DIR / f"weather-{year}.json", payload)

    return files


# ================================================================ SFTP-laget
# Uændret mønster fra push_prices.py — samme lftp-opsætning, samme genforsøg.

SFTP_EXTRA_OPTS = [
    "-o", "HostKeyAlgorithms=+ssh-rsa",
    "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "BatchMode=yes" if SFTP_KEY else "BatchMode=no",
]

CONNECT_PROGRAM = "ssh -a -x " + " ".join(SFTP_EXTRA_OPTS)


def lftp_argv(script: str) -> list[str]:
    """
    Pakker scriptet ind med forbindelsesopsætning og open.

    Laeg maerke til at OPEN er her — ikke i kalderens script. Kalderen sender
    kun sine egne kommandoer (mkdir, put); forbindelsen aabnes af denne
    funktion. Uden det fejler hvert put med "Not connected", fordi lftp ikke
    selv forbinder naar den faar et script via -e.
    """
    target = f"sftp://{SFTP_HOST}:{SFTP_PORT}"

    pre = (
        f"set sftp:connect-program '{CONNECT_PROGRAM}'; "
        "set sftp:auto-confirm yes; "
        "set net:timeout 20; "
        "set net:max-retries 2; "
        "set net:reconnect-interval-base 5; "
    )

    commands = (
        f"{pre}"
        f"open -u {SFTP_USER},{SFTP_PASSWORD} {target}; "
        f"{script}; "
        "bye"
    )

    return ["lftp", "-e", commands]


def build_env() -> dict:
    """
    Miljøet som lftp skal køre i.

    lftp læser sit password fra LFTP_PASSWORD. Det variabelnavn findes ikke i
    .env — der hedder den SFTP_PASSWORD — så uden denne oversættelse får lftp
    intet password og fejler med en tom login.
    """
    env = os.environ.copy()
    if SFTP_PASSWORD:
        env["LFTP_PASSWORD"] = SFTP_PASSWORD
    # Undgå at lftp arver et gammelt SSHPASS og bruger det ved en fejl
    env.pop("SSHPASS", None)
    return env


def require_lftp() -> None:
    if shutil.which("lftp") is None:
        raise PushError("lftp er ikke installeret (sudo apt install lftp)")


def _lftp_reported_error(out: str) -> bool:
    """
    lftp skriver fejl til stdout og returnerer ofte 0. Vi leder derfor efter
    de kendetegn, der betyder "filen kom ikke af sted".
    """
    for line in out.splitlines():
        low = line.lower()
        if "fatal error" in low or "access failed" in low or "no such file" in low:
            return True
        if low.startswith("put:") and ("denied" in low or "failed" in low):
            return True
    return False


def probe_sftp(env: dict) -> tuple[bool, str]:
    """Samme lftp-opsætning som den egentlige upload — ellers lyver proben."""
    require_lftp()
    argv = lftp_argv("pwd")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              env=env, timeout=60)
    except subprocess.TimeoutExpired:
        return False, "timeout efter 60s (netsite svarede ikke)"
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 or _lftp_reported_error(out):
        return False, out.strip() or f"exit {proc.returncode}"
    line = next((l for l in out.splitlines() if l.startswith("/")), "")
    return True, line or "ok"


def run_sftp(script: str, env: dict) -> None:
    """Kører transferen med genforsøg. Fejler den alle gange, kastes PushError."""
    require_lftp()
    argv = lftp_argv(script)
    last = ""
    for attempt in range(1, TRANSFER_ATTEMPTS + 1):
        proc = subprocess.run(argv, capture_output=True, text=True,
                              env=env, timeout=TRANSFER_TIMEOUT * 6)
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0 and not _lftp_reported_error(out):
            return
        last = out.strip() or f"exit {proc.returncode}"
        # Vis HVAD lftp sagde. Uden dette stod vi med "forsøg fejlede" og
        # ingen forklaring — og fejlen kunne vaere alt fra auth til en
        # ugyldig kommando i scriptet.
        detail = " | ".join(l for l in out.splitlines() if l.strip())[:400]
        log(f"    forsøg {attempt} fejlede (exit {proc.returncode}): {detail}")
        if attempt < TRANSFER_ATTEMPTS:
            wait = TRANSFER_BACKOFF * (2 ** (attempt - 1))
            log(f"    venter {wait}s")
            time.sleep(wait)
    raise PushError(f"SFTP fejlede efter {TRANSFER_ATTEMPTS} forsøg: {last[:400]}")


# ======================================================================= main

@dataclass
class Plan:
    """De filer der faktisk skal sendes, og hvorfor."""
    remote_dir: str
    local: Path
    kind: str

    @property
    def remote(self) -> str:
        return f"{self.remote_dir}/{self.local.name}"


def parse_args():
    ap = argparse.ArgumentParser(description="Push vejrdata til netsite via SFTP")
    ap.add_argument("--dry-run", action="store_true",
                    help="byg filer lokalt, men rør ikke netværket")
    ap.add_argument("--rebuild", action="store_true",
                    help="ignorér pushed_state og upload alt")
    ap.add_argument("--kind", choices=KINDS, default=None,
                    help="kun denne slags (default: alle)")
    ap.add_argument("--mkdir-once", action="store_true",
                    help="opret de fjernbiblioteker der mangler, og afbryd. "
                         "Kør én gang efter et nyt webhotel.")
    return ap.parse_args()


def make_remote_dirs(env: dict) -> int:
    """Opretter /web/weatherdata og /web/weatherdata/normals.

    Separat kommando, fordi mkdir i det almindelige upload-script ville faa
    lftp til at skrive "Access failed: File already exists" hver gang — og
    _lftp_reported_error() tolker det som en fejl.
    """
    pre = (
        f"set sftp:connect-program '{CONNECT_PROGRAM}'; "
        "set sftp:auto-confirm yes; "
        "set net:timeout 20; "
    )
    cmds = [f"mkdir {REMOTE_ROOT}", f"mkdir {REMOTE_NORMALS}"]
    script = "; ".join(cmds)
    argv = ["lftp", "-e",
            f"{pre}open -u {SFTP_USER},{SFTP_PASSWORD} "
            f"sftp://{SFTP_HOST}:{SFTP_PORT}; {script}; bye"]
    proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=120)
    out = (proc.stdout or "") + (proc.stderr or "")
    log(out.strip() or "mkdir: ingen output")
    # "File already exists" er OK her — vi ville bare sikre os at de findes.
    log("biblioteker sikret (en 'File already exists' er i orden)")
    return 0


def main() -> int:
    args = parse_args()

    ENV_FILE = Path(os.environ.get("PUSH_ENV_FILE", Path(__file__).parent / ".env"))
    load_env_file(ENV_FILE)
    globals().update({
        k: os.environ[k]
        for k in ("SFTP_HOST", "SFTP_PORT", "SFTP_USER", "SFTP_KEY",
                  "SFTP_PASSWORD", "SFTP_WEATHER_ROOT")
        if k in os.environ
    })
    if ENV_FILE.is_file():
        log(f"indlæste {ENV_FILE}")
    else:
        log(f"advarsel: ingen .env fundet ({ENV_FILE}) — bruger kun miljøvariabler")

    env = os.environ.copy()

    if args.mkdir_once:
        return make_remote_dirs(env)

    kinds = (args.kind,) if args.kind else KINDS
    year = datetime.now(CPH).year

    if not args.dry_run:
        ok, info = probe_sftp(env)
        if not ok:
            log(f"netsite svarer ikke på SFTP — afbryder uden at bygge: {info}")
            return 0
        log(f"SFTP-forbindelse OK ({info.splitlines()[0] if info else 'ok'})")

    with psycopg.connect(get_dsn()) as conn:
        ensure_tables(conn)
        run_id = db_start_run(conn)
        stats: dict = {}

        try:
            built = build_files(conn, year)
            if not built:
                log("intet at bygge — er weather_national og weather_season_fit fyldt?")
                if run_id is not None:
                    db_finish_run(conn, run_id, "ok", days_planned=0)
                return 0

            known = {} if args.rebuild else pushed_hashes(conn)
            if args.rebuild:
                log(f"--rebuild: glemmer pushed_state for {len(known)} kind'er")
                forget_pushed(conn, list(known))
                known = {}

            plans: list[Plan] = []
            skipped = 0
            for kind in kinds:
                if kind not in built:
                    continue
                path, payload = built[kind]
                data = canonical_bytes(payload)
                write_atomic(path, data)

                digest = content_digest(payload)
                if known.get(kind) == digest:
                    skipped += 1
                    log(f"  {kind:<8} uændret — springer over")
                    continue

                remote_dir = REMOTE_NORMALS if kind in ("normal", "season") else REMOTE_ROOT
                plans.append(Plan(remote_dir=remote_dir, local=path, kind=kind))
                log(f"  {kind:<8} {len(data):>7} bytes  ->  {plans[-1].remote}")

            stats = {
                "days_planned": len(plans),
                "days_skipped": skipped,
            }

            if not plans:
                log("intet nyt at sende — alt er allerede ude")
                if run_id is not None:
                    db_finish_run(conn, run_id, "ok", **stats, days_uploaded=0,
                                  bytes_uploaded=0)
                return 0

            total_bytes = sum(p.local.stat().st_size for p in plans)

            if args.dry_run:
                log(f"DRY RUN: ville sende {len(plans)} filer ({total_bytes} bytes) "
                    f"til {REMOTE_ROOT}")
                for p in plans:
                    log(f"    {p.local.name}  ({p.local.stat().st_size} bytes)")
                db_finish_run(conn, run_id, "dry_run", **stats, bytes_uploaded=0)
                return 0

            # Upload. mkdir -p pr. bibliotek, derefter put.
            # INGEN mkdir her. To grunde:
            #
            #   1. lftp's mkdir tager ikke '-p', og 'mkdir -p' er en shell-
            #      kommando der fejler i et lftp-script.
            #   2. Selv med 'set cmd:fail-exit no' skriver lftp
            #      "mkdir: Access failed: File already exists", og
            #      _lftp_reported_error() ser "access failed" og melder fejl —
            #      saa jobbet proevede forgaeves igen og naaede aldrig
            #      record_pushed(). push-weather.py --rebuild goer det ene
            #      fornoedne: opretter mapperne.
            #
            # Bibliotekerne findes; put skriver til en eksisterende mappe uden
            # videre. 'open' og 'bye' tilfoejes af lftp_argv.
            script_lines = [f"put {p.local} -o {p.remote}" for p in plans]
            script = "; ".join(script_lines)
            run_sftp(script, env)
            log(f"uploadet {len(plans)} filer ({total_bytes} bytes) til {REMOTE_ROOT}")

            # Først NU markeres de som sendt. Fejler uploaden, står
            # pushed_state stille og næste kørsel tager hele efterslæbet.
            for p in plans:
                _, payload = built[p.kind]
                digest = content_digest(payload)   # SAMME digest som i planen
                rows = sum(len(v.get("values", v.get("median", [])))
                           for v in payload.get("parameters", {}).values())
                record_pushed(conn, p.kind, rows, digest)

            db_finish_run(conn, run_id, "ok", **stats,
                          days_uploaded=len(plans), bytes_uploaded=total_bytes)
            return 0

        except PushError as exc:
            log(f"FEJL: {exc}")
            if run_id is not None:
                db_finish_run(conn, run_id, "failed", **stats, error=str(exc))
            # Bevidst exit 0: et SFTP-nedbrud er forventet drift, ikke en
            # defekt unit. Fejlen står i push_runs; notify.py fanger den.
            return 0
        except Exception as exc:
            log(f"Uventet fejl: {exc!r}")
            if run_id is not None:
                db_finish_run(conn, run_id, "failed", **stats, error=repr(exc))
            return 0


if __name__ == "__main__":
    sys.exit(main())
