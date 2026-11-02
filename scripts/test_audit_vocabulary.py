#!/usr/bin/env python3
"""
test_audit_vocabulary.py - every action the logger can produce, checked.

WHY THIS EXISTS
---------------
The audit log publishes a CLOSED vocabulary of `machine_action` values, and the
ledger developer filters on it. `docs/Audit_API_Reference.md` prints that
vocabulary as a table. Nothing kept the table and the code in agreement, and
nothing proved that every value in it was even reachable.

That gap is worse than it sounds, because both classifiers END IN A FALLBACK.
`classify()` returns `twin.modify` for anything it does not recognise and
`classify_request()` returns `api.<method>`. So a wrong path, a renamed API
route or a reordered branch does not raise, does not drop the record and does
not fail any delivery check. It quietly reclassifies. The log stays complete
and the vocabulary degrades into the fallback, which is exactly the failure
shape this project has now hit seven times: the thing keeps reporting success
while doing the wrong work.

Writing this test immediately found one. `classify_request()` tested "/boards"
before "/plugins", and a plugin lives under a board, so
`DELETE /v1/boards/<id>/plugins/<id>` was classified `board.delete`. The audit
log would have recorded plugin removals as board deletions, with a valid
sequence, a real operator name and a plausible timestamp. That is a worse
outcome than losing the record.

WHAT IT CHECKS
--------------
1. Every case in the tables maps to the action it should.
2. COVERAGE: the union of the tables equals the published vocabulary, so a new
   verb cannot be added to the code without a case, and a verb cannot be
   deleted from the code while the documentation still advertises it.

Pure functions only. No broker, no database, no network, no Docker.

    python3 scripts/test_audit_vocabulary.py
    python3 scripts/test_audit_vocabulary.py -v
"""

import importlib.util
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent


def load(name):
    """Import a sibling script by path, so cwd does not matter."""
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


logger = load("s4t_audit_logger")
proxy = load("s4t_audit_proxy")

U1 = "65f0bbf4-37cb-47d3-84cb-1d7902e7c1ad"      # a board
U2 = "9a1c77de-0b42-4f21-b3ac-51ee0d6f8c10"      # a plugin or a service

# =========================================================================
# The published vocabulary. This is the contract in Audit_API_Reference.md.
# =========================================================================
TWIN_VOCAB = {
    "telemetry.report", "desired.set", "desired.clear", "prediction.write",
    "twin.create", "twin.update", "twin.modify", "twin.delete",
}

OPERATOR_VOCAB = {
    "board.create", "board.read", "board.update", "board.delete", "board.action",
    "plugin.inject", "plugin.execute", "plugin.remove", "plugin.read",
    "service.expose", "service.read", "service.remove", "service.restore",
    # Derived from the action named in the request body. These are the names
    # Lightning-Rod registers, read from its RPC log, not inferred.
    "device.ping", "device.echo", "device.reboot", "device.restart_lr",
    "device.net_config", "device.upgrade_lr", "device.pkg_operation",
    "device.rest_submit",
    "service.enable", "service.disable",
    "plugin.start", "plugin.stop", "plugin.call", "plugin.reboot",
    "plugin.status",
}

# =========================================================================
# Twin events. (path, topic_action) -> (machine_action, actor_type)
# =========================================================================
TWIN_CASES = [
    # the two whole-thing lifecycle events, recognised by path "/"
    ("/",                                        "created",  "twin.create",      "system"),
    ("/",                                        "deleted",  "twin.delete",      "system"),

    # what a device reports
    ("/features/telemetry/properties",           "merged",   "telemetry.report", "device"),
    ("/features/telemetry/properties/temperature", "modified", "telemetry.report", "device"),

    # what somebody WANTS the device to do. Nobody knows who: a twin event
    # carries no identity, so `unknown` is the only honest answer. Asserting
    # `human` here would be the single most damaging lie the log could tell.
    ("/features/telemetry/desiredProperties",    "modified", "desired.set",      "unknown"),
    ("/features/telemetry/desiredProperties/fan_on", "merged", "desired.set",    "unknown"),
    # how s4t-twin-api writes: a merge AT desiredProperties, so the path
    # carries the marker (a root merge would arrive as path "/")
    ("/features/telemetry/desiredProperties",    "merged",   "desired.set",      "unknown"),
    # withdrawing a desired value
    ("/features/telemetry/desiredProperties/fan_on", "deleted", "desired.clear", "unknown"),
    # desired beats prediction and telemetry: the marker is checked first
    ("/features/prediction/desiredProperties",   "modified", "desired.set",      "unknown"),

    # stage D writes here. Not built yet, so this case is the only thing
    # keeping the verb honest until it is.
    ("/features/prediction",                     "created",  "prediction.write", "system"),
    ("/features/prediction/properties/eta",      "modified", "prediction.write", "system"),

    # metadata the provisioner maintains
    ("/attributes",                              "modified", "twin.update",      "system"),
    ("/attributes/boardName",                    "modified", "twin.update",      "system"),

    # the fallback, reached by anything else
    ("/features/health/properties",              "modified", "twin.modify",      "system"),
    ("/policyId",                                "modified", "twin.modify",      "system"),
    ("/",                                        "modified", "twin.modify",      "system"),
]

