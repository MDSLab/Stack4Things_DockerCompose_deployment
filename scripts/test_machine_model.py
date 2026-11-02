#!/usr/bin/env python3
"""
test_machine_model.py - the simulated CNC mill behaves like the real thing.

Seeded, so every run is identical. No network, no Docker.

    python3 scripts/test_machine_model.py
"""

import importlib.util
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("m", HERE / "s4t_machine_model.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")


def run_for(mill, seconds, dt=1.0):
    for _ in range(int(seconds / dt)):
        mill.step(dt)
    return mill.state()


print("controls")
mc = m.CncMill(seed=1)
check("idle at start", mc.state()["machine_state"], "idle")
check("valid controls applied", mc.apply({"run": True, "feed_override_pct": 90}), {})
check("out of range refused", list(mc.apply({"spindle_speed_setpoint_rpm": 5000})), ["spindle_speed_setpoint_rpm"])
check("wrong type refused", list(mc.apply({"coolant_on": "yes"})), ["coolant_on"])
check("unknown control refused", list(mc.apply({"self_destruct": True})), ["self_destruct"])
check("bad mode refused", list(mc.apply({"mode": "turbo"})), ["mode"])
s = run_for(mc, 20)
check("controls are echoed (so the twin can converge)",
      (s["run"], s["feed_override_pct"], s["spindle_speed_setpoint_rpm"]), (True, 90, 1500))

print("normal running, AI4I ranges")
mc = m.CncMill(seed=2)
mc.apply({"run": True})
s = run_for(mc, 120)
check("running", s["machine_state"], "running")
check("speed reaches setpoint", abs(s["spindle_speed_rpm"] - 1500) < 40, True)
check("torque in AI4I range (3-77 Nm)", 3 <= s["torque_nm"] <= 77, True)
check("power inside the PWF band", 3.5 < s["spindle_power_kw"] < 9.0, True)
check("process ~10 K above air", 7 < s["process_temperature_c"] - s["air_temperature_c"] < 13, True)
check("tool wears while cutting", s["tool_wear_min"] > 1.5, True)
check("parts are produced in auto", s["parts_produced"] > 0, True)
check("no alarm", s["alarm_code"], "")

print("failure modes")
mc = m.CncMill(seed=3)
mc.apply({"run": True, "spindle_speed_setpoint_rpm": 2900, "feed_override_pct": 120})
s = run_for(mc, 60)
check("feed 120% at 2900 rpm overloads the spindle: PWF", s["alarm_code"], "PWF")
check("the spindle stops", (s["run"], s["machine_state"]), (False, "alarm"))

mc = m.CncMill(seed=4)
mc.apply({"run": True, "spindle_speed_setpoint_rpm": 1300, "coolant_on": True})
s = run_for(mc, 400)
check("low speed with coolant: heat dissipation failure", s["alarm_code"], "HDF")

mc = m.CncMill(seed=5)
mc.apply({"run": True})
s = run_for(mc, 60 * 260, dt=5)
check("a tool run to its end: TWF or OSF", s["alarm_code"] in ("TWF", "OSF"), True)
check("it happened inside the AI4I wear window", 190 <= s["tool_wear_min"] <= 245, True)

mc = m.CncMill(seed=6)
mc.apply({"run": True, "spindle_speed_setpoint_rpm": 1500, "feed_override_pct": 50})
s = run_for(mc, 30)
check("too light a cut is low power: PWF", s["alarm_code"], "PWF")

mc = m.CncMill(seed=7)
mc.apply({"run": True})
for _ in range(2):
    mc.step(1.0)
check("no false alarm while the spindle ramps up", mc.state()["alarm_code"], "")

print("after an alarm: no self restart")
mc = m.CncMill(seed=3)
mc.apply({"run": True, "spindle_speed_setpoint_rpm": 2900, "feed_override_pct": 120})
run_for(mc, 60)
check("run refused while the alarm is active", list(mc.apply({"run": True})), ["run"])
mc.apply({"mode": "maintenance", "feed_override_pct": 100, "spindle_speed_setpoint_rpm": 1500})
s = run_for(mc, 10)
check("maintenance clears the alarm and changes the tool",
      (s["alarm_code"], s["tool_wear_min"], s["machine_state"]), ("", 0.0, "maintenance"))
mc.apply({"mode": "auto"})
check("a STALE run:true does not restart it (interlock)", list(mc.apply({"run": True})), ["run"])
check("the interlock is reported", mc.state()["restart_interlock"], True)
check("run OFF re-arms", mc.apply({"run": False}), {})
check("then run ON starts it", mc.apply({"run": True}), {})
s = run_for(mc, 30)
check("running again", s["machine_state"], "running")

print("the nameplate")
check("marked as simulated", m.NAMEPLATE["simulated"], True)
check("profile name", m.NAMEPLATE["profile"], "cnc_mill")

print(f"\n  {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
