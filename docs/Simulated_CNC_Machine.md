---
output:
  word_document: default
  html_document: default
---

# A Simulated CNC Milling Machine for the Digital Twin

Author: Mohsen Ghalem
Date: 1 November 2026
Project: Stack4Things, Industry 5.0 extension work

---

## 1. What it is

The mock board can now behave like a real machine: a vertical milling machine whose measurements, controls and failures follow the **AI4I 2020 Predictive Maintenance** dataset (UCI), the dataset chosen for Phase 2. It replaces "a temperature and a fan" with something an operator would recognise, and gives the digital twin, the Horizon controls and, later, the prediction model realistic material to work on.

It is clearly a simulation: its nameplate says `simulated: true` and names no real vendor, and the Overview shows a *simulated* label next to it.

## 2. Running it

```bash
# 1. once: give the board's twin the machine's nameplate
python3 scripts/seed_machine_profile.py <board uuid>

# 2. drive the twin with the simulated machine
docker exec -it s4t-ditto-bridge python3 s4t_mock_board.py <board uuid> \
    --host crossbar --profile cnc
```

`--time-scale` sets how many machine seconds pass per real second (default 10). At `--time-scale 60` a tool wears out in about four real minutes, which is useful in a demonstration. `--seed` makes a run repeatable.

The machine starts **stopped**. Start it from the Overview: *Run* ON, *Send to machine*.

## 3. What it reports

| Measurement | Unit | Behaviour |
|---|---|---|
| Spindle speed | rpm | Follows the setpoint with about 3 s lag |
| Torque | N·m | Cutting load: higher with feed, lower at high speed, rising with tool wear (AI4I range 3 to 77) |
| Spindle power | kW | Torque × speed |
| Process temperature | °C | Heated by spindle power, cooled by coolant, about 10 K above air in normal running |
| Air temperature | °C | Slow random walk around 25 |
| Vibration | mm/s | Rises with speed and sharply with tool wear. Overview marks ISO 10816 *alert* (2.8) and *danger* (7.1) |
| Tool wear | min | Cutting minutes on the current tool, scaled by feed |
| Tool life remaining | min | Against the tool's **rated** life of 220 min |
| Parts produced, operating hours, energy | count, h, kWh | Accumulate while cutting in auto |
| Machine state | | running, idle, stopping, maintenance or alarm |

## 4. What an operator can set

These are the twin's desired properties, offered by the Overview because the twin's nameplate says `profile: cnc_mill`. The ranges in the allow list (`conf_twin/twin_allowlist.json`) match the machine's exactly, and a test checks that they stay matched.

| Control | Values |
|---|---|
| Run | ON / OFF |
| Mode | auto, manual, maintenance |
| Spindle speed setpoint | 800 to 3000 rpm |
| Feed override | 50 to 120 % |
| Coolant | ON / OFF |

## 5. How it fails

The four AI4I failure modes are real behaviour. The machine stops, latches an alarm, and the Overview shows a red banner with the cause and the way to recover.

| Code | Failure | Condition |
|---|---|---|
| TWF | Tool wear | Tool wear reaches this tool's actual life, drawn between 200 and 240 min. The actual value is hidden: the screen counts against the rated 220, so predicting the real end of life is left to the maintenance model |
| HDF | Heat dissipation | Process minus air below 8.6 K at spindle speed below 1380 rpm. Low speed with coolant on causes it |
| PWF | Power | Spindle power below 3.5 or above 9.0 kW. Feed 120 % at 2900 rpm, or feed 50 % at 1500 rpm, causes it |
| OSF | Overstrain | Tool wear × torque above 11000 min·N·m |

Failures are judged only once the spindle has reached its setpoint and the condition has lasted 5 s, so a spindle that is still accelerating does not trip.

**Recovering, as on a real machine:**

1. Set *Mode* to **maintenance**. The spindle stops; after 5 s the alarm clears and the tool is changed (wear back to 0, a new tool life).
2. Set *Mode* back to **auto**.
3. Switch *Run* **OFF, then ON**.

Step 3 is a restart interlock. The bridge keeps re-sending any desired value the machine has not reached, so a `run: ON` left over from before the alarm would otherwise restart the machine by itself the moment the alarm cleared. A real controller refuses that, and so does this one. While the interlock is active the Overview says so.

## 6. Files

| File | What it is |
|---|---|
| `scripts/s4t_machine_model.py` | The machine: physics, controls, failures, nameplate. Pure Python, no I/O |
| `scripts/test_machine_model.py` | 31 seeded tests: ranges, every failure mode, the interlock |
| `scripts/s4t_mock_board.py` | Changed: `--profile cnc`, `--time-scale`, `--seed` |
| `scripts/seed_machine_profile.py` | Writes the nameplate to the twin's `attributes/machine` |
| `scripts/s4t_twin_api.py` | Changed: the allow list can be chosen by machine profile |
| `conf_twin/twin_allowlist.json` | Changed: the `cnc_mill` controls |
| `panels/iot_overview/templates/iot_overview/index.html` | Changed: units, readable names, alarm banner, machine state, nameplate line |

## 7. Installing it

The machine model is copied into the shared image, and the twin service changed:

```bash
dcs() { docker compose -f docker-compose.yml -f docker-compose.ditto.yml "$@"; }
dcs build s4t-twin-api
dcs up -d --no-deps --force-recreate s4t-twin-api s4t-ditto-bridge
```

The template changes need only a browser reload.