# =========================================================================
# Operator requests. (method, path) -> (machine_action, machine_id)
# =========================================================================
OPERATOR_CASES = [
    # boards
    ("POST",   "/v1/boards",                          "board.create", None),
    ("GET",    "/v1/boards",                          "board.read",   None),
    ("GET",    f"/v1/boards/{U1}",                    "board.read",   U1),
    ("PATCH",  f"/v1/boards/{U1}",                    "board.update", U1),
    ("PUT",    f"/v1/boards/{U1}",                    "board.update", U1),
    ("DELETE", f"/v1/boards/{U1}",                    "board.delete", U1),
    # a board action whose body names nothing usable falls back to this
    ("POST",   f"/v1/boards/{U1}/action",             "board.action", U1),
    ("POST",   f"/v1/boards/{U1}/actions",            "board.action", U1),
    # IoTronic's real injection route: PUT on the board's plugin collection
    ("PUT",    f"/v1/boards/{U1}/plugins",            "plugin.inject",  U1),

    # plugins. Nested under a board, which is why order matters.
    ("POST",   f"/v1/boards/{U1}/plugins",            "plugin.inject",  U1),
    ("GET",    f"/v1/boards/{U1}/plugins",            "plugin.read",    U1),
    ("PUT",    f"/v1/plugins/{U2}",                   "plugin.execute", U2),
    ("PUT",    f"/v1/plugins/{U2}/action",            "plugin.execute", U2),
    ("POST",   f"/v1/plugins/{U2}/action",            "plugin.execute", U2),
    # THE REGRESSION. This returned board.delete before the reorder.
    ("DELETE", f"/v1/boards/{U1}/plugins/{U2}",       "plugin.remove",  U1),

    # services, also nested
    ("POST",   f"/v1/boards/{U1}/services",           "service.expose", U1),
    ("PUT",    f"/v1/boards/{U1}/services/{U2}",      "service.expose", U1),
    ("GET",    f"/v1/boards/{U1}/services",           "service.read",   U1),
    ("DELETE", f"/v1/boards/{U1}/services/{U2}",      "service.remove", U1),
    # restore is a GET in IoTronic, and it is NOT a read
    ("GET",    f"/v1/boards/{U1}/services/restore",   "service.restore", U1),

    # the fallback. An action nobody anticipated still becomes a record.
    ("POST",   "/v1/something-nobody-designed",       "api.post",     None),
    ("GET",    "/v1/",                                "api.get",      None),
    ("HEAD",   "/",                                   "api.head",     None),
]

# =========================================================================
# Operator requests whose BODY names the action.
# (method, path, body, machine_action, machine_id)
#
# The real routes, from the SDF mapping of Lightning-Rod (D8.1). Without the
# body every row of the first block would be `board.action`, which is the gap
# that made "who rebooted this board" unanswerable.
# =========================================================================
def body(name, **extra):
    import json
    d = {"action": name}
    d.update(extra)
    return json.dumps(d).encode()


BODY_CASES = [
    ("POST", f"/v1/boards/{U1}/action", body("DevicePing"),         "device.ping",          U1),
    ("POST", f"/v1/boards/{U1}/action", body("DeviceEcho", parameters={"say": "hi"}),
                                                                    "device.echo",          U1),
    ("POST", f"/v1/boards/{U1}/action", body("DeviceReboot"),       "device.reboot",        U1),
    ("POST", f"/v1/boards/{U1}/action", body("DeviceRestartLR"),    "device.restart_lr",    U1),
    ("POST", f"/v1/boards/{U1}/action", body("DeviceNetConfig"),    "device.net_config",    U1),
    ("POST", f"/v1/boards/{U1}/action", body("DeviceUpgradeLR"),    "device.upgrade_lr",    U1),
    ("POST", f"/v1/boards/{U1}/action", body("DevicePkgOperation"), "device.pkg_operation", U1),
    ("POST", f"/v1/boards/{U1}/action", body("DeviceRestSubmit"),   "device.rest_submit",   U1),

    ("POST", f"/v1/boards/{U1}/services/{U2}/action", body("ServiceEnable"),  "service.enable",  U1),
    ("POST", f"/v1/boards/{U1}/services/{U2}/action", body("ServiceDisable"), "service.disable", U1),

    ("POST", f"/v1/boards/{U1}/plugins/{U2}", body("PluginStart"),  "plugin.start",  U1),
    ("POST", f"/v1/boards/{U1}/plugins/{U2}", body("PluginStop"),   "plugin.stop",   U1),
    ("POST", f"/v1/boards/{U1}/plugins/{U2}", body("PluginCall"),   "plugin.call",   U1),
    ("POST", f"/v1/boards/{U1}/plugins/{U2}", body("PluginReboot"), "plugin.reboot", U1),
    ("POST", f"/v1/boards/{U1}/plugins/{U2}", body("PluginStatus"), "plugin.status", U1),

    # A name that would not make a clean verb must not enter the vocabulary.
    # It falls back to the path verb; the raw string stays in the detail.
    ("POST", f"/v1/boards/{U1}/action", body("x; DROP TABLE"),      "board.action",  U1),
    ("POST", f"/v1/boards/{U1}/action", body("Device"),             "board.action",  U1),
    ("POST", f"/v1/boards/{U1}/action", b"not json at all",         "board.action",  U1),
    ("POST", f"/v1/boards/{U1}/action", b'["a", "list"]',           "board.action",  U1),
    ("POST", f"/v1/boards/{U1}/action", b'{"action": 42}',          "board.action",  U1),
    # a trailing newline must not sneak a variant verb into the vocabulary
    ("POST", f"/v1/boards/{U1}/action", body("DeviceReboot\n"),     "board.action",  U1),
    # a GET is a read, even on an /action path
    ("GET",  f"/v1/boards/{U1}/action", None,                       "board.read",    U1),
    ("GET",  f"/v1/boards/{U1}/plugins/{U2}/action", None,          "plugin.read",   U1),
    # A body on a DELETE is ignored: only POST and PUT carry an action.
    ("DELETE", f"/v1/boards/{U1}/plugins/{U2}", body("PluginStart"), "plugin.remove", U1),
]

