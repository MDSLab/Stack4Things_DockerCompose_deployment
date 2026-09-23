#!/usr/bin/env bash
# verify_audit_logger.sh - decide, mechanically, whether stages L1 to L5 are done.
#
# The checks that decide it are the delivery checks: they put something in one
# end and look for it at the other. Seven separate components in this project
# have reported themselves as working while failing, so no check here trusts a
# status field on its own.
#
# COVERAGE. An earlier version of this suite exercised two of the nineteen
# actions the logger can produce: telemetry.report, and whatever human action
# happened to be lying around. It reported sixteen passes. Both classifiers end
# in a FALLBACK, so an unrecognised path is not an error, it is a quiet
# reclassification, and no delivery check could ever notice. Checks 3, 5 and 10
# exist to close that: 3 proves the mapping, 5 and 10 prove every mapped action
# actually survives the trip to the API.
#
#   ./scripts/verify_audit_logger.sh

set -u
cd "$(dirname "$0")/.." || exit 1
dcs() { docker compose -f docker-compose.yml -f docker-compose.ditto.yml "$@"; }
set -a; . ./.env; set +a

API="http://localhost:${AUDIT_API_EXTERNAL_PORT:-8890}"
H="Authorization: Bearer ${AUDIT_API_TOKEN:-}"
Q_IN="${AUDIT_SOURCE_QUEUE:-ditto.twin.events}"
Q_OP="${AUDIT_OPERATOR_QUEUE:-s4t.audit.ingest}"
Q_OUT="${AUDIT_QUEUE:-s4t.audit}"

DITTO="http://localhost:${DITTO_EXTERNAL_PORT:-8090}"
DAUTH="${DITTO_API_USER:-s4t}:${DITTO_API_PASSWORD:-}"
NS="${DITTO_NAMESPACE:-s4t}"
POLICY="${DITTO_POLICY:-s4t:fleet-unime-lab}"

PASS=0; FAIL=0; VOID=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
no()   { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); [ $# -gt 1 ] && printf '        %s\n' "$2"; }
void() { printf '  \033[33mVOID\033[0m  %s\n' "$1"; VOID=$((VOID+1)); [ $# -gt 1 ] && printf '        %s\n' "$2"; }
note() { printf '        %s\n' "$1"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }
api()  { curl -s -H "$H" "$API$1"; }
jq_()  { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

# distinct machine_action values for a query, sorted, space separated
actions_for() { api "$1" | jq_ "' '.join(sorted({i['machine_action'] for i in d['items']}))"; }
# names in EXPECT that are absent from SEEN
missing() {
  local want="$1" seen="$2" a out=""
  for a in $want; do case " $seen " in *" $a "*) ;; *) out="$out$a ";; esac; done
  printf '%s' "$out"
}

head_ "1. Containers and queues"
for c in s4t-audit-logger s4t-audit-proxy; do
  S=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$c" 2>/dev/null || echo absent)
  [ "$S" = "healthy" ] && ok "$c is healthy" \
    || no "$c is '$S'" "health means consuming, publishing and able to write"
done
for q in "$Q_IN" "$Q_OP" "$Q_OUT"; do
  docker exec rabbitmq rabbitmqctl list_queues name 2>/dev/null | grep -q "^$q" \
    && ok "queue $q exists" || no "queue $q missing"
done
# A handler that throws inside message.process(requeue=True) puts the message
# back and fails again, forever, while every health signal stays green. That
# happened on 10 Sep 2026 with a stale image, and check 1 passed throughout.
for q in "$Q_OP" "$Q_IN"; do
  DEPTH=$(docker exec rabbitmq rabbitmqctl list_queues name messages 2>/dev/null \
          | awk -v q="$q" '$1==q{print $2}')
  [ "${DEPTH:-0}" -lt 5 ] 2>/dev/null && ok "queue $q is drained (${DEPTH:-0} waiting)" \
    || no "queue $q is holding ${DEPTH:-?} message(s)" \
          "the logger is not consuming them; look for a requeue loop in its log"
done
# The catalog entry is what actually puts the proxy in Horizon's path. When it
# is wrong the proxy still runs, still reports healthy, and records nothing at
# all, which is indistinguishable from a quiet system. Keystone sets it from
# IOT_PUBLIC_ENDPOINT_URL at ITS start, so a mismatch usually means keystone
# has not been recreated since the audit overlay was added.
CATURL=$(docker exec keystone sh -c \
  'openstack endpoint list --service iot --interface public -f value -c URL' \
  2>/dev/null | tr -d '\r\n')
case "$CATURL" in
  *s4t-audit-proxy*) ok "the public iot endpoint points at the proxy" ;;
  "")  void "could not read the service catalog" "is the keystone container up?" ;;
  *)   no "the public iot endpoint is $CATURL" \
          "Horizon bypasses the proxy, so no operator action can be recorded.
        Fix: dcs up -d --force-recreate keystone, then check its log for
        'Riconciliazione endpoint pubblico iot'" ;;
