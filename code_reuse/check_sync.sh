#!/usr/bin/env bash
# Daglig kontroll: är DB Syncs modified-scheman i takt med gpkg-utcheckningarna?
# Bakgrund: fynd F2/F3 (POC-runbook) — DB Sync märker inte själv när databasen ligger efter gpkg:n.
#
#   ./check_sync.sh                          # Bönsta: schema p3922_bonsta, configkatalog dbsync
#   ./check_sync.sh p3922_test dbsync-test   # klonen
#
# Layout som stöds:
#   <configdir>/conn/<lager>/work/<projekt>/<lager>.gpkg   (en container per lager, gen_dbsync.py)
#   <configdir>/work/<projekt>/<lager>.gpkg                (gammal layout, en container)
#
# Cron (root, kl 06:30):  30 6 * * * cd /opt/ak-db && ./check_sync.sh >> /var/log/ak-check-sync.log 2>&1 || echo "DB Sync ur synk, se /var/log/ak-check-sync.log" | mail -s "AK DB Sync AVVIKER" nicklas@arkeologikonsult.se
set -uo pipefail
SCHEMA="${1:-p3922_bonsta}"
CFGDIR="${2:-$(dirname "$0")/dbsync}"
DSN="${PSQL_DSN:-postgresql://ak_admin@localhost/ak}"   # PGPASSWORD i miljön eller ~/.pgpass
[[ -z "${PGPASSWORD:-}" && -f "$(dirname "$0")/.env" ]] && export PGPASSWORD="$(grep ^PG_ADMIN_PASSWORD "$(dirname "$0")/.env" | cut -d= -f2-)"

rc=0
now() { date +%F\ %T; }

# Hitta gpkg-filer och tillhörande loggar
declare -a GPKGS LOGS
if [[ -d "$CFGDIR/conn" ]]; then
  for d in "$CFGDIR"/conn/*/; do
    layer=$(basename "$d")
    f=$(ls "$d"work/*/"$layer".gpkg 2>/dev/null | head -1)
    [[ -n "$f" ]] && GPKGS+=("$f") || echo "$(now)  $layer: ingen utcheckning i ${d}work/ (init ej klar?)"
    LOGS+=("${d}dbsync.log")
  done
elif ls -d "$CFGDIR"/work/*/"${SCHEMA}"_s_*/ >/dev/null 2>&1; then
  # patchad DB Sync (#163): en utcheckning per anslutning, <work>/<projekt>/<modified-schema>/
  # varje utcheckning innehåller ALLA gpkg – ta bara lagret som hör till schemat
  for d in "$CFGDIR"/work/*/"${SCHEMA}"_s_*/; do
    sd=$(basename "$d")
    for f in "$d"[123]-*.gpkg; do
      layer=$(basename "$f" .gpkg)
      [[ "$sd" == "${SCHEMA}_s_$(echo "$layer" | tr '-' '_')" ]] && GPKGS+=("$f")
    done
  done
  LOGS=("$CFGDIR/dbsync.log")
else
  GPKGS=("$CFGDIR"/work/*/[123]-*.gpkg)
  LOGS=("$CFGDIR/dbsync.log")
fi

printf "%s  %-30s %6s %6s  %s\n" "$(now)" lager gpkg db status
for f in "${GPKGS[@]}"; do
  layer=$(basename "$f" .gpkg)
  schema="${SCHEMA}_s_$(echo "$layer" | tr '-' '_')"
  g=$(ogrinfo -ro -so "$f" "$layer" 2>/dev/null | awk '/Feature Count/ {print $3}')
  d=$(psql "$DSN" -Atc "select count(*) from \"$schema\".\"$layer\"" 2>/dev/null)
  if [[ -n "$g" && "$g" == "$d" ]]; then st=OK; else st=AVVIKER; rc=1; fi
  printf "%s  %-30s %6s %6s  %s\n" "$(now)" "$layer" "${g:-?}" "${d:-?}" "$st"
done

# Fel i DB Sync-loggarna sedan förra körningen. Loggarna är append-only; vi minns radnummer per logg.
# geodiff loggar alla NOTICE från databasen som ERROR – våra AK-VALIDERING-varningar och racet
# "pending changes on server" (ny Mergin-version mitt i cykeln) är inte fel.
for LOG in "${LOGS[@]}"; do
  [[ -f "$LOG" ]] || continue
  STATE="$(dirname "$LOG")/.check_sync.lastline"
  total=$(wc -l < "$LOG")
  last=$(cat "$STATE" 2>/dev/null || echo 0)
  [[ "$last" -gt "$total" ]] && last=0
  new=$(tail -n +$((last + 1)) "$LOG")
  v=$(printf '%s\n' "$new" | grep -c "AK-VALIDERING" || true)
  n=$(printf '%s\n' "$new" | grep -i "error\|exception\|traceback" \
        | grep -v "AK-VALIDERING\|pending changes on server" | grep -c . || true)
  [[ "${v:-0}" -gt 0 ]] && echo "$(now)  $LOG: $v valideringsvarningar (AK-VALIDERING) – se v_validering_sammanfattning"
  if [[ "${n:-0}" -gt 0 ]]; then
    echo "$(now)  $LOG: $n nya fel-rader — visa:  tail -n +$((last + 1)) $LOG | grep -n -i 'error\|traceback' | grep -v AK-VALIDERING | tail"
    rc=1
  fi
  echo "$total" > "$STATE"
done
exit $rc
