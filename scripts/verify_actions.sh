#!/usr/bin/env bash
# verify_actions.sh - Phase 1 acceptance tests T1-T7 for the action catalogue.
#
# Same rules as verify_audit_logger.sh: a check passes only when something
# was put in at one end and found at the other, and a check that could not
# gather evidence says VOID rather than PASS.
#
# What it touches, so it is safe to run on the production instance:
#   * T1 sends DevicePing (changes nothing) to LR_BOARD, if you name one.
#   * T2 REBOOTS LR_BOARD, and only when you also set RUN_REBOOT=1.
#   * T4 sets fan_on on TWIN_BOARD and clears it again afterwards. Name a
#     board driven by s4t_mock_board.py, never a real machine.
#   * T3, with RUN_REBOOT=1, asks the conductor to reboot LR_BOARD as a
#     NON-admin user. It should be refused; if the conductor's policy allows
#     it, the board WILL reboot, which is exactly what T3 exists to find.
#   * Everything else reads, or is refused by design.
#
# Usage:
#   ./scripts/verify_actions.sh
#   LR_BOARD=<uuid of an online lr-sim board> \
#   TWIN_BOARD=<uuid of the mock board's twin> \
#   MEMBER_USER=bob MEMBER_PASS=... MEMBER_PROJECT=admin \
#   RUN_REBOOT=1 ./scripts/verify_actions.sh

set -u
cd "$(dirname "$0")/.." || exit 1
set -a; . ./.env; set +a

API="http://localhost:${AUDIT_API_EXTERNAL_PORT:-8890}"
H="Authorization: Bearer ${AUDIT_API_TOKEN:-}"
LR_BOARD="${LR_BOARD:-}"
TWIN_BOARD="${TWIN_BOARD:-}"
RUN_REBOOT="${RUN_REBOOT:-0}"

PASS=0; FAIL=0; VOID=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
# Each ends in `return 0`, so `cond && no ... || ok ...` can never print both.
no()   { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); [ $# -gt 1 ] && printf '        %s\n' "$2"; return 0; }
void() { printf '  \033[33mVOID\033[0m  %s\n' "$1"; VOID=$((VOID+1)); [ $# -gt 1 ] && printf '        %s\n' "$2"; return 0; }
note() { printf '        %s\n' "$1"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }
jq_()  { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

# An HTTP call made from INSIDE the Docker network, where s4t-twin-api and the
# proxy live; neither publishes a port to the host. Prints "<status> <body>".
incall() {   # METHOD URL TOKEN [JSON_BODY]
  docker exec -i -e M="$1" -e U="$2" -e T="${3:-}" -e B="${4:-}" s4t-audit-logger python3 - <<'PY' 2>/dev/null
import os, urllib.request, urllib.error
h = {"Content-Type": "application/json"}
if os.environ["T"]:
    h["X-Auth-Token"] = os.environ["T"]
data = os.environ["B"].encode() if os.environ["B"] else None
req = urllib.request.Request(os.environ["U"], data=data, method=os.environ["M"], headers=h)
try:
    r = urllib.request.urlopen(req, timeout=40); print(r.status, r.read().decode()[:4000])
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode()[:4000])
except Exception as e:
    print(0, str(e)[:200])
PY
}
code_of() { printf '%s' "${1%% *}"; }
body_of() { printf '%s' "${1#* }"; }
TWIN="http://s4t-twin-api:8892"
IOT="http://s4t-audit-proxy:8812/v1"

head_ "0. The pieces are in place"
S=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' s4t-twin-api 2>/dev/null || echo absent)
[ "$S" = "healthy" ] && ok "s4t-twin-api is healthy (it can reach the audit queue)" \
  || no "s4t-twin-api is '$S'" "dcs logs --tail 30 s4t-twin-api"
UI="http://localhost:${UI_EXTERNAL_PORT:-8088}/horizon/iot/iot_overview/"
CODE=$(curl -s -o /dev/null -w '%{http_code}' "$UI")
case "$CODE" in
  302|200) ok "the IoT Overview URL is routed (HTTP $CODE)" ;;
  404)     no "the IoT Overview URL is 404" \
              "the enabled file was not read: docker exec iotronic-ui apachectl -k graceful" ;;
  *)       void "Horizon answered HTTP $CODE" ;;
