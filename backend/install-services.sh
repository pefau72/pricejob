#!/usr/bin/env bash
#
# install-units.sh — opretter/opdaterer ALLE systemd-units for datajobbet.
#
# Dette script ejer HELE unit-sættet: fem services og fem timere, for både
# pris- og vejrjobbet. Tidligere rørte det ikke services ("de har deres egen
# opsætning"), men efter en flytning af backend-mappen stod de to gamle
# services tilbage med døde stier, og scriptet kunne ikke se det. Døde units
# er værre end overskrevne units, så nu ejer scriptet alt.
#
# Idempotent: kan køres vilkårligt mange gange. En fil skrives kun hvis
# indholdet er ændret, og en timer genstartes kun hvis planen faktisk er ny.
#
# Units der oprettes/opdateres:
#
#   elpriser.service            collect priser
#   pricepush.service           upload priser
#   weathercollect.service      collect DMI-vejr
#   weatherpush.service         upload vejr (kun hvis ændret)
#   jobs-notify.service    kontrol af BEGGE jobs
#
#   elpriser.timer              dagligt, efter offentliggørelsen
#   pricepush.timer             hvert 20. min, tættere 13-14
#   weathercollect.timer        dagligt kl. 03:17
#   weatherpush.timer           dagligt kl. 03:40 (efter collect)
#   jobs-notify.timer      5 min efter push
#
# Kør:
#   sudo ./install-units.sh --dry-run     vis hvad der ville ske
#   sudo ./install-units.sh               skriv og aktivér

set -euo pipefail

# ------------------------------------------------------------------ konfiguration
#
# Alle stier kan overstyres fra miljøet, men defaultene er de faktiske.
# Backend-mappen hedder 'datajob' (omdøbt fra 'pricejob').

APP_DIR="${DATAJOB_DIR:-/home/peterfausboll/code/datajob/backend}"
RUN_USER="${DATAJOB_USER:-peterfausboll}"
STATE_DIR="${DATAJOB_STATE_DIR:-/var/lib/datajob}"
UNIT_DIR="/etc/systemd/system"
PYTHON="${DATAJOB_PYTHON:-/usr/bin/python3}"

# Den tætte kadence omkring offentliggørelsen (day-ahead offentliggøres ~13:00).
# Push rammer 13:05, 13:10 ... 13:55; ellers hvert 20. minut.
PUSH_TIGHT="*-*-* 13:05/5"
PUSH_NORMAL="*:0/20"
# Notify ligger 5 minutter efter push, så den altid læser en afsluttet kørsel.
NOTIFY_TIGHT="*-*-* 13:10/5"
NOTIFY_NORMAL="*:10/20"

# Vejrjobbet kører én gang i døgnet. Push skal ligge EFTER collect, ellers
# uploades gårsdagens data. content_hash i pushed_state gør at en uændret
# normal-fil ikke sendes igen.
WEATHER_COLLECT_AT="*-*-* 03:17:00"
WEATHER_PUSH_AT="*-*-* 03:40:00"

DRY_RUN=0
case "${1:-}" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
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
[[ -f "$APP_DIR/.env" ]] || fail ".env findes ikke i $APP_DIR"
id "$RUN_USER" >/dev/null 2>&1 || fail "brugeren $RUN_USER findes ikke"
[[ -x "$PYTHON" ]] || fail "python-sti virker ikke: $PYTHON"

# De scripts units peger på SKAL findes. Ellers fejler timeren først kl. 03:17
# om natten, langt fra hvor fejlen blev lavet.
for script in price_job.py push_prices.py weather_job.py push_weather.py notify.py; do
    [[ -f "$APP_DIR/$script" ]] || fail "$script findes ikke i $APP_DIR"
done

head_ "Forudsætninger OK"
say "  APP_DIR    $APP_DIR"
say "  RUN_USER   $RUN_USER"
say "  STATE_DIR  $STATE_DIR"
say "  PYTHON     $PYTHON"
(( DRY_RUN )) && say "  ${Y}DRY RUN — der skrives intet${N}"

