#!/usr/bin/env bash
#
# pricejob-status.sh — statusoversigt for prisjobbet på ubserv.
#
# Kør:  ./pricejob-status.sh            (kort oversigt)
#       ./pricejob-status.sh -v         (med de sidste loglinjer)
#       ./pricejob-status.sh --json     (maskinlæsbar, til overvågning)
#
# Scriptet læser kun. Det starter, stopper eller ændrer intet.

set -uo pipefail

APP_DIR="${PRICEJOB_DIR:-$HOME/code/pricejob}"
VERBOSE=0
JSON=0
case "${1:-}" in
    -v|--verbose) VERBOSE=1 ;;
    --json)       JSON=1 ;;
    -h|--help)    sed -n '2,10p' "$0"; exit 0 ;;
esac

# ------------------------------------------------------------------ farver
if [[ -t 1 && $JSON -eq 0 ]]; then
    R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[1m'; D=$'\e[2m'; N=$'\e[0m'
else
    R=''; G=''; Y=''; B=''; D=''; N=''
fi

ok()   { printf '%s✔%s' "$G" "$N"; }
bad()  { printf '%s✘%s' "$R" "$N"; }
warn() { printf '%s!%s' "$Y" "$N"; }

# Problemtæller, så afsluttende exit-kode kan sættes
PROBLEMS=0
note_problem() { PROBLEMS=$((PROBLEMS + 1)); }

section() { [[ $JSON -eq 1 ]] || printf '\n%s%s%s\n' "$B" "$1" "$N"; }

# Hjælper: er en unit enabled?
is_enabled() { [[ "$(systemctl is-enabled "$1" 2>/dev/null)" == "enabled" ]]; }

# Hjælper: hvornår kørte unit'en sidst, som unix-tid (0 hvis aldrig)
last_run_epoch() {
    local ts
    ts=$(systemctl show -p ExecMainStartTimestamp --value "$1" 2>/dev/null)
    [[ -z "$ts" || "$ts" == "n/a" ]] && { echo 0; return; }
    date -d "$ts" +%s 2>/dev/null || echo 0
}

now_epoch() { date +%s; }

human_age() {
    local secs=$1
    (( secs <= 0 )) && { echo "aldrig"; return; }
    if   (( secs < 3600 )); then echo "$(( secs / 60 )) min"
    elif (( secs < 86400 )); then printf '%.1f t' "$(echo "$secs / 3600" | bc -l)"
    else printf '%.1f d' "$(echo "$secs / 86400" | bc -l)"; fi
}

# ================================================================== 1. TIMERS
section "TIMERS"

TIMER_UNITS=(pricepush.timer pricepush-notify.timer elpriser.timer)
declare -A TIMER_NEXT TIMER_LAST TIMER_ENABLED

for unit in "${TIMER_UNITS[@]}"; do
    if ! systemctl list-unit-files "$unit" >/dev/null 2>&1; then
        printf '  %s %-26s %s ikke installeret%s\n' "$(bad)" "$unit" "$R" "$N"
        note_problem
        TIMER_ENABLED[$unit]=missing
        continue
    fi

    enabled=no; is_enabled "$unit" && enabled=yes
    [[ $enabled == yes ]] || note_problem
    TIMER_ENABLED[$unit]=$enabled

    # systemctl show giver felterne enkeltvis — ingen kolonne-parsing
    next=$(systemctl show -p NextElapseUSecRealtime --value "$unit" 2>/dev/null)
    last=$(systemctl show -p LastTriggerUSec        --value "$unit" 2>/dev/null)
    [[ -z "$next" || "$next" == "n/a" ]] && next="(ingen planlagt)"
    [[ -z "$last" || "$last" == "n/a" ]] && last="(aldrig kørt)"
    [[ $next == "(ingen planlagt)" ]] && note_problem

    TIMER_NEXT[$unit]=$next
    TIMER_LAST[$unit]=$last

    if [[ $JSON -eq 0 ]]; then
        mark=$( [[ $enabled == yes ]] && ok || warn )
        printf '  %s %-26s enabled=%-3s  næste: %-30s  sidst: %s\n' \
            "$mark" "$unit" "$enabled" "$next" "$last"
    fi
done

# ================================================================= 2. SERVICES
section "SERVICES"

check_service() {
    local unit=$1 max_age=$2 label=$3
    local state epoch age
    state=$(systemctl is-active "$unit" 2>/dev/null || true)
    epoch=$(last_run_epoch "$unit")
    age=$(( $(now_epoch) - epoch ))

    if (( epoch == 0 )); then
        [[ $JSON -eq 0 ]] && printf '  %s %-26s %s ingen kørsel registreret%s\n' "$(warn)" "$label" "$Y" "$N"
        note_problem
        return
    fi

    if (( age > max_age )); then
        [[ $JSON -eq 0 ]] && printf '  %s %-26s %s sidste kørsel %s siden (grænse %s)%s\n' \
            "$(bad)" "$label" "$R" "$(human_age $age)" "$(human_age $max_age)" "$N"
        note_problem
    else
        [[ $JSON -eq 0 ]] && printf '  %s %-26s sidste kørsel %s siden  (state: %s)\n' \
            "$(ok)" "$label" "$(human_age $age)" "$state"
    fi
}

# Elpriser kører dagligt -> 26 timer er rigeligt til at fange en udeblivelse
check_service elpriser.service   $((26 * 3600)) "elpriser (collect)"
# Push kører hvert 20. minut -> 90 minutter giver plads til et par fejl
check_service pricepush.service  $((90 * 60))   "pricepush (upload)"
check_service pricepush-notify.service $((90 * 60)) "notify"

# =================================================================== 3. DATABASE
section "DATABASE"

PSQL_OK=0
if command -v psql >/dev/null 2>&1; then
    # Læs .env uden at sourcere den (den kan indeholde ting bash ikke kan lide)
    if [[ -f "$APP_DIR/.env" ]]; then
        export PGHOST=$(grep -m1 '^PGHOST='   "$APP_DIR/.env" | cut -d= -f2- | tr -d '"'"'"' ')
        export PGPORT=$(grep -m1 '^PGPORT='   "$APP_DIR/.env" | cut -d= -f2- | tr -d '"'"'"' ')
        export PGDATABASE=$(grep -m1 '^PGDATABASE=' "$APP_DIR/.env" | cut -d= -f2- | tr -d '"'"'"' ')
        export PGUSER=$(grep -m1 '^PGUSER='   "$APP_DIR/.env" | cut -d= -f2- | tr -d '"'"'"' ')
        export PGPASSWORD=$(grep -m1 '^PGPASSWORD=' "$APP_DIR/.env" | cut -d= -f2- | tr -d '"'"'"' ')
    fi

    if psql -qtAc 'SELECT 1' >/dev/null 2>&1; then
        PSQL_OK=1
    else
        [[ $JSON -eq 0 ]] && printf '  %s %s kunne ikke forbinde til Postgres%s\n' "$(bad)" "$R" "$N"
        note_problem
    fi
else
    [[ $JSON -eq 0 ]] && printf '  %s %s psql ikke installeret — springer DB over%s\n' "$(warn)" "$Y" "$N"
fi

if (( PSQL_OK == 1 )); then
    # --- collect: seneste kørsler
    collect_row=$(psql -qtAF'|' -c "
        SELECT run_id, status, coalesce(rows_fetched,0), coalesce(rows_written,0),
               to_char(finished_at, 'YYYY-MM-DD HH24:MI')
          FROM sync_runs ORDER BY run_id DESC LIMIT 1" 2>/dev/null)
    if [[ -n "$collect_row" ]]; then
        IFS='|' read -r rid st fetched written fin <<<"$collect_row"
        if [[ $st == ok ]]; then
            [[ $JSON -eq 0 ]] && printf '  %s sync_runs #%-5s ok    hentet=%-5s skrevet=%-5s  %s\n' \
                "$(ok)" "$rid" "$fetched" "$written" "$fin"
        else
            [[ $JSON -eq 0 ]] && printf '  %s sync_runs #%-5s %s%s%s   %s\n' \
                "$(bad)" "$rid" "$R" "$st" "$N" "$fin"
            note_problem
        fi
    fi

    # --- fejlede kørsler sidste døgn
    fail_count=$(psql -qtAc "
        SELECT count(*) FROM sync_runs
         WHERE status <> 'ok' AND started_at > now() - interval '24 hours'" 2>/dev/null)
    if [[ -n "$fail_count" && "$fail_count" != "0" ]]; then
        [[ $JSON -eq 0 ]] && printf '  %s %s %s fejlede collect-kørsler sidste døgn%s\n' \
            "$(bad)" "$R" "$fail_count" "$N"
        note_problem
    fi

    push_row=$(psql -qtAF'|' -c "
        SELECT run_id, status, coalesce(days_uploaded,0), coalesce(bytes_uploaded,0),
               to_char(finished_at, 'YYYY-MM-DD HH24:MI')
          FROM push_runs ORDER BY run_id DESC LIMIT 1" 2>/dev/null)
    if [[ -n "$push_row" ]]; then
        IFS='|' read -r rid st days bytes fin <<<"$push_row"
        if [[ $st == ok || $st == dry_run ]]; then
            [[ $JSON -eq 0 ]] && printf '  %s push_runs #%-5s %-7s dage=%-3s bytes=%-8s %s\n' \
                "$(ok)" "$rid" "$st" "$days" "$bytes" "$fin"
        else
            [[ $JSON -eq 0 ]] && printf '  %s push_runs #%-5s %s%s%s   %s\n' \
                "$(bad)" "$rid" "$R" "$st" "$N" "$fin"
            note_problem
        fi
    fi

    # --- vandmærke: hvor langt collect er kommet
    wm=$(psql -qtAc "
        SELECT to_char(watermark_dk AT TIME ZONE 'Europe/Copenhagen', 'YYYY-MM-DD HH24:MI')
          FROM sync_state WHERE job = 'dayahead_prices'" 2>/dev/null)
    if [[ -n "$wm" ]]; then
        # Vandmærket bør være inden for de sidste ~40 timer
        wm_age_h=$(psql -qtAc "
            SELECT round(extract(epoch from (now() - watermark_dk)) / 3600)
              FROM sync_state WHERE job = 'dayahead_prices'" 2>/dev/null)
        if [[ -n "$wm_age_h" ]] && (( wm_age_h > 40 )); then
            [[ $JSON -eq 0 ]] && printf '  %s vandmærke %s — %s%s t gammelt%s\n' \
                "$(bad)" "$wm" "$R" "$wm_age_h" "$N"
            note_problem
        else
            [[ $JSON -eq 0 ]] && printf '  %s vandmærke %s\n' "$(ok)" "$wm"
        fi
    fi

    # --- ufuldstændige dage
    short_days=$(psql -qtAc "
        SELECT count(*) FROM pushed_state
         WHERE day < CURRENT_DATE - INTERVAL '1 day' AND rows_pushed < 92" 2>/dev/null)
    if [[ -n "$short_days" && "$short_days" != "0" ]]; then
        [[ $JSON -eq 0 ]] && printf '  %s %s%s dage med < 92 punkter i pushed_state%s\n' \
            "$(warn)" "$Y" "$short_days" "$N"
        PROBLEMS=$((PROBLEMS))   # advarsel, ikke nødvendigvis en fejl
    fi
fi

# ============================================================= 4. UPLOADEDE FILER
section "SIDSTE UPLOAD (lokale out/-filer)"

if [[ -d "$APP_DIR/out" ]]; then
    newest=$(ls -t "$APP_DIR/out"/prices-*.json 2>/dev/null | head -1)
    if [[ -n "$newest" ]]; then
        age=$(( $(now_epoch) - $(stat -c %Y "$newest") ))
        size=$(stat -c %s "$newest")
        name=$(basename "$newest")
        # Et komplet døgn er ~20 KB; langt mindre tyder på tomme/manglende punkter
        if (( size < 5000 )); then
            [[ $JSON -eq 0 ]] && printf '  %s %-30s %s%s bytes — mistænkeligt lille%s\n' \
                "$(bad)" "$name" "$R" "$size" "$N"
            note_problem
        else
            [[ $JSON -eq 0 ]] && printf '  %s %-30s %s bytes  skrevet %s siden\n' \
                "$(ok)" "$name" "$size" "$(human_age $age)"
        fi
    else
        [[ $JSON -eq 0 ]] && printf '  %s ingen pricedata i %s/out%s\n' "$(bad)" "$APP_DIR" "$N"
        note_problem
    fi

    file_count=$(ls -1 "$APP_DIR/out"/prices-*.json 2>/dev/null | wc -l)
    [[ $JSON -eq 0 ]] && printf '    %s%s filer i out/%s\n' "$D" "$file_count" "$N"
else
    [[ $JSON -eq 0 ]] && printf '  %s %s findes ikke%s\n' "$(bad)" "$APP_DIR/out" "$N"
    note_problem
fi

# ================================================================ 5. DETALJER
if (( VERBOSE == 1 )) && [[ $JSON -eq 0 ]]; then
    section "SENESTE LOG (pricepush, 15 linjer)"
    journalctl -u pricepush.service -n 15 --no-pager 2>/dev/null | sed 's/^/    /' || \
        printf '    %s(journalen er ikke tilgængelig uden sudo)%s\n' "$Y" "$N"

    section "SENESTE LOG (notify, 8 linjer)"
    journalctl -u pricepush-notify.service -n 8 --no-pager 2>/dev/null | sed 's/^/    /' || true

    section "JOURNALENS LAGRING"
    if [[ -d /var/log/journal ]]; then
        printf '    %s✔ persistent%s (%s)\n' "$G" "$N" "$(journalctl --disk-usage 2>/dev/null)"
    else
        printf '    %s! flygtig%s — log forsvinder ved genstart (mkdir /var/log/journal)\n' "$Y" "$N"
    fi
fi

# ==================================================================== SUMMARY
if [[ $JSON -eq 1 ]]; then
    # Maskinlæsbar oversigt
    printf '{"problems":%d,"timers":{' "$PROBLEMS"
    first=1
    for unit in "${TIMER_UNITS[@]}"; do
        (( first )) || printf ','
        first=0
        printf '"%s":{"enabled":"%s"}' "$unit" "${TIMER_ENABLED[$unit]:-unknown}"
    done
    printf '},"prices_ok":%s}\n' "$(( PSQL_OK ))"

elif (( PROBLEMS == 0 )); then
    printf '\n%s✔ alt ser fint ud%s — 0 problemer\n' "$G" "$N"
else
    printf '\n%s%s%s %d ting at se på\n' "$B$R" "ADVARSEL:" "$N" "$PROBLEMS"
fi

# Exit 0 = alt ok, 1 = der er noget at se på. Brugbart i cron/overvågning.
exit $(( PROBLEMS > 0 ? 1 : 0 ))
