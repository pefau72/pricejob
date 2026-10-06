#!/usr/bin/env python3
"""
push_prices.py — renderer prisdata fra PostgreSQL til JSON og uploader til netsite via SFTP.

Robusthedsmodel: hele verden beskrives ved, hvad der ligger i databasen.
Databasen er sandheden; webserveren er et cachet spejl af den.

    Kilder til nedetid og hvad der sker:
    ----------------------------------------------------------------------------
    Netsite nede (SFTP svarer ikke / afviser)
        -> intet uploades; intet markeres som sendt
        -> næste kørsel uploader HELE efterslæbet, fordi efterslæbet er defineret
           som "dage der findes i DB men ikke står i pushed_state"

    Egen server nede (jobbet kører ikke)
        -> der samles ikke nye priser op (det gør collect-jobbet), men straks
           maskinen er oppe igen, uploader dette job hele efterslæbet

    Begge nede i dage
        -> samme som ovenstående; intet går tabt, fordi der ikke er noget
           "vindue" der udløber — kun en mængde dage der mangler ude hos netsite

    Netsite oppe, men kun nogle dage mangler
        -> kun de manglende dage uploades; de øvrige røres ikke

    Netsite har slettet filer / ny SFTP-konto / nyt webhotel
        -> --rebuild pusher alt igen uden at spørge

    DST-dage (23/25 timer), huller i data, dubletter pr. time
        -> håndteres eksplicit; se split_days() og build_points()

Kør:
    python3 push_prices.py                # upload efterslæb + i dag + i morgen
    python3 push_prices.py --dry-run      # skriv filer lokalt, rør ikke nettet
    python3 push_prices.py --rebuild      # upload ALT uanset pushed_state
    python3 push_prices.py --days 7       # hvor mange dage tilbage der maks. bygges
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

CPH = ZoneInfo("Europe/Copenhagen")

# ------------------------------------------------------------------- .env-læsning
#
# Vi læser .env selv i stedet for at forlade os på at en shell har sourced den.
# To grunde:
#   1. cron/systemd har ingen shell der sourcer noget — uden dette ville jobbet
#      kun virke når man kørte det i hånden fra sin login-shell.
#   2. .env har sine EGNE quoting-regler (se _parse_env_line). Et password der
#      slutter på '#' bliver afkortet hvis linjen ikke er quoted, og fejlen
#      viser sig som "Permission denied" — ikke som en parse-fejl.
#
# Regel: værdien må gerne stå i enkelt- eller dobbeltanførselstegn. '#' uden for
# anførselstegn starter en kommentar. Vi overskriver ALDRIG en variabel der
# allerede findes i miljøet, så en eksplicit eksport i crontab vinder.


def _parse_env_line(line: str) -> tuple[str, str] | None:
    """Parser én .env-linje. Returnerer (nøgle, værdi) eller None."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].lstrip()
    if "=" not in line:
        return None

    key, _, raw = line.partition("=")
    key = key.strip()
    raw = raw.strip()
    if not key:
        return None

    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        quote = raw[0]
        value = raw[1:-1]
        if quote == '"':
            # I dobbeltanførselstegn udvides escapes; i enkelt ikke.
            value = (
                value.replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace('\\"', '"')
                .replace("\\\\", "\\")
            )
    else:
        # Uquoted: en '#' indleder en kommentar. DETTE er fælden der afkorter
        # et password der slutter på '#'.
        hash_pos = raw.find("#")
        if hash_pos != -1:
            raw = raw[:hash_pos]
        value = raw.rstrip()

    return key, value


def load_env_file(path: Path | None = None, override: bool = False) -> None:
    """
    Indlæser .env ind i os.environ.

    override=False (default): variabler der ALLEREDE er sat i miljøet vinder.
    Det gør crontab/systemd til den stærkeste kilde, hvilket er den rigtige
    prioritet — en eksplicit sat variabel skal ikke kunne overskrives af en
    fil nogen glemte.
    """
    path = path or Path(os.environ.get("PUSH_ENV_FILE", Path(__file__).parent / ".env"))
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(line)
        if parsed is None:
            continue
        key, value = parsed
        if override or key not in os.environ:
            os.environ[key] = value


