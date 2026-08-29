#!/usr/bin/env python3
"""
s4t_ditto_provisioner.py

Keeps Eclipse Ditto twins in sync with the IoTronic `boards` table.

WHY THIS EXISTS
---------------
Since Ditto 3.3.0 a merge/PATCH against a Thing that does not exist is
implicitly converted into a "Create Thing". The Phase 1 bridge emits merge
commands, so the first telemetry message from a board silently creates its
twin. That works, but the twin it creates is wrong in three ways:

  1. It has no explicit policyId, so Ditto generates a default policy whose
     id equals the thing id and which grants only the subject the connection
     authenticated as. Anyone else, including the Explorer UI user, may not
     be able to read or write it.
  2. It contains only what the telemetry carried. No boardName, no fleet, no
     tier, no attributes/extra, and no attributes/pipeline, which means the
     descriptor driven predictor has nothing to read.
  3. A device message becomes the thing that mints an identity. IoTronic is
     supposed to be the authority on device identity; Ditto is the authority
     on device state. Implicit creation inverts that.

This tool restores the intended direction: IoTronic is read, twins are
written to match.

WHAT IT DOES
------------
Reconciles the full set of boards against the full set of twins in the
target namespace, and handles all three CRUD directions:

  board added    -> PUT a complete twin with the right policy and attributes
  board changed  -> PATCH only the board derived attributes
  board removed  -> mark the twin orphaned, or delete it with --allow-delete

SAFETY PROPERTIES
-----------------
  * Updates never touch `features`, so live telemetry, predictions and
    desired properties are never clobbered.
  * Updates never touch `attributes/pipeline`, so a pipeline descriptor
    edited by hand or by the Phase 4 work survives every sync.
  * Creation uses `If-None-Match: *`, so if a board reports telemetry at the
    same moment the provisioner runs, the PUT fails with 412 instead of
    overwriting the twin, and the run falls through to the update path.
  * Updates send `if-equal: skip`, so an unchanged board produces no new
    revision and no change event. Without this, every poll cycle would emit
    an event per board and the Phase 3 predictor would see a stream of
    meaningless updates.
  * Removal defaults to marking rather than deleting, because deleting a
    twin destroys its event sourced history.
  * --dry-run prints every intended call and sends nothing.

USAGE
-----
    pip install pymysql

    # one reconciliation pass, printing what it would do
    python3 s4t_ditto_provisioner.py --once --dry-run

    # one real pass
    python3 s4t_ditto_provisioner.py --once

    # create the fleet policy first if it does not exist yet
    python3 s4t_ditto_provisioner.py --once --create-policy

    # repair twins that were implicitly created by the bridge
    python3 s4t_ditto_provisioner.py --once --repair-policies

    # run continuously
    python3 s4t_ditto_provisioner.py --watch --interval 30

    # test the reconcile logic with no database at all
    python3 s4t_ditto_provisioner.py --once --boards-json fixture.json

Every connection detail can be overridden by flag or environment variable;
see build_parser(). Credentials are never printed.
"""

import argparse
import base64
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

LOG = logging.getLogger("provisioner")

# Attributes this tool owns. Anything not in here is left alone on update,
# which is what protects attributes/pipeline and anything a user added.
OWNED_ATTRIBUTES = (
    "boardName", "boardType", "fleet", "status", "owner",
    "project", "mobile", "lrVersion", "extra", "provisioning",
)


