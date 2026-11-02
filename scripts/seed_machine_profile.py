#!/usr/bin/env python3
"""
seed_machine_profile.py - give a board's twin the nameplate of the simulated
CNC mill, so the twin describes a machine and not just a board.

Writes ONE key, `attributes/machine`, on the twin `s4t:<board uuid>`. The
provisioner owns a fixed list of attribute keys and never touches anything
else, so this survives every reconciliation. Nothing else on the twin changes.

The nameplate's `profile: cnc_mill` is also what makes s4t-twin-api offer the
mill's controls (run, mode, spindle speed, feed override, coolant) instead of
the generic `fan_on`. Then drive the machine with:

    docker exec -it s4t-ditto-bridge python3 s4t_mock_board.py <uuid> \\
        --host crossbar --profile cnc

Runs on the host, from the repository root, with the standard library only.
Reads DITTO_API_USER, DITTO_API_PASSWORD, DITTO_EXTERNAL_PORT and
DITTO_NAMESPACE from .env.

USAGE
    python3 scripts/seed_machine_profile.py <board_uuid>
    python3 scripts/seed_machine_profile.py <board_uuid> --serial SIM-VMC850-0002 --cell "Cell B"
    python3 scripts/seed_machine_profile.py <board_uuid> --remove
"""

import argparse
import base64
import copy
import importlib.util
import json
import pathlib
import re
import sys
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")


def read_env(path):
    env = {}
    try:
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("board_uuid")
    p.add_argument("--serial", help="override the serial number")
    p.add_argument("--line", help="override location.line")
    p.add_argument("--cell", help="override location.cell")
    p.add_argument("--remove", action="store_true", help="delete attributes/machine")
    a = p.parse_args(argv)
    if not UUID.match(a.board_uuid):
        print("not a board uuid: %s" % a.board_uuid)
        return 2

    env = read_env(HERE.parent / ".env")
    user = env.get("DITTO_API_USER", "s4t")
    pw = env.get("DITTO_API_PASSWORD", "")
    if not pw:
        print("DITTO_API_PASSWORD is not set in .env")
        return 2
    base = "http://localhost:%s/api/2/things/%s:%s/attributes/machine" % (
        env.get("DITTO_EXTERNAL_PORT", "8090"), env.get("DITTO_NAMESPACE", "s4t"), a.board_uuid)

    spec = importlib.util.spec_from_file_location("mm", HERE / "s4t_machine_model.py")
    mm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mm)
    plate = copy.deepcopy(mm.NAMEPLATE)
    if a.serial:
        plate["serial_number"] = a.serial
    if a.line:
        plate["location"]["line"] = a.line
    if a.cell:
        plate["location"]["cell"] = a.cell

    req = urllib.request.Request(
        base, method="DELETE" if a.remove else "PUT",
        data=None if a.remove else json.dumps(plate).encode(),
        headers={"Authorization": "Basic " + base64.b64encode(("%s:%s" % (user, pw)).encode()).decode(),
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
        detail = e.read().decode()[:300]
        if code == 404:
            print("404: no twin s4t:%s yet. Register the board in Horizon and wait ~30 s "
                  "for the provisioner." % a.board_uuid)
        else:
            print("Ditto answered %s: %s" % (code, detail))
        return 1
    except Exception as e:
        print("cannot reach Ditto at %s: %s" % (base, e))
        return 1

    if a.remove:
        print("removed attributes/machine (%s)" % code)
    else:
        print("nameplate written (%s): %s, %s, serial %s, %s / %s / %s" % (
            code, plate["model"], plate["manufacturer"], plate["serial_number"],
            plate["location"]["site"], plate["location"]["line"], plate["location"]["cell"]))
        print("now start the machine:\n  docker exec -it s4t-ditto-bridge python3 s4t_mock_board.py "
              "%s --host crossbar --profile cnc" % a.board_uuid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