esac
LOG=$(curl -s -o /dev/null -w '%{http_code}' "${UI}board/00000000-0000-4000-8000-000000000000/log/")
case "$LOG" in
  302|200) ok "the Activity log page is routed (HTTP $LOG)" ;;
  *)       no "the Activity log page answered HTTP $LOG" "docker restart iotronic-ui" ;;
esac
OLD=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${UI_EXTERNAL_PORT:-8088}/horizon/iot/iot_control/")
[ "$OLD" = "404" ] && ok "the old Device Control panel is gone (merged into Overview)" \
  || no "the old Device Control URL still answers HTTP $OLD" \
        "remove the iot_control mounts from iotronic-ui and recreate it"
python3 scripts/test_twin_api.py >/dev/null 2>&1 && ok "twin service unit tests pass" \
  || no "twin service unit tests fail" "python3 scripts/test_twin_api.py"
python3 scripts/test_audit_vocabulary.py >/dev/null 2>&1 && ok "audit vocabulary tests pass" \
  || no "audit vocabulary tests fail" "python3 scripts/test_audit_vocabulary.py"

ADMIN_TOKEN=$(docker exec keystone sh -c 'openstack token issue -f value -c id' 2>/dev/null | tr -d '\r\n')
[ -n "$ADMIN_TOKEN" ] || void "no admin token" "the openstack CLI lives in the keystone container"
SEQ0=$(curl -s -H "$H" "$API/audit/stats" | jq_ "d['max_sequence'] or 0" 2>/dev/null || echo 0)

head_ "T6. The twin service refuses requests without a valid token"
ANY="00000000-0000-4000-8000-000000000000"
R=$(incall GET "$TWIN/twin/$ANY/status" "")
[ "$(code_of "$R")" = "401" ] && ok "no token: 401" || no "no token gave $(code_of "$R")" "$(body_of "$R")"
R=$(incall GET "$TWIN/twin/$ANY/status" "not-a-real-token")
[ "$(code_of "$R")" = "401" ] && ok "forged token: 401" || no "forged token gave $(code_of "$R")"

head_ "T5. A property outside the allow list is refused before Ditto"
if [ -z "$TWIN_BOARD" ] || [ -z "$ADMIN_TOKEN" ]; then
  void "set TWIN_BOARD to the mock board's uuid to run T4 and T5"
else
  R=$(incall POST "$TWIN/twin/$TWIN_BOARD/desired" "$ADMIN_TOKEN" '{"properties":{"temperature":99}}')
  [ "$(code_of "$R")" = "422" ] && ok "temperature (device owned) refused: 422" \
    || no "temperature gave $(code_of "$R")" "$(body_of "$R")"
  R=$(incall POST "$TWIN/twin/$TWIN_BOARD/desired" "$ADMIN_TOKEN" '{"properties":{"fan_on":"yes"}}')
  [ "$(code_of "$R")" = "422" ] && ok "fan_on=\"yes\" (wrong type) refused: 422" \
    || no "wrong type gave $(code_of "$R")" "$(body_of "$R")"
  R=$(incall GET "$TWIN/twin/$TWIN_BOARD/status" "$ADMIN_TOKEN")
  if [ "$(code_of "$R")" != "200" ]; then
    void "could not read the twin back ($(code_of "$R"))"
  elif body_of "$R" | jq_ "'temperature' in d.get('desired', {})" 2>/dev/null | grep -q True; then
    no "a refused value reached the twin's desired state"
  else
    ok "nothing refused reached the twin"
  fi