# ---------------------------------------------------------------- konfiguration

JOB = "push_prices"

DB_JOB = "dayahead_prices"          # navnet collect-jobbet skriver under i sync_state
AREAS = ("DK1", "DK2", "DE", "NO2", "SE3", "SE4")

# Hvor mange dage tilbage vi *maksimalt* bygger, første gang vi kører mod en tom DB.
# Uden en grænse ville en ny DB med to års historik forsøge at bygge 730 filer.
MAX_BACKFILL_DAYS = int(os.environ.get("PUSH_MAX_BACKFILL_DAYS", "30"))

# Hvor mange dage frem vi prøver. Day-ahead offentliggøres typisk omkring kl. 13
# dansk tid, så i morgen findes ikke altid — den fil bliver så blot ikke bygget.
DAYS_AHEAD = 1

# SFTP
SFTP_HOST = os.environ.get("SFTP_HOST", "sftp.netsite.dk")
SFTP_PORT = os.environ.get("SFTP_PORT", "2222")
SFTP_USER = os.environ.get("SFTP_USER", "regenda.dk")

# Autentificering. To måder:
#   SFTP_KEY       — sti til en privat nøgle (anbefalet; kan scopes og tilbagekaldes)
#   SFTP_PASSWORD  — password, sendt til sshpass via miljøet (SSHPASS), så det
#                    ALDRIG står i argv og dermed ikke i `ps` for andre brugere
# Sæt kun én af dem. Nøgle vinder hvis begge er sat.
SFTP_KEY = os.environ.get("SFTP_KEY")
SFTP_PASSWORD = os.environ.get("SFTP_PASSWORD")

# Stier på webserveren (SFTP-roden ligger på /web, jf. fillisten)
REMOTE_ROOT = os.environ.get("SFTP_REMOTE_ROOT", "/web/pricedata")
REMOTE_HTML_ROOT = os.environ.get("SFTP_REMOTE_HTML_ROOT", "/web")

# Hvor de byggede filer lægges lokalt før upload
OUT_DIR = Path(os.environ.get("PUSH_OUT_DIR", Path(__file__).parent / "out"))

# Hvor mange gange vi prøver SELVE TRANSFEREN (ikke hele kørslen) ved netværksfejl
TRANSFER_ATTEMPTS = 3
TRANSFER_BACKOFF = 10                # sekunder, fordobles pr. forsøg
TRANSFER_TIMEOUT = 30


# ------------------------------------------------------------------- skema/DDL