esac

head_ "2. The API answers and is read only"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$API/health")" = "200" ] \
  && ok "/health responds" || no "/health does not respond"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$API/audit/events")" = "401" ] \
  && ok "T12: unauthenticated read is refused" || no "T12: the API is not authenticated"
[ "$(curl -s -o /dev/null -w '%{http_code}' -H "$H" -X POST "$API/audit/events")" = "405" ] \
  && ok "T12: POST is rejected, the API is read only" || no "T12: a write verb was accepted"

head_ "3. T4a. The action vocabulary matches the published table"
note "pure functions, no broker and no database, so this runs in a second"
# This is the only check that can see a MISCLASSIFICATION. Everything below
# proves records arrive; none of it can prove they arrived under the right
# verb, because a wrong verb is still a valid record. Writing this test found
# that DELETE /v1/boards/<id>/plugins/<id> was being recorded as board.delete.
if python3 scripts/test_audit_vocabulary.py >/tmp/vocab.$$ 2>&1; then
  ok "$(tail -1 /tmp/vocab.$$ | tr -s ' ')"
  note "covers all 19 verbs plus the api.<method> fallback"
else
  no "the vocabulary test failed" "$(grep -m3 FAIL /tmp/vocab.$$ | tr '\n' ' ')"
  note "run: python3 scripts/test_audit_vocabulary.py -v"
fi
rm -f /tmp/vocab.$$

head_ "4. T1 DELIVERY. Device activity becomes a queryable record"
note "runs a simulated device, then looks for its records through the API"
UUID=$(docker exec iotronic-db mariadb -uroot -p"$MYSQL_ROOT_PASSWORD" -N -B \
       -e "SELECT uuid FROM iotronic.boards WHERE uuid IS NOT NULL LIMIT 1;" 2>/dev/null | tr -d '[:space:]')
if [ -z "$UUID" ]; then
  void "no board to test with" "register one in Horizon, then re-run"
else
  BEFORE=$(api "/audit/stats" | jq_ "d['count']")
  MOCK=$(docker exec s4t-ditto-bridge timeout 14 python3 s4t_mock_board.py \
           "$UUID" --host crossbar --interval 2 2>&1)
  sleep 3
  AFTER=$(api "/audit/stats" | jq_ "d['count']")
  if [ "$AFTER" -gt "$BEFORE" ]; then
    ok "$(( AFTER - BEFORE )) record(s) captured and queryable"
  else
    no "no records were captured"
    grep -q "joined realm" <<<"$MOCK" || note "the mock board never joined WAMP"
  fi
fi