# --------------------------------------------------------------------------
# Ditto HTTP client, stdlib only so the tool has one dependency (pymysql)
# --------------------------------------------------------------------------
class Ditto:
    def __init__(self, base_url, user, password, dry_run=False):
        self.base = base_url.rstrip("/")
        self.auth = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.dry_run = dry_run

    def _call(self, method, path, body=None, headers=None, mutating=True):
        url = f"{self.base}{path}"
        if self.dry_run and mutating:
            LOG.info("DRY RUN %s %s %s", method, path,
                     json.dumps(body)[:160] if body else "")
            return 200, {}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Basic {self.auth}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError:
                payload = {"raw": raw.decode(errors="replace")[:300]}
            return exc.code, payload
        except urllib.error.URLError as exc:
            raise SystemExit(f"cannot reach Ditto at {self.base}: {exc.reason}")

    def get(self, path):
        return self._call("GET", path, mutating=False)

    def put(self, path, body, headers=None):
        return self._call("PUT", path, body, headers)

    def patch(self, path, body, headers=None):
        h = {"Content-Type": "application/merge-patch+json"}
        h.update(headers or {})
        # urllib sets Content-Type from add_header; _call adds application/json
        # first, so override explicitly here.
        return self._call("PATCH", path, body, h)

    def delete(self, path):
        return self._call("DELETE", path)

    def list_twins(self, namespace):
        """Every twin in the namespace, following the search cursor."""
        found, cursor = {}, None
        while True:
            params = {
                "namespaces": namespace,
                "fields": "thingId,policyId,attributes",
                "option": f'size(200){"" if cursor is None else f",cursor({cursor})"}',
            }
            status, body = self.get("/api/2/search/things?" + urllib.parse.urlencode(params))
            if status != 200:
                raise SystemExit(f"search failed ({status}): {json.dumps(body)[:300]}")
            for item in body.get("items", []):
                found[item["thingId"]] = item
            cursor = body.get("cursor")
            if not cursor:
                return found


# --------------------------------------------------------------------------
# Board sources
# --------------------------------------------------------------------------
BOARD_QUERY = (
    "SELECT uuid, name, type, status, fleet, owner, project, mobile, "
    "       lr_version, extra, created_at, updated_at "
    "FROM boards WHERE uuid IS NOT NULL"
)


def load_boards_from_db(host, port, user, password, database):
    try:
        import pymysql
    except ImportError:
        raise SystemExit("pymysql is required: pip install pymysql "
                         "(or use --boards-json to run without a database)")
    con = pymysql.connect(host=host, port=port, user=user,
                          password=password, database=database)
    try:
        with con.cursor() as cur:
            cur.execute(BOARD_QUERY)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        con.close()


def load_boards_from_json(path):
    with open(path) as fh:
        return json.load(fh)


