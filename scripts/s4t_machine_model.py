#!/usr/bin/env python3
"""
s4t_machine_model.py

A simulated CNC milling machine, realistic enough to operate and to break.

The numbers follow the AI4I 2020 Predictive Maintenance dataset (UCI, a
synthetic dataset modelled on a real milling process), which is the dataset
the digital twin plan uses for Phase 2: air and process temperature, spindle
speed, torque and tool wear, and its four failure modes. Temperatures are in
degrees Celsius here rather than the dataset's kelvin, because that is what an
operator reads; the 8.6 K threshold below is a difference, so it is the same.

Pure Python, no I/O and no randomness that cannot be seeded, so the behaviour
is unit tested in test_machine_model.py. s4t_mock_board.py drives it.

CONTROLS (what an operator may ask for through the twin's desired state)
------------------------------------------------------------------------
    mode                        "auto" | "manual" | "maintenance"
    run                         true = spindle running, false = stopped
    spindle_speed_setpoint_rpm  800 .. 3000
    feed_override_pct           50 .. 120   (scales the cutting load)
    coolant_on                  true | false

The machine REPORTS every control back, so the twin can show "reached".

FAILURE MODES (from AI4I 2020; the machine stops and latches an alarm)
----------------------------------------------------------------------
    TWF  tool wear failure      tool wear reaches the tool's life,
                                drawn between 200 and 240 min per tool
    HDF  heat dissipation       process - air < 8.6 K at spindle < 1380 rpm
    PWF  power failure          spindle power < 3.5 kW or > 9.0 kW
    OSF  overstrain             tool wear x torque > 11000 min.Nm

Failures are only judged once the spindle is within 5 % of its setpoint and
the condition has lasted 5 s, so a spindle that is still ramping up does not
trip a power alarm.

AFTER AN ALARM, as on a real machine:
  * the spindle stops and `run` reports false;
  * setting mode "maintenance" clears the alarm after 5 s and changes the tool
    (tool wear back to 0, a new tool life);
  * a restart needs `run` to be switched OFF and then ON again. A desired
    `run: true` left over from before the alarm does NOT restart it: the
    bridge keeps re-sending outstanding desired values, and a machine that
    restarted itself on a stale request would be a safety fault.
"""

import math
import random

MODES = ("auto", "manual", "maintenance")

# name -> (type, min, max). Anything outside is ignored, as a real controller
# rejects an out-of-range setpoint.
CONTROLS = {
    "mode": ("enum", MODES, None),
    "run": ("bool", None, None),
    "spindle_speed_setpoint_rpm": ("int", 800, 3000),
    "feed_override_pct": ("int", 50, 120),
    "coolant_on": ("bool", None, None),
}

ALARM_TEXT = {
    "TWF": "Tool wear failure: tool life reached",
    "HDF": "Heat dissipation failure: process too close to ambient at low spindle speed",
    "PWF": "Power failure: spindle power outside 3.5 to 9.0 kW",
    "OSF": "Overstrain failure: tool wear x torque above 11000 min.Nm",
}

NAMEPLATE = {
    "profile": "cnc_mill",
    "simulated": True,
    "manufacturer": "Simulated machine (no real vendor)",
    "model": "VMC-850 vertical machining centre",
    "serial_number": "SIM-VMC850-0001",
    "controller": "Simulated CNC controller, firmware 8.2.1",
    "commissioned": "2024-03-12",
    "location": {"site": "UniMe Engineering Lab", "line": "Line 2", "cell": "Cell A"},
    "ratings": {"spindle_power_kw": 11.0, "max_spindle_speed_rpm": 3000,
                "rated_torque_nm": 70.0, "supply": "400 V 3~ 50 Hz"},
    "tool": {"type": "End mill, 10 mm, carbide", "rated_life_min": 220},
    "maintenance": {"interval_h": 500, "last": "2026-10-15"},
    "data_model": "AI4I 2020 Predictive Maintenance (UCI) ranges and failure modes",
}