head_ "5. T4b DELIVERY. Every TWIN action reaches the log"
note "one probe twin is driven through all seven twin actions, then deleted"
# Each change is made over HTTP, deliberately NOT through the ingest
# connection, because Ditto suppresses an event on the connection that caused
# it. Driving these as telemetry would test nothing and look like it passed.
#
# prediction.write is written by hand here. Stage D does not exist yet, so
# this case is the only thing keeping that verb honest until it does.
PNAME="auditprobe$$"
PROBE="$NS:$PNAME"
DPUT_ERRS=""
# The status code is KEPT. The first version of this sent it to /dev/null, so a
# 404 from Ditto and an event that never arrived produced the identical
# symptom: a missing action with no explanation. That is the same fault this
# project already fixed once in verify_stage_c.sh. Never discard the output of
# the thing whose behaviour you are testing.
dput() {
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' -u "$DAUTH" \
           -X PUT "$DITTO/api/2/things/$PROBE$1" \
           -H 'Content-Type: application/json' -d "$2")
  case "$code" in 2*) ;; *) DPUT_ERRS="$DPUT_ERRS${1:-/}=$code ";; esac
}

dput "" "{\"policyId\":\"$POLICY\",\"features\":{\"telemetry\":{\"properties\":{}}}}"
sleep 1
dput "/features/telemetry/properties"        '{"temperature":21.5}'
dput "/features/telemetry/desiredProperties" '{"fan_on":true}'
# Address the FEATURE, not its properties. Ditto does not create a feature
# implicitly when you write a subresource of one that does not exist, so
# PUT /features/prediction/properties returns 404 on a fresh twin and emits
# nothing at all.
dput "/features/prediction"                  '{"properties":{"eta_minutes":12}}'
dput "/attributes/site"                      '"verification"'
dput "/features/health"                      '{"properties":{"uptime_s":1}}'
sleep 1
DEL=$(curl -s -o /dev/null -w '%{http_code}' -u "$DAUTH" \
        -X DELETE "$DITTO/api/2/things/$PROBE")
case "$DEL" in 2*) ;; *) DPUT_ERRS="$DPUT_ERRS DELETE=$DEL ";; esac

TWIN_EXPECT="desired.set prediction.write telemetry.report twin.create twin.delete twin.modify twin.update"
TWIN_SEEN=""
for _ in $(seq 1 12); do
  TWIN_SEEN=$(actions_for "/audit/events?machine_id=$PNAME&limit=200")
  [ "$TWIN_SEEN" = "$TWIN_EXPECT" ] && break
  sleep 2
done
if [ "$TWIN_SEEN" = "$TWIN_EXPECT" ]; then
  ok "all 7 twin actions recorded: $TWIN_SEEN"
elif [ -z "$TWIN_SEEN" ]; then
  no "the probe twin produced no records at all" \
     "the s4t-events connection is not delivering. That is the stage L1 fault
        returning: check it declares NO sources, or Ditto suppresses its own events."
  [ -n "$DPUT_ERRS" ] && note "but first: Ditto refused these writes: $DPUT_ERRS"
else
  no "missing twin action(s): $(missing "$TWIN_EXPECT" "$TWIN_SEEN")" "seen: $TWIN_SEEN"
  if [ -n "$DPUT_ERRS" ]; then
    note "Ditto refused: $DPUT_ERRS"
    note "so this is the probe failing to write, not the logger failing to record"
  else
    note "every write was accepted, so the break is between Ditto and the logger"
  fi
fi
note "the probe twin is deleted; its audit records stay, which is the point"

head_ "6. T5. Device records carry a null operator"
BAD=$(api "/audit/events?machine_action=telemetry.report&limit=200" \
      | jq_ "sum(1 for i in d['items'] if i['operator_name'] is not None)")
[ "$BAD" = "0" ] && ok "no telemetry record claims a human operator" \
  || no "$BAD telemetry record(s) have a non null operator_name" \
        "human and machine activity must stay distinguishable"

