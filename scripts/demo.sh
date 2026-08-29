#!/usr/bin/env bash
# demo.sh - a narrated live demonstration of the Stack4Things / Ditto twin layer.
#
# TWO TERMINALS.
#
#   Terminal 1 (the "device"):
#       ./scripts/demo.sh board
#
#   Terminal 2 (the "operator", this drives the demo):
#       ./scripts/demo.sh
#
# Each step waits for Enter, so you control the pace and can talk over it.
# Nothing here is destructive. The probe twin it creates is deleted at the end.

set -u
cd "$(dirname "$0")/.." || exit 1
set -a; . ./.env; set +a

DITTO="http://localhost:${DITTO_EXTERNAL_PORT:-8090}"
AUTH="${DITTO_API_USER:-s4t}:${DITTO_API_PASSWORD}"
NS="${DITTO_NAMESPACE:-s4t}"

B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; C=$'\033[36m'; R=$'\033[0m'
say()   { printf '\n%s%s%s\n' "$B" "$1" "$R"; }
tell()  { printf '   %s\n' "$1"; }
pause() { printf '\n   %s[Enter]%s ' "$Y" "$R"; read -r _; }
jq_()   { python3 -m json.tool 2>/dev/null || cat; }

db() { docker exec iotronic-db mariadb -uroot -p"$MYSQL_ROOT_PASSWORD" -N -B -e "$1" 2>/dev/null; }
props() { curl -s -u "$AUTH" "$DITTO/api/2/things/$NS:$1/features/telemetry/properties"; }

UUID="${2:-$(db 'SELECT uuid FROM iotronic.boards WHERE uuid IS NOT NULL ORDER BY created_at LIMIT 1;' | tr -d '[:space:]')}"

# ---------------------------------------------------------------- device mode
if [ "${1:-}" = "board" ]; then
  [ -z "$UUID" ] && { echo "no board found in iotronic.boards"; exit 1; }
  echo "${B}Simulated device: $UUID${R}"
  echo "It publishes telemetry on WAMP and listens for commands. Leave this running."
  echo
  exec docker exec -it s4t-ditto-bridge python3 s4t_mock_board.py "$UUID" \
       --host crossbar --interval 3 ${DEMO_SPIKE:+--spike-every 5 --spike 25}
fi

# -------------------------------------------------------------- operator mode
clear
cat <<BANNER
${B}Stack4Things + Eclipse Ditto: the digital twin layer${R}

   Board under test: ${C}$UUID${R}
   Ditto Explorer:   ${C}$DITTO/ui/${R}
   Horizon:          ${C}http://localhost:8088/horizon${R}
BANNER
pause

say "1. IoTronic owns device identity"
tell "Boards are registered in Horizon. That is the authority on what exists."
db "SELECT uuid, code, name, type, status FROM iotronic.boards;" | sed 's/^/     /'
pause

say "2. Ditto owns device state, and it followed automatically"
tell "Nobody created these twins by hand. The provisioner reconciles the boards"
tell "table against Ditto every 30 seconds and creates whatever is missing."
curl -s -u "$AUTH" "$DITTO/api/2/search/things?namespaces=$NS&fields=thingId,policyId,attributes/boardName" | jq_ | sed 's/^/     /'
pause

say "3. Telemetry, device to twin"
tell "In the other terminal the device is publishing over WAMP."
tell "Watch the twin's properties change. Nothing polls; this is event driven."
for i in 1 2 3; do
  printf '     %s%s%s\n' "$G" "$(props "$UUID")" "$R"
  sleep 4
done
tell ""
tell "WAMP -> bridge -> RabbitMQ -> Ditto connection -> JavaScript mapping -> twin."
pause

say "4. Commands, twin to device"
tell "Now the other direction. An operator writes a DESIRED value on the twin."
tell "The device is never addressed directly."
echo
tell "Before:  reported $(props "$UUID")"
curl -s -o /dev/null -u "$AUTH" -X PUT \
  "$DITTO/api/2/things/$NS:$UUID/features/telemetry/desiredProperties" \
  -H 'Content-Type: application/json' -d '{"fan_on":true}'
tell "Wrote:   desired {\"fan_on\": true}"
tell ""
tell "Look at the device terminal. It just printed 'applied {...}'."
sleep 6
tell "After:   reported $(props "$UUID")"
pause

say "5. Convergence, and why the traffic stops"
tell "The bridge compares desired against reported and sends only the difference."
tell "The device complied, so the difference is now empty and no further command"
tell "is sent. No acknowledgement protocol, no retry bookkeeping. It self-terminates."
echo
docker compose -f docker-compose.yml -f docker-compose.ditto.yml \
  logs --tail 6 s4t-ditto-bridge 2>/dev/null | grep -E '<- board|already converged' | sed 's/^/     /'
pause

say "6. Register a new board, live"
tell "Open Horizon, create a board, and within 30 seconds its twin exists."
tell "Reconciliation is level triggered: it compares full sets, so a board added"
tell "while the twin layer was offline still gets a twin when it comes back."
tell ""
tell "Boards now: $(db 'SELECT COUNT(*) FROM iotronic.boards;' | tr -d '[:space:]')   Twins now: $(curl -s -u "$AUTH" "$DITTO/api/2/search/things?namespaces=$NS" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["items"]))' 2>/dev/null)"
pause

say "7. The evidence"
tell "Every claim above is checked mechanically, including two checks that put a"
tell "message in one end and look for it at the other rather than reading a status."
pause
./scripts/verify_stage_c.sh

# tidy: clear the desired property so the demo is repeatable
curl -s -o /dev/null -u "$AUTH" -X DELETE \
  "$DITTO/api/2/things/$NS:$UUID/features/telemetry/desiredProperties/fan_on"
say "Demo complete. Desired property cleared, so this can be run again."