class CncMill(object):
    def __init__(self, seed=None):
        self.rng = random.Random(seed)
        self.mode = "auto"
        self.run = False
        self.setpoint = 1500
        self.feed = 100
        self.coolant = True
        self.speed = 0.0
        self.torque = 0.0
        self.power = 0.0
        self.air = 25.0
        self.process = 35.0
        self.wear = 0.0
        self.tool_life = self._new_tool_life()
        self.vibration = 0.6
        self.parts = 0
        self.cycle_progress = 0.0
        self.hours = 0.0
        self.energy = 0.0
        self.alarm = ""
        self.restart_lock = False
        self._fail_timer = {}
        self._maint_timer = 0.0

    def _new_tool_life(self):
        return round(self.rng.uniform(200.0, 240.0), 1)

    # ------------------------------------------------------------- commands
    def apply(self, delta):
        """Apply a desired-state delta. Returns {name: reason} for every value
        refused, so the caller can log it; refused values are simply not
        applied, and the twin shows them as not reached."""
        refused = {}
        if not isinstance(delta, dict):
            return {"_": "not an object"}
        for name, value in delta.items():
            spec = CONTROLS.get(name)
            if spec is None:
                refused[name] = "unknown control"
                continue
            kind, lo, hi = spec
            if kind == "bool" and not isinstance(value, bool):
                refused[name] = "must be true or false"
                continue
            if kind == "int":
                if isinstance(value, bool) or not isinstance(value, (int, float)) \
                        or not (lo <= value <= hi):
                    refused[name] = "must be a number from %s to %s" % (lo, hi)
                    continue
                value = int(round(value))
            if kind == "enum" and value not in lo:
                refused[name] = "must be one of %s" % ", ".join(lo)
                continue

            if name == "run":
                if value is False:
                    self.run = False
                    self.restart_lock = False          # OFF re-arms the restart
                elif self.alarm:
                    refused[name] = "alarm %s active: set mode to maintenance" % self.alarm
                elif self.restart_lock:
                    refused[name] = "restart interlock: switch run OFF, then ON"
                elif self.mode == "maintenance":
                    refused[name] = "machine is in maintenance mode"
                else:
                    self.run = True
            elif name == "mode":
                self.mode = value
                if value == "maintenance":
                    self.run = False
                    self._maint_timer = 0.0
            elif name == "spindle_speed_setpoint_rpm":
                self.setpoint = value
            elif name == "feed_override_pct":
                self.feed = value
            elif name == "coolant_on":
                self.coolant = value
        return refused

    # --------------------------------------------------------------- physics
    def _trip(self, code):
        self.alarm = code
        self.run = False
        self.restart_lock = True
        self._fail_timer = {}

    def step(self, dt):
        """Advance the machine by dt seconds of machine time."""
        dt = max(0.0, float(dt))
        rng = self.rng
        active = self.run and self.mode in ("auto", "manual") and not self.alarm

        # ambient: a slow random walk around 25 C
        self.air += rng.gauss(0, 0.02) * math.sqrt(dt) + (25.0 - self.air) * min(1.0, dt / 3600.0)
        self.air = min(30.0, max(20.0, self.air))

        # spindle: first order towards the setpoint, about 3 s time constant
        target = float(self.setpoint) if active else 0.0
        self.speed += (target - self.speed) * min(1.0, dt / 3.0)
        if active:
            self.speed += rng.gauss(0, self.setpoint * 0.002)
        if self.speed < 1.0:
            self.speed = 0.0

        # torque: cutting load, higher with feed, lower at high speed, and
        # rising as the tool wears
        load = self.feed / 100.0
        if active and self.speed > 50:
            base = 40.0 * load * math.sqrt(1500.0 / max(self.speed, 500.0))
            self.torque = max(3.0, min(80.0, base + 0.04 * self.wear + rng.gauss(0, 1.5)))
        else:
            self.torque = 0.0
        self.power = self.torque * self.speed * 2 * math.pi / 60.0 / 1000.0

        # process temperature: heated by spindle power, cooled by coolant,
        # one minute time constant
        heat = 4.0 + 1.0 * self.power - (3.0 if (self.coolant and active) else 0.0)
        self.process += ((self.air + max(heat, 2.0)) - self.process) * min(1.0, dt / 60.0)

        # wear, output, counters
        if active:
            self.wear += dt / 60.0 * load
            self.hours += dt / 3600.0
            self.energy += self.power * dt / 3600.0
            if self.mode == "auto" and self.speed > 0.9 * self.setpoint:
                cycle_s = 45.0 * (100.0 / self.feed) * (1500.0 / self.setpoint) ** 0.5
                self.cycle_progress += dt / cycle_s
                done = int(self.cycle_progress)
                self.parts += done
                self.cycle_progress -= done

        # vibration (ISO 10816 style mm/s RMS): speed, tool wear, a little noise
        self.vibration = max(0.2, 0.6 + 0.5 * self.speed / 3000.0
                             + 3.0 * (self.wear / 240.0) ** 2 + rng.gauss(0, 0.08))

        # failures, judged only at speed and only if they persist
        if active and abs(self.speed - self.setpoint) <= 0.05 * self.setpoint:
            checks = {
                "TWF": self.wear >= self.tool_life,
                "HDF": (self.process - self.air) < 8.6 and self.speed < 1380,
                "PWF": self.power < 3.5 or self.power > 9.0,
                "OSF": self.wear * self.torque > 11000,
            }
            for code in ("TWF", "OSF", "PWF", "HDF"):
                if checks[code]:
                    self._fail_timer[code] = self._fail_timer.get(code, 0.0) + dt
                    if self._fail_timer[code] >= 5.0:
                        self._trip(code)
                        break
                else:
                    self._fail_timer.pop(code, None)

        # maintenance: clears an alarm and changes the tool after 5 s
        if self.mode == "maintenance":
            self._maint_timer += dt
            if self._maint_timer >= 5.0 and (self.alarm or self.wear > 0):
                self.alarm = ""
                self.wear = 0.0
                self.tool_life = self._new_tool_life()
                self._fail_timer = {}

    # --------------------------------------------------------------- report
    def state(self):
        if self.alarm:
            machine_state = "alarm"
        elif self.mode == "maintenance":
            machine_state = "maintenance"
        elif self.run:
            machine_state = "running"
        elif self.speed > 0:
            machine_state = "stopping"
        else:
            machine_state = "idle"
        return {
            # controls, echoed so the twin can show "reached"
            "mode": self.mode,
            "run": self.run,
            "spindle_speed_setpoint_rpm": self.setpoint,
            "feed_override_pct": self.feed,
            "coolant_on": self.coolant,
            # measurements
            "machine_state": machine_state,
            "spindle_speed_rpm": int(round(self.speed)),
            "torque_nm": round(self.torque, 1),
            "spindle_power_kw": round(self.power, 2),
            "air_temperature_c": round(self.air, 2),
            "process_temperature_c": round(self.process, 2),
            "vibration_mm_s": round(self.vibration, 2),
            "tool_wear_min": round(self.wear, 1),
            # Against the RATED life, as a real controller counts it. The actual
            # life of this tool (200 to 240 min) stays hidden: predicting it is
            # the job of the maintenance model, not something to read off.
            "tool_life_remaining_min": round(max(0.0, NAMEPLATE["tool"]["rated_life_min"] - self.wear), 1),
            "parts_produced": self.parts,
            "operating_hours": round(self.hours, 3),
            "energy_kwh": round(self.energy, 3),
            "alarm_code": self.alarm,
            "alarm_text": ALARM_TEXT.get(self.alarm, ""),
            "restart_interlock": self.restart_lock and not self.alarm,
        }