fi

head_ "T4. A desired value reaches the device and converges"
if [ -z "$TWIN_BOARD" ] || [ -z "$ADMIN_TOKEN" ]; then
  void "TWIN_BOARD not set"
else
  note "the mock board must be running:"
  note "  docker exec -it s4t-ditto-bridge python3 s4t_mock_board.py $TWIN_BOARD --host crossbar"
  R=$(incall POST "$TWIN/twin/$TWIN_BOARD/desired" "$ADMIN_TOKEN" '{"properties":{"fan_on":true}}')
  if [ "$(code_of "$R")" != "200" ]; then
    no "setting fan_on=true gave $(code_of "$R")" "$(body_of "$R")"
  else
    ok "fan_on=true accepted"
    STATE=""
    for _ in $(seq 1 20); do
      STATE=$(incall GET "$TWIN/twin/$TWIN_BOARD/status" "$ADMIN_TOKEN" | cut -d' ' -f2- \
              | jq_ "[p['state'] for p in d.get('properties',[]) if p['property']=='fan_on'][0]" 2>/dev/null)
      [ "$STATE" = "converged" ] && break
      sleep 3
    done
    [ "$STATE" = "converged" ] && ok "the device reported fan_on=true: converged" \
      || no "fan_on is '$STATE' after 60s" \
            "is the mock board running and the bridge healthy? check verify_stage_c.sh"
    R=$(incall DELETE "$TWIN/twin/$TWIN_BOARD/desired/fan_on" "$ADMIN_TOKEN")
    [ "$(code_of "$R")" = "200" ] && ok "desired fan_on cleared again" \
      || no "clearing gave $(code_of "$R")" "$(body_of "$R")"
  fi
fi

head_ "T1. Ping reaches Lightning-Rod"
if [ -z "$LR_BOARD" ] || [ -z "$ADMIN_TOKEN" ]; then
  void "set LR_BOARD to an online lr-sim board to run T1 and T2"
else
  R=$(incall POST "$IOT/boards/$LR_BOARD/action" "$ADMIN_TOKEN" '{"action":"DevicePing","parameters":{}}')
  case "$(code_of "$R")" in
    2*) ok "DevicePing answered $(code_of "$R")"; note "$(body_of "$R" | cut -c1-160)" ;;
    *)  no "DevicePing gave $(code_of "$R")" "$(body_of "$R")
        If 400, the conductor may expect a different body: record what it says
        and adjust ACTIONS in panels/iot_overview/views.py." ;;
  esac
fi

head_ "T2. Reboot, and the board comes back"
if [ -z "$LR_BOARD" ] || [ "$RUN_REBOOT" != "1" ]; then
  void "skipped: set RUN_REBOOT=1 (and LR_BOARD) to reboot that board"
else
  R=$(incall POST "$IOT/boards/$LR_BOARD/action" "$ADMIN_TOKEN" '{"action":"DeviceReboot","parameters":{}}')
  case "$(code_of "$R")" in
    2??) GO=1 ;;
    *)   GO=0; no "DeviceReboot gave '$(code_of "$R")'" "$(body_of "$R")" ;;
  esac
  if [ "$GO" = "1" ]; then
    ok "DeviceReboot accepted"
    ST=""; WENT=0
    for _ in $(seq 1 60); do
      ST=$(incall GET "$IOT/boards/$LR_BOARD" "$ADMIN_TOKEN" | cut -d' ' -f2- | jq_ "d.get('status')" 2>/dev/null)
      [ "$ST" != "online" ] && WENT=1
      [ "$WENT" = "1" ] && [ "$ST" = "online" ] && break
      sleep 3
    done
    [ "$WENT" = "1" ] && ok "the board went offline" \
      || note "never saw it offline (a simulated board may restart faster than the poll)"
    [ "$ST" = "online" ] && ok "the board is online again" \
      || no "the board is '$ST' after 3 minutes"
  fi
