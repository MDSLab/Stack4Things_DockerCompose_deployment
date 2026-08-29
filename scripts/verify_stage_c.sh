#!/usr/bin/env bash
# verify_stage_c.sh - decide, mechanically, whether stage C is done.
#
# Stage C claims: telemetry flows and twins stay in sync with no terminal
# windows open, both services report health that means "connected and
# working", and the whole thing survives a restart.
#
# WHY THIS SCRIPT EXISTS
# ----------------------
# On 23 Aug 2026 the Ditto connection reported liveStatus <open> for hours
# while its RabbitMQ publisher crash-looped on "Duplicate key ditto" and could
# not deliver a single outbound message. Stage B had been signed off on that
# status field. A status field is a claim about a component; it is not
# evidence that a message moved.
#
# So the two checks that matter here, C9 and C10, do not read any status.
# They put a message in one end and look for it at the other.
#
# Read only, except C9 and C10 which create and then delete one probe twin.
# No credential is ever printed.
#
#   ./scripts/verify_stage_c.sh

set -u
cd "$(dirname "$0")/.." || exit 1

dcs() { docker compose -f docker-compose.yml -f docker-compose.ditto.yml "$@"; }

set -a; . ./.env; set +a
DITTO="http://localhost:${DITTO_EXTERNAL_PORT:-8090}"
AUTH="${DITTO_API_USER:-s4t}:${DITTO_API_PASSWORD}"
NS="${DITTO_NAMESPACE:-s4t}"
POLICY="${DITTO_POLICY:-s4t:fleet-unime-lab}"
PROBE="${NS}:stagec-probe"

