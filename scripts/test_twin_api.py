#!/usr/bin/env python3
"""
test_twin_api.py - the twin command service's decisions, checked.

Every rule that decides whether an operator's write reaches a device lives in a
pure function in s4t_twin_api.py, so it can be tested here with no Keystone, no
Ditto, no broker and no Docker:

1. Who may write          has_role, parse_roles
2. What may be written    validate_properties, check_value, rules_for
3. What a missing list means   load_allowlist (must refuse everything)
4. Has the device got there    property_status
5. What gets recorded      build_twin_record

    python3 scripts/test_twin_api.py
    python3 scripts/test_twin_api.py -v
"""

import importlib.util
import json
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("twin", HERE / "s4t_twin_api.py")
twin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(twin)

PASS = FAIL = 0
VERBOSE = "-v" in sys.argv
B = "65f0bbf4-37cb-47d3-84cb-1d7902e7c1ad"


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        if VERBOSE:
            print(f"  ok    {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")


# ---------------------------------------------------------------------------
print("1. who may write")
check("admin is a writer", twin.has_role(["admin", "member"], {"admin"}), True)
check("member is not", twin.has_role(["member"], {"admin"}), False)
check("no roles at all", twin.has_role([], {"admin"}), False)
check("empty requirement admits nobody", twin.has_role(["admin"], set()), False)
check("roles parsed from csv", twin.parse_roles(" admin, operator ,,"),
      {"admin", "operator"})
check("empty csv is nobody", twin.parse_roles(""), set())

# ---------------------------------------------------------------------------
print("2. what may be written")
RULES = {
    "fan_on":   {"type": "boolean"},
    "setpoint": {"type": "number", "min": 10, "max": 40},
    "speed":    {"type": "integer", "min": 0, "max": 3000},
    "mode":     {"type": "string", "enum": ["auto", "manual"]},
}
check("a boolean", twin.validate_properties({"fan_on": True}, RULES),
      ({"fan_on": True}, {}))
check("several at once", twin.validate_properties(
    {"fan_on": False, "setpoint": 22.5, "speed": 1400, "mode": "auto"}, RULES)[1], {})
check("1 is not a boolean",
      list(twin.validate_properties({"fan_on": 1}, RULES)[1]), ["fan_on"])
check("true is not an integer (bool is an int in Python)",
      list(twin.validate_properties({"speed": True}, RULES)[1]), ["speed"])
check("a float is not an integer",
      list(twin.validate_properties({"speed": 1400.5}, RULES)[1]), ["speed"])
check("below the minimum",
      twin.validate_properties({"setpoint": 9.9}, RULES)[1], {"setpoint": "must be at least 10"})
check("above the maximum",
      twin.validate_properties({"setpoint": 40.1}, RULES)[1], {"setpoint": "must be at most 40"})
check("NaN is not a number here",
      list(twin.validate_properties({"setpoint": float("nan")}, RULES)[1]), ["setpoint"])
check("infinity refused",
      list(twin.validate_properties({"setpoint": float("inf")}, RULES)[1]), ["setpoint"])
check("a uuid with a trailing newline is refused", twin.is_board_uuid(B + "\n"), False)
check("the edges are allowed",
      twin.validate_properties({"setpoint": 10, "speed": 3000}, RULES)[1], {})
check("outside the enum",
      list(twin.validate_properties({"mode": "turbo"}, RULES)[1]), ["mode"])
check("not on the allow list",
      twin.validate_properties({"temperature": 20}, RULES)[1],
      {"temperature": "is not on the allow list for this board"})
check("a name that could escape the JSON pointer",
      list(twin.validate_properties({"a/b": 1}, {"a/b": {"type": "integer"}})[1]), ["a/b"])
check("ALL OR NOTHING: one bad value refuses the good one too",
      twin.validate_properties({"fan_on": True, "setpoint": 99}, RULES)[0], {})
check("empty request refused",
      list(twin.validate_properties({}, RULES)[1]), ["properties"])
check("not an object refused",
      list(twin.validate_properties(["fan_on"], RULES)[1]), ["properties"])
check("a rule with no type refuses",
      list(twin.validate_properties({"x": 1}, {"x": {}})[1]), ["x"])

AL = {"default": {"fan_on": {"type": "boolean"}},
      "by_board_type": {"gateway": {"speed": {"type": "integer"}},
                        "sensor": {"fan_on": {"type": "string"}}}}
check("defaults for an unknown type", sorted(twin.rules_for(AL, "camera")), ["fan_on"])
check("a type adds properties", sorted(twin.rules_for(AL, "gateway")), ["fan_on", "speed"])
check("a type overrides a default", twin.rules_for(AL, "sensor")["fan_on"]["type"], "string")
check("no type, defaults only", sorted(twin.rules_for(AL, None)), ["fan_on"])
ALP = dict(AL, by_profile={"cnc_mill": {"run": {"type": "boolean"}, "mode": {"type": "string", "enum": ["auto"]}}})
check("a machine profile REPLACES the defaults",
      sorted(twin.rules_for(ALP, "gateway", "cnc_mill")), ["mode", "run"])