fi

head_ "T3. A non-admin user is refused"
if [ -z "${MEMBER_USER:-}" ] || [ -z "${MEMBER_PASS:-}" ]; then
  void "set MEMBER_USER and MEMBER_PASS (a Keystone user WITHOUT the admin role)"
else
  MT=$(docker exec -e U="$MEMBER_USER" -e P="$MEMBER_PASS" -e J="${MEMBER_PROJECT:-admin}" keystone \
       sh -c 'openstack --os-username "$U" --os-password "$P" --os-project-name "$J" token issue -f value -c id' \
       2>/dev/null | tr -d '\r\n')
  if [ -z "$MT" ]; then
    void "could not log in as $MEMBER_USER"
  else
    B="${TWIN_BOARD:-$ANY}"
    R=$(incall POST "$TWIN/twin/$B/desired" "$MT" '{"properties":{"fan_on":true}}')
    [ "$(code_of "$R")" = "403" ] && ok "twin write as $MEMBER_USER: 403" \
      || no "twin write as $MEMBER_USER gave $(code_of "$R")" "$(body_of "$R")"
    if [ -n "$LR_BOARD" ] && [ "$RUN_REBOOT" != "1" ]; then
      void "conductor policy for a non-admin reboot not tested" \
           "set RUN_REBOOT=1 too: if the policy is wrong, the board reboots"
    elif [ -n "$LR_BOARD" ]; then
      R=$(incall POST "$IOT/boards/$LR_BOARD/action" "$MT" '{"action":"DeviceReboot","parameters":{}}')
      case "$(code_of "$R")" in
        401|403) ok "reboot as $MEMBER_USER refused by the conductor: $(code_of "$R")" ;;
        2*)      no "the conductor ACCEPTED a reboot from $MEMBER_USER" \
                    "the panel still refuses it, but the conductor policy allows it" ;;
        *)       no "reboot as $MEMBER_USER gave $(code_of "$R")" "$(body_of "$R")" ;;
      esac
    fi
  fi
fi

head_ "T7. Each action is in the audit log, under a name, with its own verb"
sleep 4
# Only SUCCESSFUL actions count: T5's refusals are recorded as desired.set
# too, with outcome failure, and must not stand in for T4's real write.
SEEN=$(curl -s -H "$H" "$API/audit/events?from_seq=$SEQ0&actor_type=human&limit=500" \
       | jq_ "' '.join(sorted({i['machine_action']+'/'+str(i['operator_name']) for i in d['items'] if (i.get('action') or {}).get('outcome') == 'success'}))" 2>/dev/null)
note "recorded since the start of this run: ${SEEN:-nothing}"
want() {   # ACTION CONDITION_TEXT
  case " $SEEN " in
    *" $1/None "*) no "$1 recorded WITHOUT an operator name" ;;
    *" $1/"*)      ok "$1 recorded with an operator name" ;;
    *)             no "$1 missing from the audit log" "$2" ;;
  esac
}
if [ -n "$TWIN_BOARD" ]; then
  want desired.set "the twin service could not queue it, or T4 failed"
  want desired.clear "the twin service could not queue it, or T4 failed"
fi
[ -n "$LR_BOARD" ] && want device.ping "is the public iot endpoint the proxy? verify_audit_logger.sh check 1"
[ -n "$LR_BOARD" ] && [ "$RUN_REBOOT" = "1" ] && want device.reboot ""
[ -z "$TWIN_BOARD$LR_BOARD" ] && void "no action was run, so there is nothing to look for"

head_ "Result"
printf '  %d passed, %d failed, %d proved nothing\n\n' "$PASS" "$FAIL" "$VOID"
note "T8 (removal) is manual: drop the s4t-twin-api service and check that Overview"
note "still loads, with its twin actions reporting the service unreachable."
[ "$FAIL" -eq 0 ] || exit 1