# =========================================================================
# Runner
# =========================================================================
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
        print(f"  FAIL  {label}\n          got  {got}\n          want {want}")


print("classify(): twin events")
for path, topic_action, action, actor in TWIN_CASES:
    check(f"{topic_action:9} {path}",
          logger.classify(path, topic_action), (action, actor))

print("classify_request(): operator requests")
for method, path, action, machine_id in OPERATOR_CASES:
    check(f"{method:6} {path}",
          proxy.classify_request(method, path), (action, machine_id))

print("classify_request(): the action named in the body")
for method, path, b, action, machine_id in BODY_CASES:
    check(f"{method:6} {path[-22:]} {(b or b'')[:32]!r}",
          proxy.classify_request(method, path, b), (action, machine_id))

# -------------------------------------------------------------------------
# Coverage. Without this the tables above could drift to cover nine of the
# twelve verbs and still report a clean run, which is the same vacuous pass
# the VOID verdict was invented for in the shell suite.
# -------------------------------------------------------------------------
print("coverage: the tables exercise the whole published vocabulary")
twin_seen = {a for _, _, a, _ in TWIN_CASES}
oper_seen = ({a for _, _, a, _ in OPERATOR_CASES if not a.startswith("api.")}
             | {a for _, _, _, a, _ in BODY_CASES})
check("every twin verb has a case",     twin_seen, TWIN_VOCAB)
check("every operator verb has a case", oper_seen, OPERATOR_VOCAB)

# -------------------------------------------------------------------------
# Records must be buildable for every verb, not merely classifiable. The
# store has NOT NULL columns, and a board.create with no board identifier is
# precisely the case that wedged the consumer in a requeue loop.
# -------------------------------------------------------------------------
print("build: every verb produces a storable record")
for method, path, action, _ in OPERATOR_CASES:
    rec = proxy.build_operator_record(
        user="tester", project="p", method=method, path=path, status=200)
    # bool(), because `and` returns the last operand, not a boolean. Without
    # it a passing case reports the machine name as its result.
    ok = bool(rec["machine_action"] == action
              and rec["machine_name"]                  # NOT NULL in the store
              and rec["actor"]["type"] == "human"
              and rec["operator_name"] == "tester")
    check(f"record for {action:15} via {method}", ok, True)

for path, topic_action, action, actor in TWIN_CASES:
    rec = logger.build_record(
        topic=f"s4t/probe-01/things/twin/events/{topic_action}",
        path=path, value={"x": 1}, extra=None, revision=7, occurred_at=None)
    ok = bool(rec is not None
              and rec["machine_action"] == action
              and rec["actor"]["type"] == actor
              and rec["operator_name"] is None        # T5: never a human
              and rec["machine_name"])
    check(f"record for {action:15} at {path[:28]}", ok, True)

# the raw action name travels next to the derived verb
rec = proxy.build_operator_record(
    user="tester", project="p", method="POST", path=f"/v1/boards/{U1}/action",
    status=200, detail={"method": "POST"}, body=body("DeviceRestartLR"))
check("raw action name kept in action.detail",
      (rec["machine_action"], rec["action"]["detail"].get("action")),
      ("device.restart_lr", "DeviceRestartLR"))

# a malformed topic must be refused rather than stored as something plausible
check("a malformed topic is refused",
      logger.build_record(topic="nonsense", path="/", value={}, extra=None,
                          revision=1, occurred_at=None), None)

print(f"\n  {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