head_ "7. T6. The sequence is gapless"
GAPS=$(api "/audit/events?from_seq=0&limit=10000" | python3 -c "
import json,sys
s=[i['sequence'] for i in json.load(sys.stdin)['items']]
print(0 if not s else sum(1 for a,b in zip(s,s[1:]) if b!=a+1))")
[ "$GAPS" = "0" ] && ok "no gaps in the returned sequence" \
  || no "$GAPS gap(s) found" "a gap means records were lost, which is what it is for"

head_ "8. T8. Incremental pull returns everything exactly once"
note "small page size on purpose, so the cursor has to advance more than once"
# With limit=500 and a few hundred records this completed in ONE page, which
# proves a query returns unique rows and proves nothing about the cursor. The
# claim being tested is that following next_seq across pages loses nothing and
# repeats nothing, so the page size is deliberately small.
RES=$(python3 - <<PY
import json, urllib.request
seen, cur, pages = [], 0, 0
while True:
    req = urllib.request.Request(f"$API/audit/events?from_seq={cur}&limit=25",
                                 headers={"Authorization": "Bearer ${AUDIT_API_TOKEN:-}"})
    d = json.load(urllib.request.urlopen(req)); pages += 1
    seen += [i["sequence"] for i in d["items"]]; cur = d["next_seq"]
    if not d["has_more"] or pages > 200: break
print(f"{len(seen)} {len(set(seen))} {pages}")
PY
)
set -- $RES
if [ "$1" != "$2" ]; then
  no "T8: $1 records but only $2 unique" "pagination is returning records twice"
elif [ "${3:-1}" -lt 2 ]; then
  void "T8: $1 record(s) fitted in one page" \
       "the cursor never had to advance, so paging is untested. Generate more
        records, or lower the page size further."
else
  ok "T8: $1 records over $3 pages, no duplicates, no gaps"
fi

head_ "9. T7 DELIVERY. The queue and the API agree byte for byte"
note "this is what protects the ledger developer's hashes"
BYTES=$(python3 - <<PY
import json, urllib.request
req = urllib.request.Request("$API/audit/events?limit=1",
                             headers={"Authorization": "Bearer ${AUDIT_API_TOKEN:-}"})
d = json.load(urllib.request.urlopen(req))
if not d["items"]: print("NONE"); raise SystemExit
eid = d["items"][0]["event_id"]
req = urllib.request.Request(f"$API/audit/events/{eid}",
                             headers={"Authorization": "Bearer ${AUDIT_API_TOKEN:-}"})
single = urllib.request.urlopen(req).read().decode()
canon = json.dumps(json.loads(single), separators=(",",":"), sort_keys=True)
print("SAME" if single == canon else "DIFF")
PY
)
case "$BYTES" in
  SAME) ok "single record fetch is canonical and reproducible" ;;
  NONE) void "no records to compare" "run check 4 first" ;;
  *)    no "the API is not returning canonical bytes" \
           "his hashes will not match between transports" ;;
esac

head_ "10. T4c DELIVERY. Every OPERATOR action reaches the log"
note "ten request shapes are driven through the proxy with a real Keystone token"
# SAFETY. Every identifier below is randomly generated, so the conductor
# answers 404 or 400 and nothing real is created, changed or deleted. A
# REJECTED action is still an action and is still recorded, with
# outcome=failure, which is precisely what makes this safe to run repeatedly.
#
# Reads are absent on purpose. The proxy does not record GET unless
# AUDIT_RECORD_READS=1, because Horizon generates thousands of page loads and
# they would bury the operator actions. board.read, plugin.read and
# service.read are therefore covered by check 3 alone, and that is a deliberate
# limit rather than an oversight.
TOKEN=$(docker exec keystone sh -c 'openstack token issue -f value -c id' 2>/dev/null | tr -d '\r\n')
MAXBEFORE=$(api "/audit/stats" | jq_ "d['max_sequence'] or 0")
if [ -z "$TOKEN" ]; then
  void "could not obtain a Keystone token" \
       "the openstack CLI lives inside the keystone container, not on the host"
