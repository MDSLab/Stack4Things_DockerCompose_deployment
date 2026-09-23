#!/usr/bin/env python3
"""
test_dashboard_join.py - the dashboard's logic, without a database or a network.

WHAT IS WORTH TESTING HERE
--------------------------
Not that the three reads work. Those are one SQL query, one HTTP call and one
SELECT, and they either connect or they do not. What is worth testing is the
JOIN, because that is where the screen can be quietly wrong: a board silently
dropped because its twin is missing, a stale reading shown as current, or an
agent reported as connected on no evidence.

Every case below is one of those.

    python3 scripts/test_dashboard_join.py
    python3 scripts/test_dashboard_join.py -v
"""

import importlib.util
import pathlib
import sys
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("d", HERE / "s4t_dashboard.py")
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)

NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)
def ago(seconds):
    return (NOW - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

CFG = d.Config({"DITTO_NAMESPACE": "s4t", "DASHBOARD_STALE_SECONDS": "120"})

PASS = FAIL = 0
VERBOSE = "-v" in sys.argv

def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        if VERBOSE:
            print("  ok    %s" % label)
    else:
        FAIL += 1
        print("  FAIL  %s\n          got  %r\n          want %r" % (label, got, want))


# Status vocabulary as observed in iotronic.boards: 'registered' before an
# agent has ever connected, then 'online' and 'offline'. 'operative' is
# Lightning Rod's own word for its internal state and never appears here.
BOARDS = [
    {"uuid": "aaa", "name": "Camera_01", "status": "online",
     "type": "gateway", "fleet": None, "lr_version": "0.4.17"},
    {"uuid": "bbb", "name": "Camera_02", "status": "registered",
     "type": "gateway", "fleet": None, "lr_version": None},
    {"uuid": "ccc", "name": "Orphan_03", "status": "registered",
     "type": "gateway", "fleet": None, "lr_version": None},
]

THINGS = [
    {"thingId": "s4t:aaa", "_revision": 412, "_modified": ago(5),
     "features": {"telemetry": {"properties": {"temperature": 21.5},
                                "desiredProperties": {"fan_on": True}}}},
    {"thingId": "s4t:bbb", "_revision": 8, "_modified": ago(900),
     "features": {"telemetry": {"properties": {"temperature": 19.0}}}},
    # a twin in another namespace must not be joined onto our boards
    {"thingId": "other:aaa", "_revision": 1, "_modified": ago(1),
     "features": {"telemetry": {"properties": {"temperature": -273}}}},
]

AUDIT = {
    "aaa": {"count": 318, "last_event": ago(5), "last_action": "telemetry.report",
            "last_operator": None, "operator_count": 0},
    "bbb": {"count": 4, "last_event": ago(900), "last_action": "board.update",
            "last_operator": "admin", "operator_count": 1},
}

rows = d.join(BOARDS, THINGS, AUDIT, CFG, now=NOW)
by_uuid = {r["uuid"]: r for r in rows}

print("join: every board survives")
# A board whose twin is missing is the single most important thing on this
# screen. Dropping it would turn a visible fault into an invisible one.
check("all three boards present", len(rows), 3)
check("the board with no twin is still listed", "ccc" in by_uuid, True)
check("its twin is marked absent", by_uuid["ccc"]["twin"]["present"], False)

print("join: namespace is respected")
# Both s4t:aaa and other:aaa exist. Taking the wrong one would show a
# plausible-looking temperature from a completely different system.
check("s4t twin wins over the foreign one",
      by_uuid["aaa"]["twin"]["telemetry"], {"temperature": 21.5})

print("staleness")
check("recent reading is not stale", by_uuid["aaa"]["twin"]["stale"], False)
check("old reading is stale",        by_uuid["bbb"]["twin"]["stale"], True)
check("age is computed",             round(by_uuid["aaa"]["twin"]["age_seconds"]), 5)
# No timestamp means no verdict. Reporting "fresh" here would be a claim made
# on no evidence, which is the failure this project keeps finding.
no_ts = d.twin_view({"thingId": "s4t:zzz", "_revision": 1}, now=NOW)
check("no timestamp gives no staleness verdict", no_ts["stale"], None)

print("agent state: ever seen and online are different claims")
# lr_version can only be written by software running on the device, so it is
# strong evidence that an agent connected AT SOME POINT. It says nothing about
# now. The first version of this screen counted versions as live agents and
# reported an offline board as connected.
check("agent seen when it reported a version", by_uuid["aaa"]["agent_seen"], True)
check("not seen when it never did",            by_uuid["bbb"]["agent_seen"], False)
check("online follows status, not lr_version", by_uuid["aaa"]["online"], True)
offline = d.join([{"uuid": "ddd", "name": "Camera_03", "status": "offline",
                   "lr_version": "0.4.17"}], [], {}, CFG, now=NOW)[0]
check("a board with an agent but offline is not online", offline["online"], False)
check("...and is still recorded as having been seen", offline["agent_seen"], True)

print("ages are readable")
check("seconds",  d.human_age(45), "45s")
check("minutes",  d.human_age(300), "5m")
check("hours",    d.human_age(7200), "2h")
check("days",     d.human_age(2081406), "24d")
check("no value", d.human_age(None), None)

print("audit summary")
check("counts carried through",   by_uuid["aaa"]["audit"]["count"], 318)
check("operator carried through", by_uuid["bbb"]["audit"]["last_operator"], "admin")
check("board with no history gets zeros",
      by_uuid["ccc"]["audit"]["count"], 0)

print("degraded inputs do not crash the screen")
check("no twins at all", len(d.join(BOARDS, [], {}, CFG, now=NOW)), 3)
check("no boards at all", d.join([], THINGS, AUDIT, CFG, now=NOW), [])
check("None instead of lists", d.join(None, None, {}, CFG, now=NOW), [])
malformed = d.join(BOARDS, [{"no_thing_id": True}, {"thingId": "broken"}],
                   {}, CFG, now=NOW)
check("malformed twins are skipped, boards survive", len(malformed), 3)

print("health is reported as freshness, not as up or down")
h = d.summarise_health(rows, {"count": 322, "max_sequence": 322,
                              "last_timestamp": ago(5)},
                       {"ditto": 5.0, "horizon": 900.0})
check("one row per path", len(h), 4)
check("provisioner row counts twins",
      h[2]["detail"], "2 of 3 boards have a twin")

print("\n  %d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