# --------------------------------------------------------------------------
# Mapping
# --------------------------------------------------------------------------
def _parse_extra(raw):
    """boards.extra is a TEXT column holding JSON, and may be null or junk."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except (ValueError, TypeError):
        LOG.warning("boards.extra is not valid JSON, storing it as a string")
        return {"raw": str(raw)}


def _iso(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


def board_attributes(board, tier):
    """The attributes this tool owns, derived from one board row."""
    return {
        "boardName": board.get("name"),
        "boardType": board.get("type"),
        "fleet": board.get("fleet"),
        "status": board.get("status"),
        "owner": board.get("owner"),
        "project": board.get("project"),
        "mobile": bool(board.get("mobile")),
        "lrVersion": board.get("lr_version"),
        "extra": _parse_extra(board.get("extra")),
        "provisioning": {
            "source": "iotronic",
            "boardCreatedAt": _iso(board.get("created_at")),
            "boardUpdatedAt": _iso(board.get("updated_at")),
            "orphaned": False,
        },
        "tier": tier,
    }


def new_twin_body(board, policy_id, tier):
    """Full twin shape, used only on creation."""
    attrs = board_attributes(board, tier)
    attrs["pipeline"] = {"version": 1, "stages": []}
    return {
        "policyId": policy_id,
        "attributes": attrs,
        "features": {
            # Deliberately NO empty "desiredProperties" here. The Ditto
            # outbound target filters on
            # exists(features/telemetry/desiredProperties), and that is TRUE
            # for an empty object. Seeding {} makes every twin match, so every
            # twin event is published to the commands queue and the bridge
            # discards them all. Ditto creates the field when a desired value
            # is actually set, which is when the filter should start matching.
            "telemetry": {"properties": {}},
            "prediction": {"properties": {}},
            "health": {"properties": {}},
        },
    }


def attributes_differ(desired, current):
    """True if any attribute this tool owns needs updating.

    Only the owned keys are compared, so a pipeline descriptor or any other
    attribute added elsewhere never triggers a write. provisioning.syncedAt
    is deliberately not part of the payload at all, because a timestamp that
    changes every cycle would make every comparison unequal and defeat the
    if-equal: skip optimisation.
    """
    for key in OWNED_ATTRIBUTES:
        if key == "provisioning":
            want = desired.get("provisioning") or {}
            have = current.get("provisioning") or {}
            if any(want.get(k) != have.get(k) for k in
                   ("source", "boardCreatedAt", "boardUpdatedAt", "orphaned")):
                return True
            continue
        if desired.get(key) != current.get(key):
            return True
    return desired.get("tier") != current.get("tier")


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------
def policy_body(subjects):
    return {
        "entries": {
            "owner": {
                "subjects": {s: {"type": "provisioned by s4t_ditto_provisioner"}
                             for s in subjects},
                "resources": {
                    "thing:/": {"grant": ["READ", "WRITE"], "revoke": []},
                    "policy:/": {"grant": ["READ", "WRITE"], "revoke": []},
                    "message:/": {"grant": ["READ", "WRITE"], "revoke": []},
                },
            }
        }
    }


def ensure_policy(ditto, policy_id, subjects, create):
    status, _ = ditto.get(f"/api/2/policies/{urllib.parse.quote(policy_id)}")
    if status == 200:
        LOG.info("policy %s exists", policy_id)
        return True
    if status != 404:
        LOG.error("unexpected status %s checking policy %s", status, policy_id)
        return False
    if not create:
        LOG.error("policy %s does not exist. Re-run with --create-policy, "
                  "or create it by hand before provisioning.", policy_id)
        return False
    status, body = ditto.put(f"/api/2/policies/{urllib.parse.quote(policy_id)}",
                             policy_body(subjects))
    if status in (200, 201, 204):
        LOG.info("created policy %s for subjects %s", policy_id, ", ".join(subjects))
        return True
    LOG.error("could not create policy %s (%s): %s", policy_id, status,
              json.dumps(body)[:300])
    return False


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------
class Counts:
    def __init__(self):
        self.created = self.updated = self.unchanged = 0
        self.orphaned = self.deleted = self.repaired = 0
        self.failed = 0

    def summary(self):
        return (f"created={self.created} updated={self.updated} "
                f"unchanged={self.unchanged} orphaned={self.orphaned} "
                f"deleted={self.deleted} repaired={self.repaired} "
                f"failed={self.failed}")


def reconcile(ditto, boards, opts):
    c = Counts()
    ns = opts.namespace
    twins = ditto.list_twins(ns)
    LOG.info("%d board(s) in IoTronic, %d twin(s) in namespace '%s'",
             len(boards), len(twins), ns)

    seen = set()
    for board in boards:
        uuid = board.get("uuid")
        if not uuid:
            continue
        if opts.fleet and board.get("fleet") != opts.fleet:
            continue
        thing_id = f"{ns}:{uuid}"
        seen.add(thing_id)
        path = f"/api/2/things/{urllib.parse.quote(thing_id)}"

        if thing_id not in twins:
            status, body = ditto.put(path, new_twin_body(board, opts.policy, opts.tier),
                                     headers={"If-None-Match": "*"})
            if status in (200, 201, 204):
                LOG.info("created  %s (%s)", thing_id, board.get("name"))
                c.created += 1
                continue
            if status == 412:
                # A telemetry message created it between our search and our
                # PUT. Fall through to the update path rather than clobber it.
                LOG.info("raced     %s, twin appeared mid-run, updating instead", thing_id)
                twins[thing_id] = {"thingId": thing_id, "attributes": {}, "policyId": None}
            else:
                LOG.error("FAILED to create %s (%s): %s", thing_id, status,
                          json.dumps(body)[:200])
                c.failed += 1
                continue

        current = twins[thing_id]
        desired = board_attributes(board, opts.tier)

        if opts.repair_policies:
            actual_policy = current.get("policyId")
            if actual_policy and actual_policy != opts.policy:
                implicit = actual_policy == thing_id
                status, body = ditto.patch(path, {"policyId": opts.policy})
                if status in (200, 204):
                    LOG.info("repaired %s policy %s -> %s", thing_id,
                             actual_policy, opts.policy)
                    c.repaired += 1
                elif status == 403:
                    LOG.error(
                        "CANNOT repair %s: policy %s does not grant this user "
                        "WRITE on policy:/. %s", thing_id, actual_policy,
                        "It was implicitly created by the bridge, so only the "
                        "bridge's subject owns it. Either re-run as that "
                        "subject, or delete the twin and let this tool "
                        "recreate it." if implicit else "")
                    c.failed += 1
                else:
                    LOG.error("FAILED to repair %s policy (%s): %s", thing_id,
                              status, json.dumps(body)[:200])
                    c.failed += 1

        if not attributes_differ(desired, current.get("attributes") or {}):
            c.unchanged += 1
            continue

        status, body = ditto.patch(f"{path}/attributes", desired,
                                   headers={"if-equal": "skip"})
        if status in (200, 204):
            LOG.info("updated  %s (%s)", thing_id, board.get("name"))
            c.updated += 1
        else:
            LOG.error("FAILED to update %s (%s): %s", thing_id, status,
                      json.dumps(body)[:200])
            c.failed += 1

    # twins with no matching board
    for thing_id, twin in twins.items():
        if thing_id in seen:
            continue
        attrs = twin.get("attributes") or {}
        prov = attrs.get("provisioning") or {}
        if prov.get("source") != "iotronic":
            LOG.info("ignoring %s, not provisioned by this tool", thing_id)
            continue
        path = f"/api/2/things/{urllib.parse.quote(thing_id)}"

        if opts.allow_delete:
            status, body = ditto.delete(path)
            if status in (200, 204):
                LOG.warning("DELETED  %s, board no longer in IoTronic", thing_id)
                c.deleted += 1
            else:
                LOG.error("FAILED to delete %s (%s): %s", thing_id, status,
                          json.dumps(body)[:200])
                c.failed += 1
            continue

        if prov.get("orphaned"):
            c.unchanged += 1
            continue
        status, body = ditto.patch(
            f"{path}/attributes/provisioning",
            {"orphaned": True, "orphanedAt": datetime.now(timezone.utc)
             .isoformat(timespec="seconds")},
            headers={"if-equal": "skip"})
        if status in (200, 204):
            LOG.warning("orphaned %s, board no longer in IoTronic "
                        "(history kept, use --allow-delete to remove)", thing_id)
            c.orphaned += 1
        else:
            LOG.error("FAILED to mark %s orphaned (%s): %s", thing_id, status,
                      json.dumps(body)[:200])
            c.failed += 1

    return c


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        description="Sync IoTronic boards into Eclipse Ditto twins.",
        formatter_class=argparse.RawDescriptionHelpFormatter)

    g = p.add_argument_group("Ditto")
    g.add_argument("--ditto-url", default=os.environ.get("DITTO_URL", "http://localhost:8090"))
    g.add_argument("--ditto-user", default=os.environ.get("DITTO_USER", "ditto"))
    g.add_argument("--ditto-pass", default=os.environ.get("DITTO_PASS", "ditto"))
    g.add_argument("--namespace", default=os.environ.get("DITTO_NAMESPACE", "s4t"))
    g.add_argument("--policy", default=os.environ.get("DITTO_POLICY", "s4t:fleet-unime-lab"))
    g.add_argument("--policy-subject", action="append", default=None,
                   help="Subject to grant in a policy created with --create-policy. "
                        "Repeatable. Default: nginx:ditto")

    g = p.add_argument_group("IoTronic database")
    g.add_argument("--db-host", default=os.environ.get("S4T_DB_HOST", "127.0.0.1"))
    g.add_argument("--db-port", type=int, default=int(os.environ.get("S4T_DB_PORT", "3306")))
    g.add_argument("--db-user", default=os.environ.get("S4T_DB_USER", "iotronic"))
    g.add_argument("--db-pass", default=os.environ.get("S4T_DB_PASS", "unime"))
    g.add_argument("--db-name", default=os.environ.get("S4T_DB_NAME", "iotronic"))
    g.add_argument("--boards-json", help="Read boards from a JSON file instead of "
                                         "the database. For testing.")

    g = p.add_argument_group("Behaviour")
    g.add_argument("--once", action="store_true", help="One pass, then exit")
    g.add_argument("--watch", action="store_true", help="Loop forever")
    g.add_argument("--interval", type=float, default=30.0, help="Seconds between passes")
    g.add_argument("--fleet", help="Only provision boards in this fleet")
    g.add_argument("--tier", default="edge", help="Value for attributes/tier")
    g.add_argument("--create-policy", action="store_true",
                   help="Create the policy if it does not exist")
    g.add_argument("--repair-policies", action="store_true",
                   help="Point twins at the target policy if they are on another one, "
                        "which is how implicitly created twins are corrected")
    g.add_argument("--allow-delete", action="store_true",
                   help="DELETE twins whose board is gone, destroying their history. "
                        "Without this they are only marked orphaned.")
    g.add_argument("--dry-run", action="store_true", help="Print, do not write")
    g.add_argument("--heartbeat-file",
                   default=os.environ.get("HEARTBEAT_FILE", "/tmp/provisioner-alive"),
                   help="Touched after every reconciliation pass that had no failures")
    g.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv=None):
    opts = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if opts.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    if not opts.once and not opts.watch:
        LOG.error("choose --once or --watch")
        return 2
    if opts.allow_delete and not opts.dry_run:
        LOG.warning("--allow-delete is set: twins for removed boards will be "
                    "DELETED and their history destroyed")

    ditto = Ditto(opts.ditto_url, opts.ditto_user, opts.ditto_pass, opts.dry_run)
    subjects = opts.policy_subject or ["nginx:ditto"]
    if not ensure_policy(ditto, opts.policy, subjects, opts.create_policy):
        return 1

    def one_pass():
        boards = (load_boards_from_json(opts.boards_json) if opts.boards_json
                  else load_boards_from_db(opts.db_host, opts.db_port, opts.db_user,
                                           opts.db_pass, opts.db_name))
        counts = reconcile(ditto, boards, opts)
        level = logging.ERROR if counts.failed else logging.INFO
        LOG.log(level, "pass complete: %s", counts.summary())
        return counts

    if opts.once:
        return 1 if one_pass().failed else 0

    LOG.info("watching every %.0fs, Ctrl-C to stop", opts.interval)
    from pathlib import Path
    hb = Path(opts.heartbeat_file)
    while True:
        try:
            counts = one_pass()
            # Touch ONLY after a pass with no failures. A provisioner that
            # cannot reach MariaDB or Ditto keeps looping, and leaving the file
            # stale is what turns that into an unhealthy container instead of a
            # silently broken one.
            if counts.failed == 0:
                try:
                    hb.touch()
                except OSError as exc:
                    LOG.warning("cannot write heartbeat %s: %s", hb, exc)
        except SystemExit:
            raise
        except Exception as exc:  # keep the loop alive across transient faults
            LOG.error("pass failed: %s", exc)
        time.sleep(opts.interval)


if __name__ == "__main__":
    sys.exit(main())
