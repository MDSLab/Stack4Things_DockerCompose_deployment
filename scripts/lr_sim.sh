#!/usr/bin/env bash
# lr_sim.sh - start, onboard and test three simulated Lightning-Rod boards.
#
#   ./scripts/lr_sim.sh up         start lr-sim-1..3 (separate "lr-sim" project)
#   ./scripts/lr_sim.sh onboard    configure each LR with its board code, no web UI
#   ./scripts/lr_sim.sh status     boards in the registry + simulation containers
#   ./scripts/lr_sim.sh test       automatic PASS/FAIL checks
#   ./scripts/lr_sim.sh logs N     last log lines of lr-sim-N
#   ./scripts/lr_sim.sh down       remove ONLY the simulation containers and volumes
#
# Before "onboard": create three boards in Horizon with the codes
# SIM_01, SIM_02 and SIM_03 (any name, any type).
#
# Safe for the main stack: it never touches the main compose project, its
# containers or its volumes. The only change outside the simulation is the
# three SIM_* boards you create yourself in Horizon.

set -u
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$0")/.." || exit 1
set -a; . ./.env; set +a

PROJECT=lr-sim
FILE=docker-compose.lr-sim.yml
CODES=(SIM_01 SIM_02 SIM_03)
AGENT_URL="${LR_AGENT_URL:-wss://crossbar:8181/}"
TIMEOUT="${LR_TIMEOUT:-240}"

B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; RD=$'\033[31m'; R=$'\033[0m'
say()  { printf '\n%s%s%s\n' "$B" "$1" "$R"; }
ok()   { printf '   %sPASS%s  %s\n' "$G" "$R" "$1"; PASS=$((PASS+1)); }
bad()  { printf '   %sFAIL%s  %s\n' "$RD" "$R" "$1"; FAIL=$((FAIL+1)); }
info() { printf '   %sINFO%s  %s\n' "$Y" "$R" "$1"; }
PASS=0; FAIL=0

db() { docker exec iotronic-db mariadb -uroot -p"$MYSQL_ROOT_PASSWORD" -N -B -e "$1" 2>/dev/null; }
db_check() {
  local err
  if [ "$(db 'SELECT 1;')" != "1" ]; then
    err=$(docker exec iotronic-db mariadb -uroot -p"$MYSQL_ROOT_PASSWORD" -N -B -e 'SELECT 1;' 2>&1 | tail -2)
    echo "   ${RD}Cannot query the IoTronic database:${R} ${err:-no output}"
    echo "   Check that iotronic-db is running and MYSQL_ROOT_PASSWORD in .env is correct."
    exit 1
  fi
}
# Codes are matched ignoring case and surrounding spaces, as typed in Horizon.
norm() { echo "UPPER(TRIM(code))"; }
real_code() { db "SELECT code FROM iotronic.boards WHERE $(norm)=UPPER('$1') LIMIT 1;"; }
running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }
sim_in() { local s=""; for c in "${CODES[@]}"; do s="$s,'$c'"; done; echo "${s#,}"; }

network() {
  local nets
  nets=$(docker inspect crossbar -f '{{range $k,$v := .NetworkSettings.Networks}}{{println $k}}{{end}}' 2>/dev/null | grep .)
  echo "$nets" | grep -m1 's4t$' || echo "$nets" | head -1
}
compose() { S4T_NETWORK="$(network)" docker compose -p "$PROJECT" -f "$FILE" "$@"; }

# ------------------------------------------------------------------- up
cmd_up() {
  running crossbar || { echo "crossbar is not running: start the main stack first."; exit 1; }
  say "Starting 3 simulated Lightning-Rod boards on network $(network)"
  compose up -d || exit 1
  echo
  echo "   Each container installs two Python packages at start (about 30-90 s)."
  echo "   Web UIs: http://localhost:1475  :1476  :1477   (login me / arancino)"
  echo "   Next: create boards SIM_01, SIM_02, SIM_03 in Horizon, then run:"
  echo "         ./scripts/lr_sim.sh onboard"
}

# -------------------------------------------------------------- onboard
configure_one() {   # $1 container  $2 code
  local c="$1" code="$2" i
  for i in $(seq 1 60); do
    docker exec "$c" test -f /etc/iotronic/settings.json 2>/dev/null && break
    sleep 2
  done
  if ! docker exec "$c" test -f /etc/iotronic/settings.json 2>/dev/null; then
    bad "$c: /etc/iotronic/settings.json not found after 120 s"; return
  fi
  if ! docker exec "$c" grep -q '<REGISTRATION-TOKEN>' /etc/iotronic/settings.json; then
    info "$c already configured, skipped"; return
  fi
  if docker exec "$c" sh -c 'command -v lr_configure' >/dev/null 2>&1; then
    docker exec "$c" lr_configure "$code" "$AGENT_URL" >/dev/null
  else
    # Same edit lr_configure makes, done directly on the JSON file.
    docker exec -i "$c" python3 - "$code" "$AGENT_URL" <<'PY'
import json, sys
p = "/etc/iotronic/settings.json"
conf = json.load(open(p))
conf["iotronic"]["board"]["code"] = sys.argv[1]
ra = conf["iotronic"]["wamp"]["registration-agent"]
ra["url"] = sys.argv[2]
ra["realm"] = "s4t"
json.dump(conf, open(p, "w"), indent=4)
PY
  fi
  if docker exec "$c" grep -q "\"$code\"" /etc/iotronic/settings.json; then
    docker restart "$c" >/dev/null && ok "$c configured with $code, restarting"
  else
    bad "$c: configuration was not written"
  fi
}