DDL_PUSHED_STATE = """
CREATE TABLE IF NOT EXISTS pushed_state (
    day            date PRIMARY KEY,
    pushed_at      timestamptz NOT NULL DEFAULT now(),
    rows_pushed    int         NOT NULL,
    content_hash   text        NOT NULL
);
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


# ============================================================ små hjælpefunktioner

class PushError(RuntimeError):
    """Fejl vi gerne vil have rapportret pænt i push_runs."""


def log(msg: str) -> None:
    print(f"{datetime.now(CPH):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def parse_dsn(raw: str) -> str:
    """Accepterer både en libpq-conninfo og 'postgresql://user:pass@host:port/db'."""
    if "://" not in raw:
        return raw
    from urllib.parse import urlparse, unquote

    u = urlparse(raw)
    if u.scheme not in ("postgres", "postgresql"):
        raise PushError(f"ukendt DSN-skema: {u.scheme!r}")
    parts = [f"host={u.hostname or 'localhost'}"]
    if u.port:
        parts.append(f"port={u.port}")
    if u.path and len(u.path) > 1:
        parts.append(f"dbname={unquote(u.path[1:])}")
    if u.username:
        parts.append(f"user={unquote(u.username)}")
    if u.password:
        parts.append(f"password={unquote(u.password)}")
    return " ".join(parts)


def get_dsn() -> str:
    """
    Rækkefølge:
      1. DSN fra miljøet (eksplicit)
      2. sammensat af PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD
    Vi bygger bevidst ikke en URL med password i, så et traceback aldrig
    kan printe hemmeligheden.
    """
    if dsn := os.environ.get("DSN"):
        return parse_dsn(dsn)

    missing = [k for k in ("PGHOST", "PGUSER", "PGDATABASE") if not os.environ.get(k)]
    if missing:
        raise PushError(
            "mangler DB-konfiguration: " + ", ".join(missing)
            + " (sæt dem i .env — se README-blokken øverst i scriptet)"
        )

    parts = [
        f"host={os.environ['PGHOST']}",
        f"port={os.environ.get('PGPORT', '5432')}",
        f"dbname={os.environ['PGDATABASE']}",
        f"user={os.environ['PGUSER']}",
    ]
    if pw := os.environ.get("PGPASSWORD"):
        parts.append(f"password={pw}")
    return " ".join(parts)


# ==================================================================== DB-laget

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
    post tabellen findes for. Det var en rigtig fejl: den originale fejl blev
    begravet under en sekundær.
    """
    try:
        conn.rollback()
    except Exception:
        pass                                    # forbindelsen kan være død helt

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


def pushed_hashes(conn) -> dict[date, str]:
    """Dage vi tidligere har uploadet, med deres indholds-hash."""
    with conn.cursor() as cur:
        cur.execute("SELECT day, content_hash FROM pushed_state")
        return {r[0]: r[1] for r in cur.fetchall()}


