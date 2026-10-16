#!/usr/bin/env python3
"""
notify.py — sender mail når push-jobbet ikke har det godt.

Dette er BEVIDST ikke en del af push_prices.py. Grunden: hvis mail-afsendelsen
fejler (SMTP nede, password udløbet), skal det ikke kunne vælte selve
datapushet. Jobbet skal pushe priser uanset om der kan sendes besked om det.

Modellen er derfor:
    push_prices.py  ->  skriver status i push_runs  ->  notify.py læser og varsler

Kør fra cron efter push-jobbet:
    5,25,45 * * * * cd /home/peterfausboll/code/datajob && python3 notify.py
Varsler (se CHECKS nedenfor):
  - fejlede kørsler
  - ingen vellykket kørsel i mere end jobbets silence_hours
  - begge jobs overvåges hver for sig (push_prices, push_weather)
  - netsite nede mange gange i træk
  - dage der bliver ved med at være ufuldstændige

Tilstand (hvad der allerede er varslet om) gemmes i notify_state.json, så du
får ÉN mail pr. problem — ikke én hver time. Når problemet er væk, ryddes
tilstanden, og en ny fejl kan varsle igen.
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import psycopg

HERE = Path(__file__).parent
STATE_FILE = Path(os.environ.get("NOTIFY_STATE_FILE", HERE / "notify_state.json"))
# systemd-unit'en skal have ReadWritePaths for at kunne skrive notify_state.json.
# ------------------------------------------------------------------ konfiguration

def _env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


SMTP_HOST = _env("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(_env("SMTP_PORT", "587"))
SMTP_USER = _env("SMTP_USER")
SMTP_PASSWORD = _env("SMTP_PASSWORD")
SMTP_STARTTLS = _env("SMTP_STARTTLS", "1") not in ("0", "false", "no")

MAIL_FROM = _env("MAIL_FROM", SMTP_USER)
MAIL_TO = _env("MAIL_TO")                       # kommasepareret for flere

# JOBS OG DERES KADENCE.
#
# push_runs deles af to jobs med vidt forskellig kadence: priser hvert 20.
# minut, vejr én gang i døgnet. Et fælles MAX_SILENCE_HOURS ville derfor
# være forkert for mindst ét af dem — 2 timer er rigeligt for priser og alt
# for stramt for vejr.
#
# Hvert job overvåges for sig, og alarm-nøglerne prefikses med job-navnet,
# saa de ikke overskriver hinandens tilstand i notify_state.json.
#
#   silence_hours  hvor længe uden en vellykket kørsel før vi siger til.
#                  Vejr kører kl. 03:17 med Persistent=true, saa en genstart
#                  om natten udskyder kørslen til opstart — 30 timer giver
#                  plads til det uden falske alarmer.
#   interval_hint  ren tekst til mailens "tjek selv"-afsnit.
JOBS = {
    "push_prices": {
        "label": "priser",
        "silence_hours": 2,
        "interval_hint": "hvert 20. minut",
    },
    "push_weather": {
        "label": "vejr",
        "silence_hours": 30,
        "interval_hint": "én gang i døgnet (kl. 03:17)",
    },
}

# Antal fejlede kørsler i træk før vi kalder det "netsite er nede" snarere end
# "en enkelt kørsel fejlede".
FAIL_STREAK_THRESHOLD = int(_env("NOTIFY_FAIL_STREAK", "3"))

# En dag der har været ufuldstændig så længe, er ikke bare "ikke offentliggjort
# endnu" — så fejler collect-jobbet sandsynligvis.
STALE_DAY_HOURS = int(_env("NOTIFY_STALE_DAY_HOURS", "48"))

# ------------------------------------------------------------------ env-indlæsning

# Samme parser som push_prices.py, så de to filer læser .env ens. Vi duplikerer
# den bevidst i stedet for at importere: notify.py skal kunne køre selv hvis
# push_prices.py er midt i en redigering, og en import ville trække psycopg-
# forbindelser og alt muligt andet med.

def _parse_env_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].lstrip()
    if "=" not in line:
        return None
    key, _, raw = line.partition("=")
    key, raw = key.strip(), raw.strip()
    if not key:
        return None
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        quote = raw[0]
        value = raw[1:-1]
        if quote == '"':
            value = (value.replace("\\n", "\n").replace("\\t", "\t")
                          .replace('\\"', '"').replace("\\\\", "\\"))
    else:
        h = raw.find("#")
        if h != -1:
            raw = raw[:h]
        value = raw.rstrip()
    return key, value


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(line)
        if parsed is None:
            continue
        k, v = parsed
        if k not in os.environ:
            os.environ[k] = v


# ------------------------------------------------------------------ DB

def get_dsn() -> str:
    if dsn := os.environ.get("DSN"):
        if "://" not in dsn:
            return dsn
        from urllib.parse import urlparse, unquote
        u = urlparse(dsn)
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

    missing = [k for k in ("PGHOST", "PGUSER", "PGDATABASE") if not os.environ.get(k)]
    if missing:
        raise RuntimeError("mangler DB-konfiguration: " + ", ".join(missing))
    parts = [f"host={os.environ['PGHOST']}",
             f"port={os.environ.get('PGPORT', '5432')}",
             f"dbname={os.environ['PGDATABASE']}",
             f"user={os.environ['PGUSER']}"]
    if pw := os.environ.get("PGPASSWORD"):
        parts.append(f"password={pw}")
    return " ".join(parts)


# ------------------------------------------------------------------ tilstand

def load_state() -> dict:
    if STATE_FILE.is_file():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except OSError as exc:
        print(f"ADVARSEL: kunne ikke gemme tilstand: {exc!r} — næste kørsel sender igen",
              file=sys.stderr)

# ------------------------------------------------------------------ mail

def send_mail(subject: str, body: str) -> None:
    if not (SMTP_USER and SMTP_PASSWORD and MAIL_TO):
        print(f"   (mail ikke konfigureret — ville sende: {subject})")
        return

    msg = EmailMessage()
    msg["From"] = MAIL_FROM or SMTP_USER
    msg["To"] = MAIL_TO
    msg["Subject"] = subject
    msg.set_content(body)

    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.ehlo()
        if SMTP_STARTTLS:
            s.starttls(context=ctx)
            s.ehlo()
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)


# ------------------------------------------------------------------ tjek

# ------------------------------------------------------------------ tjek

def check_job(conn, job: str, cfg: dict, now: datetime) -> list[dict]:
    """
    Checks for ÉT job.

    Alle tre push_runs-queries filtrerer på job, saa de to jobs ikke blandes.
    Alarm-nøglerne prefikses med job-navnet: uden det ville en vejralarm
    blive slettet fra notify_state.json i samme sekund prisjobbet kom i
    orden igen, fordi nøglen var den samme.
    """
    problems: list[dict] = []
    label = cfg["label"]

    with conn.cursor() as cur:
        cur.execute("""
            SELECT run_id, started_at, finished_at, status,
                   days_planned, days_uploaded, days_skipped, error
              FROM push_runs
             WHERE job = %s
             ORDER BY run_id DESC
             LIMIT 20
        """, (job,))
        runs = cur.fetchall()

        cur.execute("""
            SELECT max(finished_at)
              FROM push_runs
             WHERE job = %s AND status = 'ok'
        """, (job,))
        last_ok = cur.fetchone()[0]

    # 1) Ingen vellykket kørsel i lang tid
    if last_ok is not None:
        quiet_for = now - last_ok
        if quiet_for > timedelta(hours=cfg["silence_hours"]):
            problems.append({
                "key": f"{job}_silence",
                "subject": f"[pricejob] ingen vellykket {label}-push i "
                           f"{quiet_for.total_seconds() / 3600:.1f} timer",
                "body": (
                    f"Job:                       {job}\n"
                    f"Seneste vellykkede kørsel: {last_ok.isoformat()}\n"
                    f"Nuværende tid:             {now.isoformat()}\n"
                    f"Jobbet skulle køre {cfg['interval_hint']}.\n\n"
                    "Tjek:\n"
                    "  systemctl list-timers 'weather-*'\n"
                    "  systemctl status cron  (eller crontab -l)\n"
                    f"  SELECT run_id, status, error FROM push_runs\n"
                    f"   WHERE job = '{job}' ORDER BY run_id DESC LIMIT 10;\n"
                ),
            })
    elif runs:
        problems.append({
            "key": f"{job}_never_ok",
            "subject": f"[pricejob] ingen {label}-push er nogensinde lykkedes",
            "body": f"Der findes rækker i push_runs for {job}, men ingen med "
                    f"status 'ok'.\n\n"
                    + "\n".join(f"  {r[0]} {r[1]} {r[3]} {r[7] or ''}"
                                for r in runs[:5]),
        })

    # 2) Fejl i træk — kun dette jobs egne kørsler
    streak = 0
    for r in runs:                       # nyeste først
        if r[3] == "failed":
            streak += 1
        elif r[3] in ("ok", "dry_run"):
            break
    if streak >= FAIL_STREAK_THRESHOLD:
        latest = runs[0]
        problems.append({
            "key": f"{job}_fail_streak",
            "subject": f"[pricejob] {streak} fejlede {label}-push-kørsler i træk",
            "body": (
                f"Job: {job}\n"
                f"Nyeste fejl (kørsel {latest[0]}, {latest[1]}):\n\n"
                f"{latest[7] or '(ingen fejltekst)'}\n\n"
                "Fejler SFTP, henter jobbet hele efterslæbet selv når "
                "forbindelsen virker igen — intet går tabt.\n"
            ),
        })

    return problems


def check_short_days(conn, now: datetime) -> list[dict]:
    """
    Dage i pushed_state der har været ufuldstændige længe.

    Kun prisjobbet: det skriver DAGE i pushed_state med day sat. Vejrjobbet
    skriver rækker hvor day er NULL og rows_pushed er et andet slags tal
    (antal maalinger, ikke antal punkter i et doegn). Uden 'day IS NOT NULL'
    ville vejr-raekker blive vurderet mod 92-punktsgraensen og give falske
    alarmer.
    """
    problems: list[dict] = []
    with conn.cursor() as cur:
        cur.execute("""
            SELECT day, rows_pushed, pushed_at
              FROM pushed_state
             WHERE day IS NOT NULL
               AND day < CURRENT_DATE - INTERVAL '1 day'
               AND rows_pushed < 92
               AND pushed_at < now() - make_interval(hours => %s)
             ORDER BY day DESC
             LIMIT 5
        """, (STALE_DAY_HOURS,))
        short_days = cur.fetchall()

    if short_days:
        days_txt = "\n".join(
            f"  {d[0]}: {d[1]} punkter (pushet {d[2].isoformat()})"
            for d in short_days
        )
        problems.append({
            "key": "short_days",
            "subject": f"[pricejob] {len(short_days)} dage er stadig ufuldstændige",
            "body": (
                f"Ingen af disse dage har fået fulde 96 punkter:\n\n{days_txt}\n\n"
                "Det tyder på at collect-jobbet (job.py) ikke har hentet "
                "hele døgnet.\n\n"
                "Tjek:\n"
                "  SELECT job, watermark_dk, last_success_at FROM sync_state;\n"
                "  SELECT run_id, status, error FROM sync_runs "
                "ORDER BY run_id DESC LIMIT 10;\n"
            ),
        })

    return problems


def main() -> int:
    load_env_file(HERE / ".env")

    state = load_state()
    active_keys: set[str] = set()
    now = datetime.now(timezone.utc)

    try:
        with psycopg.connect(get_dsn()) as conn:
            problems: list[dict] = []
            # Hvert job overvåges for sig — se JOBS ovenfor.
            for job, cfg in JOBS.items():
                problems += check_job(conn, job, cfg, now)
            # Dagschecken hører til prisjobbet alene.
            problems += check_short_days(conn, now)
    except Exception as exc:                      # noqa: BLE001
        # Kan vi ikke engang læse databasen, er det selv et problem — og et
        # alvorligt et, for så logger push-jobbet heller ikke noget.
        problems = [{
            "key": "db_unreachable",
            "subject": "[pricejob] notify.py kan ikke læse databasen",
            "body": f"Fejl ved forbindelse til Postgres:\n\n{exc!r}\n\n"
                    "Hvis DB er nede, kan push-jobbet heller ikke køre.",
        }]

    sent = 0
    for p in problems:
        active_keys.add(p["key"])
        if state.get("notified", {}).get(p["key"]):
            continue                              # allerede varslet — ikke spam
        try:
            send_mail(p["subject"], p["body"])
            print(f"sendte: {p['subject']}")
            sent += 1
        except Exception as exc:                  # noqa: BLE001
            print(f"kunne ikke sende mail: {exc!r}", file=sys.stderr)
            continue                              # ikke markeret -> forsøges igen
        state.setdefault("notified", {})[p["key"]] = now.isoformat()

    # Ryd tilstand for problemer der ikke længere er aktive, så en ny fejl
    # kan varsle igen senere.
    for k in list(state.get("notified", {})):
        if k not in active_keys:
            del state["notified"][k]

    save_state(state)
    if not problems:
        print("alt i orden — ingen problemer")
    else:
        print(f"{len(problems)} problem(er), {sent} mail(s) sendt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