# ------------------------------------------------- et unit-par: skriv hvis ændret
# Skriver $2 (indhold) til $UNIT_DIR/$1, men kun hvis indholdet er nyt.
# Eksisterende filer med andet indhold vises som en DIFF først, så en manuel
# tilføjelse i en gammel unit ikke forsvinder ubemærket.
CHANGED=0
write_unit() {
    local name=$1 content=$2 path="$UNIT_DIR/$1"
    CHANGED=0

    if [[ -f "$path" ]] && [[ "$(cat "$path")" == "$content" ]]; then
        say "  ${G}=${N} $name (uændret)"
        return 0
    fi

    CHANGED=1

    # Findes filen i forvejen med ANDET indhold? Vis hvad der ændrer sig.
    if [[ -f "$path" ]] && (( ! DRY_RUN )); then
        local diffout
        diffout="$(diff -u "$path" <(printf '%s\n' "$content") || true)"
        if [[ -n "$diffout" ]]; then
            say "  ${Y}~${N} $name — ændringer:"
            printf '%s\n' "$diffout" | sed 's/^/      /'
        fi
    fi

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

# ================================================================ OPRYDNING
#
# Gamle units fra foer omdoebningen (pricejob -> datajob). De peger paa
# stier der ikke findes mere.
#
# VIGTIGT: denne sektion skal koere FOER Services og Timere. Laa den efter,
# slettede den de units scriptet lige havde skrevet — og aktiveringen
# fejlede med "Unit not found", fordi servicen var vaek. Det skete:
# weatherpush.timer blev enabled, men weatherpush.service var slettet i
# samme koersel.

LEGACY_UNITS=(
    "weather-job.timer"
    "weather-job.service"
    # notify-unit'en blev oprettet som 'pricepush-notify', dengang der kun var
    # ét job. Den overvåger BEGGE jobs, så navnet løj — omdøbt til 'jobs-notify'.
    # De gamle fjernes her, så omdøbningen klarer sig i én kørsel.
    "pricepush-notify.timer"
    "pricepush-notify.service"
)

head_ "Oprydning af gamle units"
FOUND_LEGACY=0
for unit in "${LEGACY_UNITS[@]}"; do
    if [[ -e "$UNIT_DIR/$unit" ]]; then
        FOUND_LEGACY=1
        if (( DRY_RUN )); then
            say "  ${Y}x${N} $unit (ville blive fjernet)"
        else
            systemctl disable --now "$unit" >/dev/null 2>&1 || true
            rm -f "$UNIT_DIR/$unit"
            say "  ${Y}x${N} $unit (fjernet)"
        fi
    fi
done
(( FOUND_LEGACY )) || say "  ${G}=${N} ingen gamle units fundet"

# ================================================================ SERVICES
head_ "Services"

read -r -d '' COLLECT_PRICES_SERVICE <<EOF || true
[Unit]
Description=Collect electricity prices (Energidataservice)
After=network-online.target postgresql@14-main.service
Wants=network-online.target
Requires=postgresql@14-main.service

[Service]
Type=oneshot
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$PYTHON $APP_DIR/price_job.py
EOF
write_unit "elpriser.service" "$COLLECT_PRICES_SERVICE"

read -r -d '' PUSH_PRICES_SERVICE <<EOF || true
[Unit]
Description=Push electricity prices to netsite via SFTP
After=network-online.target postgresql@14-main.service
Wants=network-online.target
Requires=postgresql@14-main.service

[Service]
Type=oneshot
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$PYTHON $APP_DIR/push_prices.py
# Bevidst ikke SuccessExitStatus her: push_prices.py returnerer selv 0 ved
# netsite-nedetid. Se kommentaren i scriptet.
EOF
write_unit "pricepush.service" "$PUSH_PRICES_SERVICE"

read -r -d '' COLLECT_WEATHER_SERVICE <<EOF || true
[Unit]
Description=Collect DMI weather data
After=network-online.target postgresql@14-main.service
Wants=network-online.target
Requires=postgresql@14-main.service

[Service]
Type=oneshot
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$PYTHON $APP_DIR/weather_job.py
# DMI-nedetid er forventet drift: vandmærket står stille, og næste kørsel
# dækker hullet. weather-job.py returnerer 0 efter at have logget fejlen,
# så systemd ikke melder "failed" hver gang DMI har et hikke.
SuccessExitStatus=0
TimeoutStartSec=5min
EOF
write_unit "weathercollect.service" "$COLLECT_WEATHER_SERVICE"

read -r -d '' PUSH_WEATHER_SERVICE <<EOF || true
[Unit]
Description=Push weather data to netsite via SFTP
After=network-online.target postgresql@14-main.service
Wants=network-online.target
Requires=postgresql@14-main.service

[Service]
Type=oneshot
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$PYTHON $APP_DIR/push_weather.py
SuccessExitStatus=0
TimeoutStartSec=5min
EOF
write_unit "weatherpush.service" "$PUSH_WEATHER_SERVICE"

read -r -d '' NOTIFY_SERVICE <<EOF || true
[Unit]
Description=Notify on data job problems (prices and weather)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
Environment=NOTIFY_STATE_FILE=$STATE_DIR/notify_state.json
# notify.py skriver notify_state.json i STATE_DIR. Giv retten eksplicit —
# ellers afhænger det af at ProtectSystem ikke er sat, hvilket er implicit.
ReadWritePaths=$STATE_DIR
ExecStart=$PYTHON $APP_DIR/notify.py
EOF
write_unit "jobs-notify.service" "$NOTIFY_SERVICE"

# ================================================================ TIMERS
head_ "Timere"

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

read -r -d '' NOTIFY_TIMER <<EOF || true
[Unit]
Description=Check data job every 20 minutes, tighter 13-14

[Timer]
OnCalendar=$NOTIFY_TIGHT
OnCalendar=$NOTIFY_NORMAL
Persistent=true
RandomizedDelaySec=30
Unit=jobs-notify.service

[Install]
WantedBy=timers.target
EOF
write_unit "jobs-notify.timer" "$NOTIFY_TIMER"

read -r -d '' WEATHER_COLLECT_TIMER <<EOF || true
[Unit]
Description=Collect DMI weather data daily

[Timer]
OnCalendar=$WEATHER_COLLECT_AT
Persistent=true
RandomizedDelaySec=180
Unit=weathercollect.service

[Install]
WantedBy=timers.target
EOF
write_unit "weathercollect.timer" "$WEATHER_COLLECT_TIMER"

read -r -d '' WEATHER_PUSH_TIMER <<EOF || true
[Unit]
Description=Push weather data to netsite (only when changed)

[Timer]
OnCalendar=$WEATHER_PUSH_AT
Persistent=true
RandomizedDelaySec=120
Unit=weatherpush.service

[Install]
WantedBy=timers.target
EOF
write_unit "weatherpush.timer" "$WEATHER_PUSH_TIMER"

# ================================================================ AKTIVERING
head_ "Aktivering"

ALL_TIMERS="elpriser.timer pricepush.timer jobs-notify.timer weathercollect.timer weatherpush.timer"

if (( DRY_RUN )); then
    say "  (dry run — ingen daemon-reload, enable eller genstart)"
else
    systemctl daemon-reload
    say "  ${G}${N} daemon-reload"

    # enable er idempotent: systemctl siger til hvis linket allerede findes
    for unit in $ALL_TIMERS; do
        if systemctl is-enabled "$unit" >/dev/null 2>&1; then
            say "  ${G}=${N} $unit (allerede enabled)"
        elif systemctl enable "$unit" >/dev/null 2>&1; then
            say "  ${Y}+${N} $unit (enabled)"
        else
            say "  ${R}x${N} $unit — kunne IKKE enables (mangler dens .service?)"
        fi
    done

    # restart er idempotent og rører ikke allerede korrekte planer
    for unit in $ALL_TIMERS; do
        if systemctl restart "$unit" 2>/dev/null; then
            say "  ${G}=${N} $unit (genstartet)"
        else
            say "  ${R}x${N} $unit — kunne IKKE genstartes"
        fi
    done
fi

# ================================================================ VERIFIKATION
head_ "Status"
if (( DRY_RUN )); then
    say "  (springes over i dry run)"
else
    # list-timers' kolonner er svære at parse; systemctl show giver felterne enkeltvis
    for unit in $ALL_TIMERS; do
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
say "  Verificér med:  systemctl list-timers --all | grep -E 'pricepush|elpriser|weather'"
say "  Logs:           journalctl -u pricepush.service -n 50 --no-pager"
say "                  journalctl -u weathercollect.service -n 50 --no-pager"