check("an unknown profile falls back to type and defaults",
      sorted(twin.rules_for(ALP, "gateway", "lathe")), ["fan_on", "speed"])
check("profile read from attributes/machine/profile",
      twin.profile_of({"machine": {"profile": "cnc_mill"}}), "cnc_mill")
check("no machine attribute, no profile", twin.profile_of({"boardType": "gateway"}), None)
check("a malformed machine attribute is ignored", twin.profile_of({"machine": "cnc"}), None)

# ---------------------------------------------------------------------------
print("3. a missing or broken allow list refuses everything")
empty = twin.load_allowlist("/nonexistent/allowlist.json")
check("missing file -> nothing allowed", twin.rules_for(empty, None), {})
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
    f.write("{ not json")
check("broken file -> nothing allowed", twin.rules_for(twin.load_allowlist(f.name), None), {})
os.unlink(f.name)
shipped = HERE.parent / "conf_twin" / "twin_allowlist.json"
if shipped.exists():
    check("the shipped allow list loads and allows fan_on",
          "fan_on" in twin.rules_for(twin.load_allowlist(str(shipped)), None), True)
    # every control the simulated mill accepts must be on its allow list, with
    # the same range, or the twin would offer values the machine refuses
    spec2 = importlib.util.spec_from_file_location("mm", HERE / "s4t_machine_model.py")
    mm = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(mm)
    cnc = twin.rules_for(twin.load_allowlist(str(shipped)), "gateway", "cnc_mill")
    check("cnc_mill allow list == the machine's controls", sorted(cnc), sorted(mm.CONTROLS))
    for name, (kind, lo, hi) in mm.CONTROLS.items():
        r = cnc.get(name, {})
        if kind == "int":
            check("range of " + name, (r.get("type"), r.get("min"), r.get("max")), ("integer", lo, hi))
        elif kind == "enum":
            check("values of " + name, sorted(r.get("enum") or []), sorted(lo))
        else:
            check("type of " + name, r.get("type"), "boolean")

# ---------------------------------------------------------------------------
print("4. has the device got there")
rows = twin.property_status(
    desired={"fan_on": True, "setpoint": 22, "mode": "auto", "speed": 1400},
    reported={"fan_on": True, "setpoint": 21, "speed": 1200},
    set_at={"fan_on": 100.0, "setpoint": 100.0, "speed": 10.0},
    now=130.0, timeout=60)
by = {r["property"]: r for r in rows}
check("matching value is converged", by["fan_on"]["state"], "converged")
check("converged has no pending age", by["fan_on"]["pending_seconds"], None)
check("30s and different is pending", by["setpoint"]["state"], "pending")
check("its age is reported", by["setpoint"]["pending_seconds"], 30.0)
check("120s and different is unconfirmed", by["speed"]["state"], "unconfirmed")
check("never reported, no age known -> pending, not unconfirmed",
      (by["mode"]["state"], by["mode"]["pending_seconds"], by["mode"]["has_reported"]),
      ("pending", None, False))
check("rows are sorted by name", [r["property"] for r in rows],
      ["fan_on", "mode", "setpoint", "speed"])
check("nothing desired, nothing to show",
      twin.property_status({}, {"fan_on": True}, {}, 0, 60), [])
check("false reported is a real value, not missing",
      twin.property_status({"fan_on": False}, {"fan_on": False}, {}, 0, 60)[0]["state"],
      "converged")

# ---------------------------------------------------------------------------
print("5. what gets recorded")
rec = twin.build_twin_record("alice", "p1", B, "Camera_03", "set",
                             {"properties": {"fan_on": True}}, 204, "cid-1")
check("action", rec["machine_action"], "desired.set")
check("a human, by name", (rec["actor"]["type"], rec["operator_name"]), ("human", "alice"))
check("the board", (rec["machine"]["id"], rec["machine_name"]), (B, "Camera_03"))
check("success on 2xx", rec["action"]["outcome"], "success")
check("correlation id carried", rec["correlation_id"], "cid-1")
check("source", rec["source"], "horizon")
rec = twin.build_twin_record("bob", "p1", B, None, "clear", {"property": "fan_on"},
                             404, "cid-2")
check("clear verb", rec["machine_action"], "desired.clear")
check("no name falls back to the uuid (NOT NULL in the store)", rec["machine_name"], B)
check("failure on 4xx", rec["action"]["outcome"], "failure")

# The audit logger validates operator records before storing them. A record
# this service builds must pass that same gate, or it would be dropped.
spec2 = importlib.util.spec_from_file_location("logger", HERE / "s4t_audit_logger.py")
logger = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(logger)
for verb in ("set", "clear"):
    r = twin.build_twin_record("alice", "p", B, "X", verb, {}, 200, "c")
    check(f"desired.{verb} passes the logger's validation",
          logger.record_from_operator_event(json.dumps(r).encode()) is not None, True)

check("refused NaN is stored as text, keeping the record valid JSON",
      json.dumps(twin.json_safe({"x": float("nan"), "y": 1}), allow_nan=False),
      '{"x": "nan", "y": 1}')
check("board uuid accepted", twin.is_board_uuid(B), True)
check("anything else refused", twin.is_board_uuid("../things"), False)

print(f"\n  {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