cmd_onboard() {
  say "Onboarding (registration agent $AGENT_URL, realm s4t)"
  db_check
  local i code c rc MISSING=0
  for i in 1 2 3; do
    code="${CODES[$((i-1))]}"; c="lr-sim-$i"
    running "$c" || { bad "$c is not running (run: up)"; continue; }
    rc=$(real_code "$code")
    if [ -z "$rc" ]; then
      bad "no board with code $code in the registry: create it in Horizon first"
      MISSING=1; continue
    fi
    configure_one "$c" "$rc"
  done
  if [ "$MISSING" = 1 ]; then
    echo
    echo "   Board codes currently in the registry:"
    db "SELECT CONCAT('     ', name, '  code=[', code, ']  status=', status) FROM iotronic.boards ORDER BY created_at;"
    echo "   The code is the field typed in the Horizon \"Create Board\" form, not the name."
  fi
  echo
  echo "   Boards need about 1-2 minutes to come up. Then run: ./scripts/lr_sim.sh test"
}

# --------------------------------------------------------------- status
cmd_status() {
  db_check
  say "Boards in the IoTronic registry"
  local out
  out=$(db "SELECT name, code, status, IFNULL(lr_version,'-'), IFNULL(agent,'-') FROM iotronic.boards ORDER BY created_at;") \
    && { printf 'NAME\tCODE\tSTATUS\tLR_VERSION\tAGENT\n%s\n' "$out" | column -t -s $'\t'; } \
    || { out=$(db "SELECT name, code, status, IFNULL(lr_version,'-') FROM iotronic.boards ORDER BY created_at;");
         printf 'NAME\tCODE\tSTATUS\tLR_VERSION\n%s\n' "$out" | column -t -s $'\t'; }
  say "Simulation containers"
  docker ps -a --filter "name=lr-sim-" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
}

# ----------------------------------------------------------------- test
cmd_test() {
  db_check
  local in; in=$(sim_in)
  say "1. Platform components"
  for c in crossbar iotronic-wstun iotronic-conductor iotronic-wagent iotronic-db; do
    running "$c" && ok "$c running" || bad "$c not running"
  done

  say "2. Simulated devices"
  for i in 1 2 3; do running "lr-sim-$i" && ok "lr-sim-$i running" || bad "lr-sim-$i not running"; done

  say "3. Boards registered in IoTronic"
  for code in "${CODES[@]}"; do
    [ "$(db "SELECT COUNT(*) FROM iotronic.boards WHERE $(norm)=UPPER('$code');")" = "1" ] \
      && ok "board $code exists" || bad "board $code missing (create it in Horizon)"
  done

  say "4. All boards operative at the same time (waiting up to ${TIMEOUT}s)"
  local t=0 n=0
  while [ $t -lt "$TIMEOUT" ]; do
    n=$(db "SELECT COUNT(*) FROM iotronic.boards WHERE $(norm) IN ($in) AND status='online';")
    n2=$(db "SELECT COUNT(*) FROM iotronic.boards WHERE $(norm) IN ($in) AND status='operative';")
    n=$(( ${n:-0} + ${n2:-0} ))
    [ "$n" -ge ${#CODES[@]} ] && break
    printf '\r   ... %s/%s operative after %ss ' "$n" "${#CODES[@]}" "$t"; sleep 5; t=$((t+5))
  done
  printf '\r%60s\r' ''
  for code in "${CODES[@]}"; do
    st=$(db "SELECT status FROM iotronic.boards WHERE $(norm)=UPPER('$code');")
    case "$st" in operative|online) ok "$code status = $st";; *) bad "$code status = ${st:-unknown}";; esac
  done

  say "5. Each agent really connected (lr_version reported by the device)"
  for code in "${CODES[@]}"; do
    v=$(db "SELECT IFNULL(lr_version,'') FROM iotronic.boards WHERE $(norm)=UPPER('$code');")
    [ -n "$v" ] && ok "$code lr_version = $v" || bad "$code has no lr_version"
  done

  say "6. Each device has its own identity"
  d=$(db "SELECT COUNT(DISTINCT uuid) FROM iotronic.boards WHERE $(norm) IN ($in);")
  [ "${d:-0}" = "${#CODES[@]}" ] && ok "${#CODES[@]} different board UUIDs" || bad "only ${d:-0} distinct UUIDs"
  for i in 1 2 3; do
    u=$(docker exec "lr-sim-$i" python3 -c 'import json;print(json.load(open("/etc/iotronic/settings.json"))["iotronic"]["board"].get("uuid","-"))' 2>/dev/null)
    info "lr-sim-$i holds board uuid ${u:--}"
  done

  say "7. Existing real board not affected"
  others=$(db "SELECT CONCAT(name,' (',status,')') FROM iotronic.boards WHERE $(norm) NOT IN ($in);" | paste -sd ',' -)
  info "other boards: ${others:-none}"

  say "Result: ${G}${PASS} passed${R}, $([ $FAIL -gt 0 ] && echo "$RD")${FAIL} failed${R}"
  [ $FAIL -eq 0 ]
}

# ------------------------------------------------------------ logs/down
cmd_logs() { docker logs --tail "${2:-60}" "lr-sim-${1:-1}"; }

cmd_down() {
  say "This removes lr-sim-1..3 and their volumes. The main stack is not touched."
  printf '   Type yes to continue: '; read -r a; [ "$a" = "yes" ] || exit 0
  compose down -v
  echo
  echo "   Also delete boards SIM_01..SIM_03 in Horizon (Boards > Delete),"
  echo "   so they can be created again cleanly next time."
}

case "${1:-}" in
  up) cmd_up ;; onboard) cmd_onboard ;; status) cmd_status ;;
  test) cmd_test ;; logs) cmd_logs "${2:-1}" "${3:-60}" ;; down) cmd_down ;;
  *) sed -n '2,15p' "$SELF" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
