#!/usr/bin/env bash
#
# install-pricejob-units.sh — opretter/opdaterer systemd-units for prisjobbet.
#
# Idempotent: kan køres vilkårligt mange gange. Scriptet skriver kun en fil hvis
# indholdet er ændret, og genstarter kun en timer hvis planen faktisk er ny.
#
# Kør med sudo:   sudo ./install-pricejob-units.sh
# Tør kørsel:     sudo ./install-pricejob-units.sh --dry-run
#
# Units der oprettes/opdateres:
#   elpriser.timer              collect dagligt, tættere vindue i 13-timen
#   pricepush.timer             upload hvert 20. min, tættere vindue i 13-timen
#   pricepush-notify.timer      kontrol, forskudt 5 min efter push
#   pricepush-notify.service    selve notify-jobbet
#
# Services (elpriser.service, pricepush.service) røres IKKE — de eksisterer og
# har deres egen opsætning. Dette script ejer kun timerne plus notify-servicen.

set -euo pipefail

# ------------------------------------------------------------------ konfiguration
APP_DIR="${PRICEJOB_DIR:-/home/peterfausboll/code/pricejob}"
RUN_USER="${PRICEJOB_USER:-peterfausboll}"
STATE_DIR="${PRICEJOB_STATE_DIR:-/var/lib/pricejob}"
UNIT_DIR="/etc/systemd/system"
PYTHON="${PRICEJOB_PYTHON:-/usr/bin/python3}"

# Den tætte kadence omkring offentliggørelsen (day-ahead offentliggøres ~13:00).
# Push rammer 13:05, 13:10 ... 13:55; ellers hvert 20. minut.
PUSH_TIGHT="*-*-* 13:05/5"
PUSH_NORMAL="*:0/20"
# Notify ligger 5 minutter efter push, så den altid læser en afsluttet kørsel.
NOTIFY_TIGHT="*-*-* 13:10/5"
NOTIFY_NORMAL="*:10/20"

DRY_RUN=0
case "${1:-}" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
esac

