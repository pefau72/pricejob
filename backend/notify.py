#!/usr/bin/env python3
"""
notify.py — sender mail når push-jobbet ikke har det godt.

Dette er BEVIDST ikke en del af push_prices.py. Grunden: hvis mail-afsendelsen
fejler (SMTP nede, password udløbet), skal det ikke kunne vælte selve
datapushet. Jobbet skal pushe priser uanset om der kan sendes besked om det.

Modellen er derfor:
    push_prices.py  ->  skriver status i push_runs  ->  notify.py læser og varsler

Kør fra cron efter push-jobbet:
    5,25,45 * * * * cd /home/peterfausboll/code/pricejob && python3 notify.py

Varsler (se CHECKS nedenfor):
  - fejlede kørsler
  - ingen vellykket kørsel i mere end MAX_SILENCE_HOURS
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
ReadWritePaths="/home/peterfausboll/code/pricejob"
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

# Hvor længe uden en vellykket kørsel før vi siger til. Jobbet kører hvert
# 20. minut, så 3 timer er rigeligt til at fange "cron er død" / "serveren er nede".
MAX_SILENCE_HOURS = int(_env("NOTIFY_MAX_SILENCE_HOURS", "3"))

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

def check(conn, state: dict) -> list[dict]:
    """
    Returnerer en liste af aktive problemer. Hvert problem har en 'key' der
    bruges til at huske hvad der allerede er varslet om.
    """
    problems: list[dict] = []
    now = datetime.now(timezone.utc)

    with conn.cursor() as cur:
        # --- seneste kørsler ------------------------------------------------
        cur.execute("""
            SELECT run_id, started_at, finished_at, status,
                   days_planned, days_uploaded, days_skipped, error
              FROM push_runs
             ORDER BY run_id DESC
             LIMIT 20
        """)
        runs = cur.fetchall()

        cur.execute("""
            SELECT max(finished_at)
              FROM push_runs
             WHERE status = 'ok' AND days_uploaded > 0
        """)
        last_ok_any = cur.fetchone()[0]

        cur.execute("""
            SELECT max(finished_at)
              FROM push_runs
             WHERE status = 'ok'
        """)
        last_ok = cur.fetchone()[0]

        # --- ufuldstændige dage --------------------------------------------
        # En dag er mistænkelig hvis den er i fortiden og har færre punkter end
        # et komplet døgn. Vi kender ikke DST-dage her, så vi bruger 96 som
        # nominelt og tolererer under.
        cur.execute("""
            SELECT day, rows_pushed, pushed_at
              FROM pushed_state
             WHERE day < CURRENT_DATE - INTERVAL '1 day'
               AND rows_pushed < 92
             ORDER BY day DESC
             LIMIT 5
        """)
        short_days = cur.fetchall()

    # 1) Ingen vellykket kørsel i lang tid
    if last_ok is not None:
        quiet_for = now - last_ok
        if quiet_for > timedelta(hours=MAX_SILENCE_HOURS):
            problems.append({
                "key": "silence",
                "subject": f"[elpriser] ingen vellykket push-kørsel i {quiet_for.total_seconds()/3600:.1f} timer",
                "body": (
                    f"Seneste vellykkede kørsel: {last_ok.isoformat()}\n"
                    f"Nuværende tid:              {now.isoformat()}\n"
                    f"Jobbet skulle køre hvert 20. minut.\n\n"
                    "Tjek:\n"
                    "  systemctl status cron  (eller crontab -l)\n"
                    "  python3 push_prices.py --dry-run\n"
                    "  journalctl -u pricepush.service -n 50 --no-pager"
                ),
            })
    elif runs:
        # Der ER kørsler, men ingen er lykkedes nogensinde
        problems.append({
            "key": "never_ok",
            "subject": "[elpriser] ingen push-kørsel er nogensinde lykkedes",
            "body": "Der findes rækker i push_runs, men ingen med status 'ok'.\n\n"
                    + "\n".join(f"  {r[0]} {r[1]} {r[3]} {r[7] or ''}" for r in runs[:5]),
        })

    # 2) Fejl i træk
    streak = 0
    for r in runs:                       # nyeste først
        if r[3] == "failed":
            streak += 1
        elif r[3] in ("ok", "dry_run"):
            break
    if streak >= FAIL_STREAK_THRESHOLD:
        latest = runs[0]
        problems.append({
            "key": "fail_streak",
            "subject": f"[elpriser] {streak} fejlede push-kørsler i træk",
            "body": (
                f"Nyeste fejl (kørsel {latest[0]}, {latest[1]}):\n\n"
                f"{latest[7] or '(ingen fejltekst)'}\n\n"
                "Er fejlen en SFTP-fejl, er det oftest netsite der er nede eller\n"
                "har skiftet host-nøgle. Ingén af delene kræver handling herfra —\n"
                "jobbet henter efterslæbet selv når forbindelsen virker igen.\n\n"
                "Er fejlen en DB-fejl, skal den ses på med det samme."
            ),
        })

    # 3) Dage der bliver ved med at være ufuldstændige
    for day, rows, pushed_at in short_days:
        age = now - pushed_at
        if age > timedelta(hours=STALE_DAY_HOURS):
            problems.append({
                "key": f"short_day:{day}",
                "subject": f"[elpriser] dagen {day} er stadig ufuldstændig ({rows} punkter)",
                "body": (
                    f"Dagen {day} blev sidst pushet {pushed_at.isoformat()} "
                    f"({age.total_seconds()/3600:.0f} timer siden) med kun {rows} punkter.\n"
                    "Et komplet døgn har 96 (92/100 på DST-dage).\n\n"
                    "Det tyder på at collect-jobbet (job.py) ikke har hentet hele døgnet.\n"
                    "Tjek:\n"
                    "  SELECT job, watermark_dk, last_success_at FROM sync_state;\n"
                    "  SELECT run_id, status, error FROM sync_runs ORDER BY run_id DESC LIMIT 10;"
                ),
            })

    return problems


# ------------------------------------------------------------------ main

def main() -> int:
    load_env_file(HERE / ".env")

    state = load_state()
    active_keys: set[str] = set()

    try:
        with psycopg.connect(get_dsn()) as conn:
            problems = check(conn, state)
    except Exception as exc:                      # noqa: BLE001
        # Kan vi ikke engang læse databasen, er det selv et problem — og et
        # alvorligt et, for så logger push-jobbet heller ikke noget.
        problems = [{
            "key": "db_unreachable",
            "subject": "[elpriser] notify.py kan ikke læse databasen",
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
        state.setdefault("notified", {})[p["key"]] = datetime.now(timezone.utc).isoformat()

    # Ryd tilstand for problemer der ikke længere er aktive, så en ny fejl
    # kan varsle igen senere.
    for k in list(state.get("notified", {})):
        if k not in active_keys:
            del state["notified"][k]

    save_state(state)
    print(f"tjek færdigt: {len(problems)} aktive problem(er), {sent} mail(s) sendt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