else
  docker exec -i -e TOK="$TOKEN" s4t-audit-logger python3 - >/dev/null 2>&1 <<'PY'
import os, urllib.request, uuid
B = "http://s4t-audit-proxy:8812"
u1, u2 = str(uuid.uuid4()), str(uuid.uuid4())      # nothing real, by construction
CALLS = [
    ("POST",   "/v1/boards"),                       # board.create
    ("PATCH",  f"/v1/boards/{u1}"),                 # board.update
    ("DELETE", f"/v1/boards/{u1}"),                 # board.delete
    ("POST",   f"/v1/boards/{u1}/actions"),         # board.action
    ("POST",   f"/v1/boards/{u1}/plugins"),         # plugin.inject
    ("PUT",    f"/v1/plugins/{u2}/action"),         # plugin.execute
    ("DELETE", f"/v1/boards/{u1}/plugins/{u2}"),    # plugin.remove
    ("POST",   f"/v1/boards/{u1}/services"),        # service.expose
    ("DELETE", f"/v1/boards/{u1}/services/{u2}"),   # service.remove
    ("POST",   "/v1/audit-verification-probe"),     # api.post, the fallback
]
for method, path in CALLS:
    body = b"{}" if method in ("POST", "PUT", "PATCH") else None
    req = urllib.request.Request(
        B + path, data=body, method=method,
        headers={"X-Auth-Token": os.environ["TOK"],
                 "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass                                        # a rejection is still recorded
PY
  OP_EXPECT="api.post board.action board.create board.delete board.update plugin.execute plugin.inject plugin.remove service.expose service.remove"
  OP_SEEN=""
  for _ in $(seq 1 10); do
    OP_SEEN=$(actions_for "/audit/events?from_seq=$MAXBEFORE&actor_type=human&limit=200")
    [ "$OP_SEEN" = "$OP_EXPECT" ] && break
    sleep 2
  done
  if [ "$OP_SEEN" = "$OP_EXPECT" ]; then
    ok "all 10 operator actions recorded: $OP_SEEN"
  elif [ -z "$OP_SEEN" ]; then
    no "not one request was recorded" \
       "the proxy log says whether it saw them: dcs logs --tail 30 s4t-audit-proxy"
  else
    no "missing operator action(s): $(missing "$OP_EXPECT" "$OP_SEEN")" "seen: $OP_SEEN"
  fi
fi

head_ "11. T2/T3 DELIVERY. Operator identity"
if docker exec rabbitmq rabbitmqctl list_queues name messages 2>/dev/null | grep -q "^$Q_OP"; then
  HUMANS=$(api "/audit/events?actor_type=human&limit=500" | jq_ "d['count']")
  NAMES=$(api "/audit/events?actor_type=human&limit=500" \
          | jq_ "' '.join(sorted({str(i['operator_name']) for i in d['items']}))")
  if [ "${HUMANS:-0}" -eq 0 ]; then
    void "no operator records at all" \
         "check 10 should have produced some. Either the catalog endpoint does not
          point at the proxy, or the proxy is not reaching the ingest queue."
  elif [ "$NAMES" = "None" ]; then
    no "operator records exist but every operator_name is null" \
       "Keystone is not resolving the token, so nothing is actually attributed.
        The proxy log line reads 'recorded ... by None'."
  else
    ok "$HUMANS operator record(s) captured, operators: $NAMES"
    case "$NAMES" in
      *" "*) ok "T3: two or more distinct operators, identity is resolved per request" ;;
      *)     note "T3 needs a SECOND Keystone user. One name proves the field is
        populated; two prove it is resolved per request rather than hardcoded." ;;
    esac
  fi
else
  no "the ingest queue does not exist" "the proxy has not started"
fi

head_ "12. Realtime. SSE and WebSocket deliver live"
note "run inside the container, which has aiohttp, rather than on the host"