# ------------------------------------------------------------------ farver + log
if [[ -t 1 ]]; then
    R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[1m'; N=$'\e[0m'
else
    R=''; G=''; Y=''; B=''; N=''
fi

say()  { printf '%s\n' "$*"; }
head_() { printf '\n%s%s%s\n' "$B" "$*" "$N"; }
fail() { printf '%sFEJL:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

# --------------------------------------------------------------- forudsætninger
[[ $EUID -eq 0 ]] || fail "skal køres med sudo (skriver til $UNIT_DIR)"
[[ -d "$APP_DIR" ]] || fail "APP_DIR findes ikke: $APP_DIR"
[[ -f "$APP_DIR/notify.py" ]] || fail "notify.py findes ikke i $APP_DIR"
[[ -f "$APP_DIR/.env" ]] || fail ".env findes ikke i $APP_DIR"
id "$RUN_USER" >/dev/null 2>&1 || fail "brugeren $RUN_USER findes ikke"
[[ -x "$PYTHON" ]] || fail "python-sti virker ikke: $PYTHON"

head_ "Forudsætninger OK"
say "  APP_DIR    $APP_DIR"
say "  RUN_USER   $RUN_USER"
say "  STATE_DIR  $STATE_DIR"
say "  PYTHON     $PYTHON"
(( DRY_RUN )) && say "  ${Y}DRY RUN — der skrives intet${N}"

# ------------------------------------------------- et unit-par: skriv hvis ændret
# Skriver $2 (indhold) til $UNIT_DIR/$1, men kun hvis indholdet er nyt.
# Returnerer 0 uanset, og sætter CHANGED=1 hvis filen blev skrevet.
CHANGED=0
write_unit() {
    local name=$1 content=$2 path="$UNIT_DIR/$1"
    CHANGED=0

    if [[ -f "$path" ]] && [[ "$(cat "$path")" == "$content" ]]; then
        say "  ${G}=${N} $name (uændret)"
        return 0
    fi

    CHANGED=1
    if (( DRY_RUN )); then
        say "  ${Y}+${N} $name (ville blive skrevet)"
        return 0
    fi

    # Skriv atomisk: temp-fil i samme filsystem, derefter mv.
    local tmp
    tmp="$(mktemp "$UNIT_DIR/.${name}.XXXXXX")"
    printf '%s\n' "$content" >"$tmp"
    chmod 0644 "$tmp"
    chown root:root "$tmp"
    mv -f "$tmp" "$path"
    say "  ${Y}+${N} $name (opdateret)"
}

# ================================================================ STATE-DIR
head_ "Tilstandsmappe"
if [[ -d "$STATE_DIR" ]]; then
    # Ret owner hvis den er forkert — ellers kan notify ikke skrive sin tilstand
    cur_owner="$(stat -c '%U:%G' "$STATE_DIR")"
    if [[ "$cur_owner" != "$RUN_USER:$RUN_USER" ]]; then
        (( DRY_RUN )) || chown "$RUN_USER:$RUN_USER" "$STATE_DIR"
        say "  ${Y}~${N} $STATE_DIR (ejer rettet: $cur_owner -> $RUN_USER:$RUN_USER)"
    else
        say "  ${G}=${N} $STATE_DIR (findes, ejer OK)"
    fi
else
    (( DRY_RUN )) || install -d -o "$RUN_USER" -g "$RUN_USER" -m 0755 "$STATE_DIR"
    say "  ${Y}+${N} $STATE_DIR (oprettet)"
fi

# ================================================================ NOTIFY-SERVICE
head_ "Units"
read -r -d '' NOTIFY_SERVICE <<EOF || true
[Unit]
Description=Notify on electricity price job problems
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
Environment=NOTIFY_STATE_FILE=$STATE_DIR/notify_state.json
ExecStart=$PYTHON $APP_DIR/notify.py
EOF
write_unit "pricepush-notify.service" "$NOTIFY_SERVICE"

# ================================================================ TIMERS
read -r -d '' NOTIFY_TIMER <<EOF || true
[Unit]
Description=Check electricity price job every 20 minutes, tighter 13-14

[Timer]
OnCalendar=$NOTIFY_TIGHT
OnCalendar=$NOTIFY_NORMAL
Persistent=true
RandomizedDelaySec=30
Unit=pricepush-notify.service

[Install]
WantedBy=timers.target
EOF
write_unit "pricepush-notify.timer" "$NOTIFY_TIMER"

read -r -d '' PUSH_TIMER <<EOF || true
[Unit]
Description=Run price push every 20 minutes, tighter 13-14

[Timer]
OnCalendar=$PUSH_TIGHT
OnCalendar=$PUSH_NORMAL
Persistent=true
RandomizedDelaySec=30
Unit=pricepush.service

[Install]
WantedBy=timers.target
EOF
write_unit "pricepush.timer" "$PUSH_TIMER"

read -r -d '' COLLECT_TIMER <<EOF || true
[Unit]
Description=Collect electricity prices daily after publication

[Timer]
OnCalendar=*-*-* 13:20:00
Persistent=true
RandomizedDelaySec=60
Unit=elpriser.service

[Install]
WantedBy=timers.target
EOF
write_unit "elpriser.timer" "$COLLECT_TIMER"

# ================================================================ AKTIVERING
head_ "Aktivering"

if (( DRY_RUN )); then
    say "  (dry run — ingen daemon-reload, enable eller genstart)"
else
    systemctl daemon-reload
    say "  ${G}${N} daemon-reload"

    # enable er idempotent: systemctl siger til hvis linket allerede findes
    for unit in elpriser.timer pricepush.timer pricepush-notify.timer; do
        if systemctl is-enabled "$unit" >/dev/null 2>&1; then
            say "  ${G}=${N} $unit (allerede enabled)"
        else
            systemctl enable "$unit" >/dev/null 2>&1
            say "  ${Y}+${N} $unit (enabled)"
        fi
    done

    # Start/opdater kun timere hvis en fil faktisk blev ændret.
    # restart er idempotent og rører ikke allerede korrekte planer.
    for unit in elpriser.timer pricepush.timer pricepush-notify.timer; do
        systemctl restart "$unit"
    done
    say "  ${G}${N} timere genstartet (planen er genlæst)"
fi

# ================================================================ VERIFIKATION
head_ "Status"
if (( DRY_RUN )); then
    say "  (springes over i dry run)"
else
    # list-timers' kolonner er svære at parse; systemctl show giver felterne enkeltvis
    for unit in elpriser.timer pricepush.timer pricepush-notify.timer; do
        next="$(systemctl show -p NextElapseUSecRealtime --value "$unit" 2>/dev/null)"
        last="$(systemctl show -p LastTriggerUSec --value "$unit" 2>/dev/null)"
        [[ -z "$next" || "$next" == "n/a" ]] && next="${R}(ingen planlagt)${N}"
        [[ -z "$last" || "$last" == "n/a" ]] && last="(aldrig)"
        printf '  %-26s næste: %-30s sidst: %s\n' "$unit" "$next" "$last"
    done

    echo
    say "Dagens plan for pricepush:"
    systemctl list-timers pricepush.timer --no-pager 2>/dev/null | sed -n '1,3p' | sed 's/^/  /'

    # Tør kørsel af notify, så en DSN- eller sti-fejl viser sig nu og ikke om 20 min
    echo
    say "Tør kørsel af notify.py som $RUN_USER:"
    if sudo -u "$RUN_USER" "$PYTHON" "$APP_DIR/notify.py"; then
        say "  ${G}OK${N} — notify kan forbinde og skrive tilstand"
    else
        say "  ${R}notify fejlede${N} — se output ovenfor"
    fi
fi

head_ "Færdig"
say "  Verificér med:  systemctl list-timers --all | grep -E 'pricepush|elpriser'"
say "  Logs:           journalctl -u pricepush.service -n 50 --no-pager"