PASS=0; FAIL=0; VOID=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
no()   { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); [ $# -gt 1 ] && printf '        %s\n' "$2"; }
# A check that ran, did not fail, and proved nothing. "0 twins for 0 boards"
# is true and worthless. Counting these as passes is how a suite reports green
# on a system nobody has demonstrated works.
void() { printf '  \033[33mVOID\033[0m  %s\n' "$1"; VOID=$((VOID+1)); [ $# -gt 1 ] && printf '        %s\n' "$2"; }
note() { printf '        %s\n' "$1"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

health() { docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$1" 2>/dev/null || echo absent; }

head_ "1. The three containers"

CODE=$(docker inspect -f '{{.State.ExitCode}}' s4t-ditto-bootstrap 2>/dev/null || echo missing)
[ "$CODE" = "0" ] && ok "s4t-ditto-bootstrap exited 0" \
  || no "s4t-ditto-bootstrap exit code is '$CODE'" "nothing downstream starts until this is 0"

for c in s4t-ditto-bridge s4t-ditto-provisioner; do
  H=$(health "$c")
  [ "$H" = "healthy" ] && ok "$c is healthy" \
    || no "$c is '$H'" "the heartbeat file is stale; this is a real signal, read the logs"
done

head_ "2. The bridge is attached to both buses"

BLOG=$(dcs logs s4t-ditto-bridge 2>/dev/null)
grep -q "AMQP queue .* ready"    <<<"$BLOG" && ok "AMQP inbound queue declared"   || no "no AMQP queue line"
grep -q "AMQP exchange .* ready" <<<"$BLOG" && ok "AMQP outbound exchange bound"  || no "no AMQP exchange line"
if grep -q "WAMP joined realm" <<<"$BLOG"; then
  ok "WAMP session joined and subscribed"
else
  no "bridge never joined WAMP" "$(grep -m1 -i 'error\|assertion' <<<"$BLOG" | cut -c1-100)"
fi

head_ "3. The provisioner is reconciling"

PLOG=$(dcs logs --tail 200 s4t-ditto-provisioner 2>/dev/null)
LAST=$(grep 'pass complete' <<<"$PLOG" | tail -1)
if [ -z "$LAST" ]; then
  no "no completed reconciliation pass" "it has never finished a cycle"
else
  if grep -qE '(created|updated|unchanged|repaired)=[1-9]' <<<"$LAST"; then
    ok "reconciliation running: ${LAST#*pass complete: }"
  else
    void "pass completed but every counter is zero" \
         "proves the loop runs and can reach the DB and Ditto; proves nothing about provisioning"
  fi
  grep -q 'failed=0' <<<"$LAST" && ok "last pass had failed=0" \
    || no "last pass reports failures" "the count line names the failing operation"
  # a steady stream of updated= on an idle system means something rewrites an
  # always-changing value, which would emit a change event per board per cycle
  UPD=$(grep -c 'updated=[1-9]' <<<"$PLOG")
  [ "$UPD" -le 1 ] && ok "no update churn on an idle system ($UPD passes with updates)" \
    || no "$UPD passes wrote updates while idle" "something is rewriting a changing value"
fi

head_ "4. Twins match boards"

BOARDS=$(docker exec iotronic-db mariadb -uroot -p"$MYSQL_ROOT_PASSWORD" -N -B \
         -e "SELECT COUNT(*) FROM iotronic.boards WHERE uuid IS NOT NULL;" 2>/dev/null | tr -d '[:space:]')
TWINS=$(curl -s -u "$AUTH" "$DITTO/api/2/search/things?namespaces=$NS" \
        | python3 -c 'import json,sys
try: print(len(json.load(sys.stdin).get("items",[])))
except Exception: print("?")' 2>/dev/null)
if [ "$BOARDS" = "0" ] && [ "$TWINS" = "0" ]; then
  void "0 twins for 0 boards" \
       "vacuously true; the board-to-twin path is the point of the provisioner and is untested"
elif [ "$BOARDS" = "$TWINS" ] && [ -n "$BOARDS" ]; then
  ok "$TWINS twin(s) for $BOARDS board(s)"
else
  no "board/twin mismatch: $BOARDS boards, $TWINS twins" "give the provisioner one interval, then re-check"
fi

head_ "5. Regression guard: the publisher is not crash-looping"

DUP=$(dcs logs --since 5m ditto-connectivity 2>/dev/null | grep -c "Duplicate key")
[ "$DUP" -eq 0 ] && ok "no 'Duplicate key' in the last 5 minutes" \
  || no "$DUP 'Duplicate key' errors in 5 minutes" "two targets share an exchange again; one exchange per target"

head_ "6. DELIVERY, outbound. Does a command actually reach the bridge?"
note "this is the check that liveStatus could not make"

curl -s -o /dev/null -u "$AUTH" -X PUT "$DITTO/api/2/things/$PROBE" \
  -H 'Content-Type: application/json' \
  -d "{\"policyId\":\"$POLICY\",\"features\":{\"telemetry\":{\"properties\":{}}}}"
MARK=$(dcs logs --tail 1 s4t-ditto-bridge 2>/dev/null | wc -l)
curl -s -o /dev/null -u "$AUTH" -X PUT \
  "$DITTO/api/2/things/$PROBE/features/telemetry/desiredProperties" \
  -H 'Content-Type: application/json' -d '{"stagec_probe":true}'

DELIVERED=0
for _ in $(seq 1 15); do
  if dcs logs --since 60s s4t-ditto-bridge 2>/dev/null | grep -q "stagec-probe"; then DELIVERED=1; break; fi
  sleep 2
done
[ "$DELIVERED" = "1" ] \
  && ok "twin change traversed Ditto -> RabbitMQ -> bridge -> WAMP" \
  || no "the command never arrived at the bridge" "outbound is broken regardless of what liveStatus says"

curl -s -o /dev/null -u "$AUTH" -X DELETE "$DITTO/api/2/things/$PROBE"
note "probe twin deleted"

head_ "7. DELIVERY, inbound. Does telemetry actually reach a twin?"

UUID=$(docker exec iotronic-db mariadb -uroot -p"$MYSQL_ROOT_PASSWORD" -N -B \
       -e "SELECT uuid FROM iotronic.boards WHERE uuid IS NOT NULL LIMIT 1;" 2>/dev/null | tr -d '[:space:]')
if [ -z "$UUID" ]; then
  void "no board to test with" "register one in Horizon, or restore the backup, then re-run"
else
  BEFORE=$(curl -s -u "$AUTH" "$DITTO/api/2/things/$NS:$UUID/features/telemetry/properties")

  # Foreground under `timeout`, output captured. The earlier version used
  # `docker exec -d ... 2>/dev/null` and then pkill, which meant a mock board
  # that failed to start looked exactly like telemetry that failed to arrive.
  # Two very different faults, one indistinguishable symptom. Never discard the
  # output of the thing whose behaviour you are testing.
  # (`pkill` is also absent from python:3.12-slim, which has no procps.)
  MOCK=$(docker exec s4t-ditto-bridge timeout 14 python3 s4t_mock_board.py \
           "$UUID" --host crossbar --interval 2 2>&1)

  AFTER=$(curl -s -u "$AUTH" "$DITTO/api/2/things/$NS:$UUID/features/telemetry/properties")

  if [ "$BEFORE" != "$AFTER" ] && [ -n "$AFTER" ]; then
    ok "mock board telemetry reached twin $NS:$UUID"
  else
    no "twin telemetry did not change"
    if ! grep -q "joined realm" <<<"$MOCK"; then
      note "the MOCK BOARD never joined WAMP, so nothing was ever published:"
      sed -n '1,6p' <<<"$MOCK" | sed 's/^/          /'
    elif ! grep -q "reported" <<<"$MOCK"; then
      note "the board joined but published nothing:"
      sed -n '1,6p' <<<"$MOCK" | sed 's/^/          /'
    else
      note "the board published, so the break is downstream of it:"
      note "  board said: $(grep -m1 reported <<<"$MOCK")"
      note "  bridge '-> ditto' lines in the last minute: $(dcs logs --since 60s s4t-ditto-bridge 2>/dev/null | grep -c -- '-> ditto')"
      note "  ditto.inbound consumers: $(docker exec rabbitmq rabbitmqctl list_queues name consumers 2>/dev/null | grep ditto.inbound || echo unknown)"
      note "  twin before: ${BEFORE:0:70}"
      note "  twin after : ${AFTER:0:70}"
    fi
  fi
fi

head_ "Result"
printf '  %d passed, %d failed, %d proved nothing\n\n' "$PASS" "$FAIL" "$VOID"
if [ "$FAIL" -eq 0 ] && [ "$VOID" -gt 0 ]; then
  printf '  Stage C is NOT done. Nothing is broken, but %d check(s) could not\n' "$VOID"
  printf '  gather evidence. An empty system passes almost any test.\n'
  exit 2
fi
if [ "$FAIL" -eq 0 ]; then
  printf '  Stage C is done, except for restart survival, which is deliberately\n'
  printf '  not automated because it takes minutes and should be watched:\n\n'
  printf '      dcs down          # NO -v, ever\n'
  printf '      dcs up -d\n'
  printf '      sleep 180 && ./scripts/verify_stage_c.sh\n\n'
  printf '  Stage C is complete when this script passes twice: once on a warm\n'
  printf '  stack, and again after that restart with no manual step in between.\n'
  exit 0
else
  printf '  Stage C is NOT done. Fix the failures above and re-run.\n'
  exit 1
fi