def record_pushed(conn, day: date, rows: int, digest: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pushed_state (day, pushed_at, rows_pushed, content_hash)
            VALUES (%s, now(), %s, %s)
            ON CONFLICT (day) DO UPDATE
               SET pushed_at    = now(),
                   rows_pushed  = EXCLUDED.rows_pushed,
                   content_hash = EXCLUDED.content_hash
            """,
            (day, rows, digest),
        )
    conn.commit()


def forget_pushed(conn, days: list[date]) -> None:
    if not days:
        return
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pushed_state WHERE day = ANY(%s)", (days,))
    conn.commit()


def collect_watermark(conn) -> datetime | None:
    """
    Hvor langt collect-jobbet er kommet. Bruges KUN til at begrænse, hvor langt
    tilbage vi gider bygge ved første kørsel — ikke til at styre hvad der sendes.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT watermark_dk FROM sync_state WHERE job = %s", (DB_JOB,)
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def fetch_prices(conn, day_from: date, day_to: date) -> list[tuple]:
    """
    Henter alle timer i [day_from, day_to] (danske lokaldage, begge inklusive).

    ts_dk er timestamptz, altså et absolut tidspunkt. Vi sammenligner derfor
    mod døgnets faktiske grænser i Europe/Copenhagen — ikke mod naive datoer —
    så DST-dage med 23 eller 25 timer kommer korrekt med.
    """
    start = datetime.combine(day_from, datetime.min.time(), tzinfo=CPH)
    end = datetime.combine(day_to + timedelta(days=1), datetime.min.time(), tzinfo=CPH)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT area, ts_dk, dkk_per_kwh, eur_per_kwh
              FROM prices
             WHERE ts_dk >= %s AND ts_dk < %s
             ORDER BY ts_dk, area
            """,
            (start, end),
        )
        return cur.fetchall()


# ================================================================ bygning af JSON

def local_day(ts: datetime) -> date:
    """Hvilken dansk kalenderdag et tidsstempel hører til."""
    if ts.tzinfo is None:                     # burde ikke ske med timestamptz
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(CPH).date()


def split_days(rows) -> dict[date, list[tuple]]:
    """
    Fordeler rækker på danske kalenderdage.

    En række hører til den dag dens TimeDK (dansk lokaltid) falder på. Fordi
    ts_dk er absolut og vi konverterer til CPH, lander DST-døgnene rigtigt:
    forårsdøgnet får 23 timer, efterårsdøgnet 25.
    """
    days: dict[date, list[tuple]] = defaultdict(list)
    for area, ts, dkk, eur in rows:
        days[local_day(ts)].append((area, ts, dkk, eur))
    return days


# Dansk day-ahead afregnes på KVART-intervaller (15 min), ikke hele timer.
# Et normalt døgn har derfor 96 punkter, ikke 24. Det er den opløsning
# markedet faktisk publicerer, og den skal bevares hele vejen til grafen —
# at trunkere til hele timer ville smide 75% af data væk.
INTERVAL_MINUTES = 15
POINTS_PER_DAY = 24 * 60 // INTERVAL_MINUTES          # 96


def build_points(rows) -> tuple[list[dict], list[str]]:
    """
    Bygger punktlisten (kvart-intervaller) for én dag.

    Returnerer (points, warnings). Nøglen er det fulde 15-minutters tidspunkt,
    BEVIDST ikke trunkeret til hele timer: en time indeholder fire gyldige
    punkter (12:00, 12:15, 12:30, 12:45), og de er ikke dubletter.

    En ægte kollision — samme (område, 15-minutters-instant) to gange — kan
    ikke opstå med prices' primærnøgle (area, ts_dk), men hvis den gør, slås
    den sammen og rapporteres i warnings i stedet for at vælte kørslen.
    """
    per_slot: dict[datetime, dict[str, tuple[float, float]]] = defaultdict(dict)
    warnings: list[str] = []

    for area, ts, dkk, eur in rows:
        slot = ts.astimezone(CPH).replace(second=0, microsecond=0)
        pair = (float(dkk), float(eur) if eur is not None else None)
        if slot in per_slot and area in per_slot[slot]:
            prev = per_slot[slot][area]
            if prev != pair:
                warnings.append(
                    f"dublet {slot.isoformat()} {area}: {prev} -> {pair} (sidste vinder)"
                )
            else:
                warnings.append(f"dublet {slot.isoformat()} {area}: identisk værdi")
        per_slot[slot][area] = pair

    points = []
    for slot in sorted(per_slot):
        areas = per_slot[slot]
        point = {"ts": slot.isoformat()}
        for area in AREAS:                      # DK1, DK2, DE, NO2, SE3, SE4
            key = area.lower()
            dkk, eur = areas.get(area, (None, None))
            point[key] = dkk                      # bagudkompatibel: "dk1": 1.0837
            point[f"{key}_eur"] = eur             # ny: "dk1_eur": 0.1456
        points.append(point)
    return points, warnings

def expected_points(day: date) -> int:
    """
    Antal 15-minutters punkter et komplet døgn har.

    92 / 96 / 100 på DST-skiftedage, fordi døgnet dér er 23, 24 eller 25 timer.
    Regnet ud ved at tælle de faktiske minutter mellem døgnets to midnats-
    instanser i Europe/Copenhagen — ikke ved at antage 96.
    """
    start = datetime.combine(day, datetime.min.time(), tzinfo=CPH)
    next_day = day + timedelta(days=1)
    end = datetime.combine(next_day, datetime.min.time(), tzinfo=CPH)
    minutes = (end.astimezone(timezone.utc) - start.astimezone(timezone.utc))
    return int(minutes.total_seconds() // 60 // INTERVAL_MINUTES)


def build_day_payload(day: date, rows, now_utc: datetime) -> tuple[dict, list[str]]:
    points, warnings = build_points(rows)

    exp = expected_points(day)
    found = len(points)
    if found < exp:
        warnings.append(f"ufuldstændigt døgn: {found} af {exp} kvarterspunkter")
    elif found > exp:
        warnings.append(f"flere punkter end forventet: {found} mod {exp}")

    payload = {
        "date": day.isoformat(),
        "generated_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "unit": "DKK_per_kWh",
        "unit_eur": "EUR_per_kWh", 
        "interval_minutes": INTERVAL_MINUTES,
        "areas": list(AREAS),
        "hours": points,
    }
    return payload, warnings


def canonical_bytes(payload: dict) -> bytes:
    """Deterministisk serialisering — samme data giver altid samme bytes/hash."""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def write_atomic(path: Path, data: bytes) -> None:
    """
    Skriv til temp-fil i SAMME katalog og rename. Upload sker fra disse filer,
    så en halvskreven fil kan aldrig blive sendt af sted.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ================================================================== SFTP-laget

@dataclass
class UploadPlan:
    """De filer der faktisk skal sendes, og hvorfor."""

    daily: dict[date, Path] = field(default_factory=dict)
    index: Path | None = None
    latest: Path | None = None

    def paths(self) -> list[Path]:
        return [*self.daily.values(), self.index, self.latest]

    def files(self) -> list[Path]:
        return [p for p in self.paths() if p is not None]



SFTP_EXTRA_OPTS = [
    # Netsite tilbyder kun ssh-rsa (SHA-1) som host-nøgle. Moderne OpenSSH har
    # fjernet den fra standardlisten, så forhandlingen fejler med
    # "no matching host key type found". '+' tilføjer algoritmen til listen
    # i stedet for at erstatte den, så alt moderne stadig virker.
    "-o", "HostKeyAlgorithms=+ssh-rsa",
    "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
]

# Hvorfor lftp og ikke sftp+sshpass:
#
# OpenSSH's sftp er interaktiv — den spørger om passwordet på /dev/tty. Et
# script (og især en systemd-unit) har ingen terminal, så der er intet at
# svare på prompten med. sshpass findes for at lukke dét hul, men den skal
# selv bruge en pseudo-terminal for at kunne skrive ind i prompten, og uden
# en terminal fejler den med "Permission denied (password)" — altså samme
# fejl som et FORKERT password, hvilket gjorde den svær at diagnosticere.
#
# lftp taler SFTP selv og sender passwordet selv, ligesom WinSCP gør på
# Windows. Ingen prompt, ingen pty, ingen sshpass. Det er derfor den virker
# under systemd.
#
# BEMÆRK: 'set sftp:auto-confirm yes' er obligatorisk. Uden den spørger lftp
# om lov ved mkdir/put og hænger i en unit uden terminal.
CONNECT_PROGRAM = "ssh -a -x " + " ".join(SFTP_EXTRA_OPTS)


def lftp_argv(script: str) -> list[str]:
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
    intet password og fejler med en tom login. Samme klasse fejl som de
    tidligere: en variabel der læses, men aldrig sættes.
    """
    env = os.environ.copy()
    if SFTP_PASSWORD:
        env["LFTP_PASSWORD"] = SFTP_PASSWORD
    # Undgå at lftp arver et gammelt SSHPASS og bruger det ved en fejl
    env.pop("SSHPASS", None)
    return env


def require_lftp() -> None:
    if not shutil.which("lftp"):
        raise PushError(
            "lftp findes ikke — installér den med: sudo apt install lftp"
        )


def run_sftp(script: str, env: dict) -> None:
    """
    Kører transferen med genforsøg. Fejler den alle gange, kaster vi PushError
    — og kalderen lader være med at skrive noget til pushed_state.
    """
    require_lftp()
    argv = lftp_argv(script)
    last = ""
    for attempt in range(1, TRANSFER_ATTEMPTS + 1):
        print(argv)
        proc = subprocess.run(argv, capture_output=True, text=True, env=env,
                              timeout=TRANSFER_TIMEOUT)
        # lftp returnerer 0 selv når enkelte filer fejler — den skriver til
        # stdout i stedet. Derfor tjekker vi BEGGE dele.
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0 and not _lftp_reported_error(out):
            return
        last = out.strip() or f"exit {proc.returncode}"
        if attempt < TRANSFER_ATTEMPTS:
            wait = TRANSFER_BACKOFF * (2 ** (attempt - 1))
            log(f"    transfer-forsøg {attempt} fejlede: {last.splitlines()[-1] if last else 'ukendt fejl'}")
            log(f"    venter {wait}s og prøver igen")
            import time as _t

            _t.sleep(wait)
    raise PushError(f"lftp fejlede efter {TRANSFER_ATTEMPTS} forsøg: {last}")


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
    """
    Billigt tjek FØR vi bygger og uploader alt: kan vi overhovedet logge ind?

    Bruger SAMME forbindelsesopsætning som den egentlige upload — det var en
    fejl tidligere, at proben og uploaden ikke talte samme protokol.
    """
    require_lftp()
    argv = lftp_argv("pwd")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)
    except subprocess.TimeoutExpired:
        return False, "timeout efter 60s (netsite svarede ikke)"
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 or _lftp_reported_error(out):
        return False, out.strip() or f"exit {proc.returncode}"
    line = next((l for l in out.splitlines() if l.startswith("/")), "")
    return True, line or "ok"


def probe_sftp(env: dict) -> tuple[bool, str]:
    """

    Bruger SAMME lftp-opsætning som den egentlige upload — det var en fejl
    tidligere, at proben og uploaden ikke talte samme protokol (proben kørte
    sftp+sshpass, uploaden gjorde noget andet), så proben meldte "netsite
    nede" selv når login ville have virket.
    """
    require_lftp()
    argv = lftp_argv("pwd")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)
    except subprocess.TimeoutExpired:
        return False, "timeout efter 60s (netsite svarede ikke)"
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 or _lftp_reported_error(out):
        return False, out.strip() or f"exit {proc.returncode}"
    line = next((l for l in out.splitlines() if l.startswith("/")), "")
    return True, line or "ok"


# ======================================================================= main

def plan_window(
    conn, now: datetime, rebuild: bool, max_days: int
) -> tuple[date, date, dict[date, str]]:
    """
    Afgør hvilket datointerval der overhovedet er relevant.

    Gulvet sættes af:
      - --rebuild: hele det tilgængelige spænd
      - ellers: mindste dag i pushed_state, dog aldrig længere tilbage end
        MAX_BACKFILL_DAYS eller collect-jobbets vandmærke

    Loftet er i dag + DAYS_AHEAD, fordi day-ahead ikke findes længere frem.
    """
    with conn.cursor() as cur:
        # prices har ingen 'day'-kolonne — dagen er afledt af ts_dk i Python.
        # Vi finder derfor den ældste og nyeste pris og konverterer til
        # danske kalenderdage nedenfor.
        cur.execute("SELECT min(ts_dk), max(ts_dk) FROM prices")
        row = cur.fetchone()
    if not row or row[0] is None:
        raise PushError("prices-tabellen er tom — intet at sende")

    first_data: date = row[0].astimezone(CPH).date()
    last_data: date = row[1].astimezone(CPH).date()

    today: date = now.astimezone(CPH).date()
    ceiling: date = today + timedelta(days=DAYS_AHEAD)

    if rebuild:
        floor: date = first_data
    else:
        known = pushed_hashes(conn)
        if known:
            floor = min(known)
        else:
            floor = today - timedelta(days=max_days)
            wm = collect_watermark(conn)
            if wm is not None:
                wm_day: date = wm.astimezone(CPH).date()
                floor = max(floor, wm_day - timedelta(days=1))

    floor = max(floor, today - timedelta(days=max_days))
    floor = max(floor, first_data)
    ceiling = min(ceiling, max(last_data, today))
    return floor, ceiling, ({} if rebuild else known)


def main() -> int:
    # .env SKAL indlæses før noget som helst læser konfiguration. get_dsn() og
    # SFTP_*-konstanterne læser os.environ ved modulets import, men variablerne
    # herunder slås op ved brug — så en load her i starten af main() dækker dem
    # alle. Uden dette kald virker scriptet kun når shell'en selv har eksporteret
    # variablerne, hvilket er præcis den fælde der gav "mangler DB-konfiguration".
    ENV_FILE = Path(os.environ.get("PUSH_ENV_FILE", Path(__file__).parent / ".env"))
    load_env_file(ENV_FILE)
    globals().update({
        k: os.environ[k]
        for k in ("SFTP_HOST", "SFTP_PORT", "SFTP_USER", "SFTP_KEY",
                  "SFTP_PASSWORD", "SFTP_REMOTE_ROOT", "SFTP_REMOTE_HTML_ROOT")
        if k in os.environ
    })
    if ENV_FILE.is_file():
        log(f"indlæste {ENV_FILE}")
    else:
        log(f"advarsel: ingen .env fundet ({ENV_FILE}) — bruger kun miljøvariabler")

    ap = argparse.ArgumentParser(description="Push prisdata til netsite via SFTP")
    ap.add_argument("--dry-run", action="store_true",
                    help="byg filer lokalt, men rør ikke netværket")
    ap.add_argument("--rebuild", action="store_true",
                    help="ignorér pushed_state og upload alt i vinduet")
    ap.add_argument("--days", type=int, default=MAX_BACKFILL_DAYS,
                    help=f"maks. antal dage tilbage (default {MAX_BACKFILL_DAYS})")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    env = os.environ.copy()

    # --- tjek netværket først: er netsite nede, spilder vi ikke tid på at bygge
    if not args.dry_run:
        ok, info = probe_sftp(env)
        if not ok:
            # Bevidst exit 0: en netsite-nedetid er forventet drift, ikke en
            # defekt unit. Med exit 2 melder systemd "Failed" og
            # status=2/INVALIDARGUMENT hver gang netsite er nede, hvilket
            # gør journalen ubrugelig. Fejlen registreres i push_runs, og
            # notify.py fanger den derfra.
            log(f"netsite svarer ikke på SFTP — afbryder uden at bygge: {info}")
            return 0
        log(f"SFTP-forbindelse OK ({info.splitlines()[0] if info else 'ok'})")

    with psycopg.connect(get_dsn()) as conn:
        ensure_tables(conn)
        run_id = db_start_run(conn)
        stats: dict = {}

        try:
            floor, ceiling, known = plan_window(conn, now, args.rebuild, args.days)

            if args.rebuild:
                log(f"--rebuild: glemmer pushed_state for {len(known)} dage")
                forget_pushed(conn, list(known))
                known = {}

            log(f"vindue: {floor} .. {ceiling} "
                f"({(ceiling - floor).days + 1} kalenderdage)")

            rows = fetch_prices(conn, floor, ceiling)
            by_day = split_days(rows)
            log(f"læste {len(rows)} prisrækker fordelt på {len(by_day)} danske dage")

            # --- byg + afgør hvad der skal sendes ---------------------------
            plan = UploadPlan()
            skipped = 0
            total_warnings = 0
            partial_days: list[date] = []

            for day in sorted(by_day):                     # kun dage med data
                payload, warnings = build_day_payload(day, by_day[day], now)
                if warnings:
                    total_warnings += len(warnings)
                    for w in warnings:
                        log(f"    {day}: {w}")
                if any("ufuldstændigt" in w for w in warnings):
                    partial_days.append(day)

                data = canonical_bytes(payload)
                digest = hashlib.sha256(data).hexdigest()

                # Uændret siden sidst? Så er der ikke noget at hente ude hos
                # netsite, og vi springer uploadet over. Tiden (generated_utc)
                # indgår bevidst ikke i hashen, så et rent tidsstempel-skift
                # ikke udløser en unødvendig upload.
                if not args.rebuild and known.get(day) == digest:
                    skipped += 1
                    (OUT_DIR / f"prices-{day.isoformat()}.json").write_bytes(data)  # lokal kopi
                    continue

                path = OUT_DIR / f"prices-{day.isoformat()}.json"
                write_atomic(path, data)

                # Index/latest flyttes kun ind i planen for dage der faktisk
                # sendes — ellers ville de pege på data webserveren ikke har.
                plan.daily[day] = path

            # --- hvad er "latest"? ----------------------------------------
            all_days = sorted(by_day)
            if not all_days:
                raise PushError("ingen dage at bygge")

            # Sidste dag der enten allerede er ude eller bliver sendt nu.
            uploaded_or_known = sorted(set(all_days) | set(known))
            latest_day = max(uploaded_or_known)

            if plan.daily:
                index_payload = {
                    "generated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "unit": "DKK_per_kWh",
                    "areas": list(AREAS),
                    "latest": latest_day.isoformat(),
                    "days": [d.isoformat() for d in uploaded_or_known],
                }
                plan.index = OUT_DIR / "index.json"
                write_atomic(plan.index, canonical_bytes(index_payload))

                latest_path = OUT_DIR / "latest.json"
                # latest.json er byte-identisk med den nyeste dags fil, så
                # forsiden altid henter ét kendt filnavn uden at læse index først.
                if latest_day in plan.daily:
                    write_atomic(latest_path, plan.daily[latest_day].read_bytes())
                else:
                    write_atomic(latest_path, (OUT_DIR / f"prices-{latest_day.isoformat()}.json").read_bytes())
                plan.latest = latest_path

            log(f"plan: {len(plan.daily)} dage at sende, {skipped} uændrede sprunget over")
            if partial_days:
                log(f"bemærk: ufuldstændige dage sendes alligevel og opdateres næste kørsel: "
                    f"{', '.join(d.isoformat() for d in partial_days)}")

            stats = {
                "days_planned": len(plan.daily),
                "days_skipped": skipped,
            }

            # --- upload ----------------------------------------------------
            if not plan.daily:
                log("intet nyt at sende — alt er allerede ude")
                db_finish_run(conn, run_id, "ok", **stats, bytes_uploaded=0)
                return 0

            total_bytes = sum(p.stat().st_size for p in plan.files())

            if args.dry_run:
                log(f"DRY RUN: ville sende {len(plan.files())} filer "
                    f"({total_bytes} bytes) til {REMOTE_ROOT}")
                for p in plan.files():
                    log(f"    {p.name}  ({p.stat().st_size} bytes)")
                db_finish_run(conn, run_id, "dry_run", **stats, bytes_uploaded=0)
                return 0
            #script_lines = [f"mkdir -p {REMOTE_ROOT}"]
            script_lines = []
            for p in plan.files():
                script_lines.append(f"put {p} -o {REMOTE_ROOT}/{p.name}")
            script = "\n".join(script_lines)
            run_sftp(script, env)

            # --- FØRST NU markeres noget som sendt -------------------------
            # Sker der noget mellem upload og denne skrivning, sender vi bare
            # de samme dage igen næste gang. Upload er idempotent, så det er
            # den rigtige side at fejle på.
            for day, path in plan.daily.items():
                payload = json.loads(path.read_text(encoding="utf-8"))
                digest = hashlib.sha256(
                    canonical_bytes(payload)
                ).hexdigest()
                record_pushed(conn, day, len(payload["hours"]), digest)

            log(f"uploadet {len(plan.daily)} dage ({total_bytes} bytes) til {REMOTE_ROOT}")
            db_finish_run(
                conn, run_id, "ok", **stats,
                days_uploaded=len(plan.daily),
                bytes_uploaded=total_bytes,
            )
            return 0

        except PushError as exc:
            log(f"FEJL: {exc}")
            db_finish_run(conn, run_id, "failed", **stats, error=str(exc))
            # Bevidst exit 0: et SFTP-nedbrud er forventet drift, ikke en fejl
            # der skal sende cron-mails hver time døgnet rundt. Fejlen står i
            # push_runs. Skal du have alarm, så læs tabellen.
            return 0
        except Exception as exc:                       # noqa: BLE001
            log(f"Uventet fejl: {exc!r}")
            db_finish_run(conn, run_id, "failed", **stats, error=repr(exc))
            return 1


if __name__ == "__main__":
    sys.exit(main())
