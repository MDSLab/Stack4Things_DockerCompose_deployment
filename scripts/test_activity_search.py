#!/usr/bin/env python3
"""
test_activity_search.py - the Overview's activity search, checked.

Runs the real query builder against a real SQLite database built with the
audit logger's own schema, so the SQL, the LIKE escaping and json_extract are
all exercised. No broker, no network, no Docker.

    python3 scripts/test_activity_search.py
    python3 scripts/test_activity_search.py -v
"""

import importlib.util
import json
import pathlib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dash = load("s4t_dashboard")
logger = load("s4t_audit_logger")

PASS = FAIL = 0
VERBOSE = "-v" in sys.argv


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        if VERBOSE:
            print(f"  ok    {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")


NOW = datetime(2026, 11, 1, 22, 0, 0, tzinfo=timezone.utc)
B, OTHER = "adf213c4-5c24-41c6-be78-bba5390ed893", "11111111-2222-4333-8444-555555555555"


class Store:
    def __init__(self):
        # as the real store: the aggregator reads it from a worker thread
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.executescript(logger.SCHEMA)
        self.n = 0

    def add(self, board, action, actor, minutes_ago, operator=None,
            outcome="success", detail=None):
        self.n += 1
        ts = (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        rec = {"event_id": f"e{self.n}", "timestamp": ts, "operator_name": operator,
               "machine_name": "Device 01", "machine_action": action,
               "actor": {"type": actor}, "machine": {"id": board},
               "action": {"outcome": outcome, "detail": detail or {}},
               "source": "test"}
        self.db.execute(
            "INSERT INTO audit_events (event_id, timestamp, operator_name, machine_name,"
            " machine_action, actor_type, machine_id, source, twin_revision, record)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rec["event_id"], ts, operator, "Device 01", action, actor, board, "test",
             None, logger.canonical(rec)))


st = Store()
# a week of telemetry every 10 minutes: 1008 records, the noise
for i in range(7 * 24 * 6):
    st.add(B, "telemetry.report", "device", 10 * i + 1, detail={"temperature": 21})
st.add(B, "device.ping", "human", 5, operator="admin")
st.add(B, "device.reboot", "human", 90, operator="bob", outcome="failure",
       detail={"action": "DeviceReboot"})
st.add(B, "desired.set", "human", 30, operator="admin", detail={"properties": {"fan_on": True}})
st.add(B, "desired.set", "unknown", 30, detail={"fan_on": True})
st.add(B, "plugin.start", "human", 60 * 30, operator="alice")
st.add(B, "twin.update", "system", 60 * 24 * 3)
st.add(B, "device.ping", "human", 2, operator="50%_off")   # an awkward name
st.add(OTHER, "device.reboot", "human", 1, operator="admin")


def run(**params):
    return dash.search_board_events(st, B, params, now=NOW)


def actions(res):
    return [e["machine_action"] for e in res["events"]]


print("paging")
r = run()
check("default page is 50", len(r["events"]), 50)
# newest = highest sequence, the order the log recorded them in
check("newest first", r["events"][0]["operator_name"], "50%_off")
check("sequences strictly descending",
      all(a["event_id"] != b["event_id"] for a, b in zip(r["events"], r["events"][1:])), True)
check("there is more", r["has_more"], True)
r2 = run(before_seq=r["next_before"])
check("next page starts strictly older",
      r2["events"][0]["event_id"] != r["events"][-1]["event_id"], True)
check("limit is capped at 200", len(run(limit=5000)["events"]), 200)
check("a nonsense limit falls back to 50", len(run(limit="lots")["events"]), 50)
check("a nonsense cursor is ignored", len(run(before_seq="x")["events"]), 50)

print("hide telemetry: what an operator actually wants to see")
r = run(hide_telemetry="1")
check("only the real actions are left",
      sorted(actions(r)), sorted(["device.ping", "device.reboot", "desired.set",
                                  "desired.set", "plugin.start", "twin.update",
                                  "device.ping"]))
check("and nothing more to page through", r["has_more"], False)
check("hide is ignored when you ASK for telemetry",
      len(run(category="telemetry", hide_telemetry="1")["events"]), 50)

print("categories")
check("commands", sorted(actions(run(category="commands"))),
      ["device.ping", "device.ping", "device.reboot"])
check("twin", actions(run(category="twin")), ["desired.set", "desired.set"])
check("software", actions(run(category="software")), ["plugin.start"])
check("system", actions(run(category="system")), ["twin.update"])
check("an unknown category filters nothing",
      len(run(category="'; DROP TABLE audit_events; --")["events"]), 50)

print("actor, outcome, operator")
check("people only", sorted(set(actions(run(actor="human")))),
      ["desired.set", "device.ping", "device.reboot", "plugin.start"])
check("failures only", actions(run(outcome="failure")), ["device.reboot"])
check("by operator", sorted(actions(run(operator="admin"))), ["desired.set", "device.ping"])
check("operators offered for this board only",
      run()["operators"], ["50%_off", "admin", "alice", "bob"])

print("time")
check("last hour, no telemetry",
      sorted(actions(run(since="1h", hide_telemetry="1"))),
      ["desired.set", "desired.set", "device.ping", "device.ping"])
check("last 24h includes the failed reboot",
      "device.reboot" in actions(run(since="24h", hide_telemetry="1")), True)
check("last 24h excludes the 3 day old twin update",
      "twin.update" in actions(run(since="24h", hide_telemetry="1")), False)

print("search")
check("matches the action", actions(run(q="reboot")), ["device.reboot"])
check("case insensitive", actions(run(q="REBOOT")), ["device.reboot"])
check("matches the operator", actions(run(q="alice")), ["plugin.start"])
check("matches inside the record (a property name)",
      sorted(actions(run(q="fan_on"))), ["desired.set", "desired.set"])
check("matches the raw Lightning-Rod name in the detail",
      actions(run(q="DeviceReboot")), ["device.reboot"])
check("% is literal, not a wildcard", actions(run(q="50%")), ["device.ping"])
check("_ is literal, not a wildcard", actions(run(q="%_o")), ["device.ping"])
check("quotes cannot break the query", actions(run(q="' OR 1=1 --")), [])
check("search is combined with filters",
      actions(run(q="device", outcome="failure")), ["device.reboot"])
check("another board's records never appear",
      [e["machine"]["id"] for e in run(q="reboot")["events"]], [B])

print(f"\n  {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