# The first version of this check ran on the host and reported VOID whenever
# aiohttp was missing there. That is the test apparatus failing, not the
# system, and reporting it as 'no evidence' hides a real capability. The
# container already carries aiohttp, so the test belongs in there. Same reason
# the mock board runs inside the bridge container.
cat > /tmp/rt_probe.py <<'PROBE'
import asyncio, json, os, sys, aiohttp

API = "http://127.0.0.1:8890"
H = {"Authorization": "Bearer " + os.environ.get("TOKEN", "")}

async def main():
    result = {}
    async with aiohttp.ClientSession(headers=H) as s:
        # SSE: replay from 0 proves the transport carries real records
        try:
            async with s.get(API + "/audit/stream?from_seq=0") as r:
                chunk = await asyncio.wait_for(r.content.readuntil(b"\n\n"), timeout=6)
                txt = chunk.decode()
                data = [l[6:] for l in txt.split("\n") if l.startswith("data: ")]
                result["sse"] = "ok" if data and json.loads(data[0]).get("sequence") else "empty"
        except Exception as e:
            result["sse"] = "fail:" + type(e).__name__
        # WebSocket
        try:
            async with s.ws_connect(API + "/audit/ws?from_seq=0") as ws:
                m = await asyncio.wait_for(ws.receive(), timeout=6)
                result["ws"] = "ok" if m.data and json.loads(m.data).get("sequence") else "empty"
        except Exception as e:
            result["ws"] = "fail:" + type(e).__name__
    print(json.dumps(result))

asyncio.run(main())
PROBE
docker cp /tmp/rt_probe.py s4t-audit-logger:/tmp/rt_probe.py >/dev/null 2>&1
RT=$(docker exec -e TOKEN="${AUDIT_API_TOKEN:-}" s4t-audit-logger \
       python3 /tmp/rt_probe.py 2>/dev/null)
rm -f /tmp/rt_probe.py

SSE=$(echo "$RT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('sse','?'))" 2>/dev/null)
WS=$(echo  "$RT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('ws','?'))"  2>/dev/null)

[ "$SSE" = "ok" ] && ok "SSE delivered a record with a sequence" \
  || no "SSE did not deliver (${SSE:-no result})"
[ "$WS" = "ok" ] && ok "WebSocket delivered a record with a sequence" \
  || no "WebSocket did not deliver (${WS:-no result})"

head_ "13. T17. No credentials in the store"
LEAK=$(api "/audit/events?limit=500" | python3 -c "
import json,sys,re
raw=sys.stdin.read()
print(sum(1 for p in ('X-Auth-Token','x-auth-token','password','\"token\"') if p in raw))")
[ "$LEAK" = "0" ] && ok "no token or password string found in the records" \
  || no "$LEAK suspicious string(s) in the stored records" "a leaked token would be an incident"

head_ "14. Dashboard. Boards are joined to their twins"
note "fetched from inside iotronic-ui, which is the container that will call it"
# Asking the aggregator whether it answers proves nothing: it answers happily
# with an empty list when the database is unreachable. The claim being tested
# is that the JOIN produced something, so the check counts joined rows and
# VOIDs when there are no boards to join.
if ! docker inspect s4t-audit-logger >/dev/null 2>&1; then
  no "s4t-audit-logger is not running"
elif ! docker inspect iotronic-ui >/dev/null 2>&1; then
  void "iotronic-ui is not running" "the panel's own container is the honest client"
else
  DASH=$(docker exec iotronic-ui python -c "
import json, urllib2
try:
    d = json.load(urllib2.urlopen('http://s4t-audit-logger:8891/data', timeout=8))
except Exception as e:
    print 'ERR %s' % e
else:
    b = d.get('boards') or []
    print '%d %d %d %s' % (
        len(b),
        sum(1 for x in b if x['twin']['present']),
        sum(1 for x in b if (x['audit'] or {}).get('count')),
        ','.join(sorted(d.get('errors') or {})) or '-')
" 2>&1 | tail -1)
  set -- $DASH
  if [ "$1" = "ERR" ]; then
    no "the panel's container cannot reach the aggregator" "$DASH"
    note "is DASHBOARD_PORT set on s4t-audit-logger? check its log for 'dashboard'"
  elif [ "${1:-0}" -eq 0 ]; then
    void "the aggregator answered with no boards" \
         "register a board, or check the ${4:-} read that failed"
  else
    ok "$1 board(s), $2 with twin state, $3 with audit history"
    [ "${4:-}" = "-" ] || no "some sources failed: $4" "partial data is on screen"
  fi
fi

head_ "15. Dashboard. The panel is registered and renders"
note "asks Horizon itself, rather than importing the module out of context"
# The first version of this check ran `python -c "import ...panel"` inside the
# container. That is not the question. A Horizon panel is imported by Django
# with settings configured; a bare interpreter is a different environment, and
# the control experiment proved it: the working `boards` panel imported fine
# standalone while this one did not, at the same moment Apache was routing
# requests to it successfully. The import said nothing about the panel.
#
# These two ask the running system instead.
if ! docker inspect iotronic-ui >/dev/null 2>&1; then
  void "iotronic-ui is not running"
else
  UI="http://localhost:${UI_EXTERNAL_PORT:-8088}/horizon/iot/iot_overview/"
  CODE=$(curl -s -o /dev/null -w '%{http_code}' "$UI")
  case "$CODE" in
    # 302 is the login redirect. Reaching it proves the URL is routed, which
    # only happens when the panel is registered. No session needed.
    302|200) ok "the panel URL is routed (HTTP $CODE), so it is registered" ;;
    404)     no "the panel URL is 404" \
                "the enabled file was not read. Restart Apache:
        docker exec iotronic-ui apachectl -k graceful" ;;
    000)     void "Horizon did not answer on port ${UI_EXTERNAL_PORT:-8088}" ;;
    *)       no "the panel URL returned HTTP $CODE" ;;
  esac

  # A 500 during render happens AFTER routing, so the check above cannot see
  # it. The Apache log can. TemplateDoesNotExist lives here.
  ERRS=$(docker exec iotronic-ui sh -c \
          'tail -400 /var/log/apache2/error.log 2>/dev/null' \
         | grep -c "iot_overview" 2>/dev/null)
  TRACE=$(docker exec iotronic-ui sh -c \
          'tail -400 /var/log/apache2/error.log 2>/dev/null' \
         | grep -iE "TemplateDoesNotExist|NoReverseMatch|Internal Server Error: /horizon/iot/iot_overview" \
         | tail -1)
  if [ -z "$TRACE" ]; then
    ok "no render errors for the panel in the last 400 log lines"
  else
    no "the panel is routed but fails to render" "${TRACE##*] }"
  fi
  note "the sidebar itself needs a human: open Horizon, IoT, Overview"
fi

head_ "Result"
printf '  %d passed, %d failed, %d proved nothing\n\n' "$PASS" "$FAIL" "$VOID"
if [ "$FAIL" -eq 0 ] && [ "$VOID" -gt 0 ]; then
  printf '  NOT done. Nothing is broken, but %d check(s) gathered no evidence.\n' "$VOID"
  printf '  An idle system passes almost any test.\n'; exit 2
fi
if [ "$FAIL" -eq 0 ]; then
  printf '  Stages L1 to L5 are done. All 19 actions are exercised: 7 twin actions\n'
  printf '  end to end, 10 operator actions end to end, and the 3 read actions in\n'
  printf '  the vocabulary test, since the proxy does not record reads by default.\n'
  printf '  Remaining by hand: T13 the failure mode, T14 the backlog, T15 the\n'
  printf '  sustained rate.\n'; exit 0
fi
printf '  NOT done. Fix the failures above and re-run.\n'; exit 1
