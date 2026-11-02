#!/usr/bin/env python3
"""
s4t_twin_api.py

The twin command service. The ONLY way a person writes to a digital twin.

    Horizon (Device Control panel)
        |  X-Auth-Token: the operator's own Keystone token
        v
    [ this ] --validate token--> Keystone
        |  --check role, check allow list
        |  --merge desiredProperties--> Ditto --> bridge --> device
        +--operator record--> s4t.audit.ingest --> audit logger

WHY A SEPARATE SERVICE
----------------------
Ditto authenticates every caller as one shared subject, `nginx:s4t`, so if
Horizon wrote to Ditto directly two things would go wrong. The Ditto password
would have to live in the Horizon container, and the twin's history would say
WHAT changed but never WHO changed it. This service holds the Ditto credential
on the server, refuses anything the operator's role or the allow list does not
permit, and records the operator's validated name next to the change.

It is deliberately NOT part of the aggregator (whose rule is "every route is a
GET") and NOT part of the audit proxy (which holds no credentials, by design).

WHAT IT ALLOWS
--------------
    GET    /health
    GET    /twin/<uuid>/status            any valid token
    POST   /twin/<uuid>/desired           write role + allow list
    DELETE /twin/<uuid>/desired/<prop>    write role

Nothing else. In particular it never touches `properties` (the device owns
those), attributes (the provisioner owns those) or policies.

THE ALLOW LIST
--------------
A small JSON file names the properties an operator may set, with their type
and range. Anything not on it is refused before it reaches Ditto. Phase 3
replaces this file with the twin's own definition (the D8.1 SDF model), at
which point Ditto validates and this list can go.

CONVERGENCE
-----------
The status route compares desired against reported per property:

    converged     reported == desired
    pending       not yet, and set less than TWIN_CONFIRM_TIMEOUT ago
    unconfirmed   not yet, and set longer ago than that

The time a value was set is kept in memory. After a restart a value that is
still pending reports `pending` with an unknown age, rather than inventing one.

FAILURE MODE
------------
Fail open, like the audit proxy: if the audit record cannot be queued the
write still stands and the loss is logged and counted. TWIN_FAIL_CLOSED=1
refuses writes while the audit queue is unreachable instead.

DEPENDENCIES
------------
    pip install aiohttp aio-pika
"""

import argparse
import asyncio
import json
import logging
import math
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone

LOG = logging.getLogger("twin-api")

SCHEMA_VERSION = "1.0"
# \Z, not $: `$` also matches before a trailing newline.
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
# Property names travel into a Ditto JSON pointer, so they are kept to a
# conservative alphabet: no slash, no dot, no whitespace.
PROP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
FEATURE = "telemetry"


# =========================================================================
# Pure functions. No I/O, unit tested in test_twin_api.py.
# =========================================================================
def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def is_board_uuid(s):
    return bool(s) and bool(UUID_RE.match(s))


def parse_roles(csv):
    """'admin, operator' -> {'admin', 'operator'}. Empty means nobody."""
    return {r.strip() for r in (csv or "").split(",") if r.strip()}


def has_role(user_roles, required):
    """True if the user holds at least one required role."""
    return bool(set(user_roles or ()) & set(required or ()))


def load_allowlist(path):
    """The allow list file, or an EMPTY list if it cannot be read.

    Empty, not permissive: a missing or broken file must mean that nothing can
    be written, never that everything can.
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        LOG.error("allow list %s unreadable, every write will be refused: %s",
                  path, exc)
        return {"default": {}, "by_board_type": {}, "by_profile": {}}
    if not isinstance(data, dict):
        return {"default": {}, "by_board_type": {}, "by_profile": {}}
    return {"default": data.get("default") or {},
            "by_board_type": data.get("by_board_type") or {},
            "by_profile": data.get("by_profile") or {}}


def rules_for(allowlist, board_type=None, profile=None):
    """The properties allowed for one board.

    A machine PROFILE (the twin's attributes/machine/profile, e.g. cnc_mill)
    describes the machine completely, so when the allow list has an entry for
    it, that entry is the whole list: a milling machine is not offered a
    generic `fan_on`. Without a profile: the defaults, overridden or extended
    by the entry for the board type.
    """
    by_profile = allowlist.get("by_profile") or {}
    if profile and profile in by_profile:
        return dict(by_profile[profile] or {})
    rules = dict(allowlist.get("default") or {})
    if board_type:
        rules.update((allowlist.get("by_board_type") or {}).get(board_type) or {})
    return rules


def profile_of(attributes):
    """The machine profile named on the twin, if any."""
    machine = (attributes or {}).get("machine")
    profile = machine.get("profile") if isinstance(machine, dict) else None
    return profile if isinstance(profile, str) else None


def check_value(value, rule):
    """None if `value` satisfies `rule`, else a short reason."""
    kind = (rule or {}).get("type")
    # bool is a subclass of int in Python, so it has to be ruled out by hand
    # or `true` would pass as the integer 1.
    if kind == "boolean":
        if not isinstance(value, bool):
            return "must be true or false"
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return "must be an integer"
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "must be a number"
        # NaN slips past every comparison and is not valid JSON for Ditto.
        if not math.isfinite(value):
            return "must be a finite number"
    elif kind == "string":
        if not isinstance(value, str):
            return "must be a string"
        if len(value) > int(rule.get("max_length", 256)):
            return "is too long"
    else:
        return "has no usable type in the allow list"

    if kind in ("integer", "number"):
        if "min" in rule and value < rule["min"]:
            return "must be at least %s" % rule["min"]
        if "max" in rule and value > rule["max"]:
            return "must be at most %s" % rule["max"]
    if "enum" in rule and value not in rule["enum"]:
        return "must be one of %s" % ", ".join(str(v) for v in rule["enum"])
    return None


def validate_properties(props, rules):
    """(clean, errors). `errors` maps property -> reason. Nothing is written
    unless `errors` is empty: a request is all or nothing."""
    errors = {}
    if not isinstance(props, dict) or not props:
        return {}, {"properties": "must be a non-empty object"}
    for name, value in props.items():
        if not isinstance(name, str) or not PROP_RE.match(name):
            errors[str(name)] = "is not a valid property name"
        elif name not in rules:
            errors[name] = "is not on the allow list for this board"
        else:
            reason = check_value(value, rules[name])
            if reason:
                errors[name] = reason
    return ({} if errors else dict(props)), errors


def json_safe(props):
    """A copy of a refused request that is valid JSON, for the audit record.
    NaN and Infinity are parsed by aiohttp but are not JSON, and the record's
    canonical bytes must stay parseable everywhere."""
    out = {}
    for k, v in (props or {}).items():
        try:
            json.dumps(v, allow_nan=False)
            out[str(k)] = v
        except (ValueError, TypeError):
            out[str(k)] = repr(v)
    return out


def property_status(desired, reported, set_at, now, timeout):
    """One row per desired property: is the device there yet?

    `set_at` maps property -> epoch seconds when this service set it. A
    property with no entry (set before a restart, or by something else) is
    judged without an age, and reported as pending rather than unconfirmed,
    because there is no evidence of how long it has been waiting.
    """
    rows = []
    desired = desired or {}
    reported = reported or {}
    for name in sorted(desired):
        want = desired[name]
        have = reported.get(name)
        since = (set_at or {}).get(name)
        age = None if since is None else max(0.0, now - since)
        if name in reported and have == want:
            state = "converged"
        elif age is not None and age > timeout:
            state = "unconfirmed"
        else:
            state = "pending"
        rows.append({"property": name, "desired": want,
                     "reported": have if name in reported else None,
                     "has_reported": name in reported,
                     "state": state,
                     "pending_seconds": None if state == "converged" or age is None
                     else round(age, 1)})
    return rows


def build_twin_record(user, project, board_uuid, machine_name, verb, detail,
                      status, correlation_id):
    """One operator write -> one audit record, minus the sequence.

    `machine_action` is `desired.set` or `desired.clear`, with actor `human`.
    The twin layer will log the same change a second time as `desired.set`
    with actor `unknown`; `correlation_id` joins the two.
    """
    action = "desired." + verb
    outcome = "success" if 200 <= int(status) < 400 else "failure"
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(uuid.uuid4()),
        "timestamp": now_iso(),
        "operator_name": user,
        "machine_name": machine_name or board_uuid,
        "machine_action": action,
        "actor": {"type": "human", "id": None, "name": user, "project": project},
        "machine": {"id": board_uuid, "name": machine_name or board_uuid,
                    "namespace": None},
        "action": {"verb": verb,
                   "target": "/features/%s/desiredProperties" % FEATURE,
                   "detail": detail, "outcome": outcome,
                   "status_code": int(status)},
        "source": "horizon",
        "twin_revision": None,
        "correlation_id": correlation_id,
    }


# =========================================================================
# Keystone
# =========================================================================
class TokenValidator:
    """X-Auth-Token -> (user, project, roles), validated by Keystone.

    A token is checked by asking Keystone, never by trusting a header a
    client could set. Results are cached briefly, because the panel polls the
    status route and each poll would otherwise cost a Keystone round trip.
    The cache is short (60s by default) so a role removed in Keystone takes
    effect quickly.
    """

    def __init__(self, keystone_url, ttl):
        self.url = keystone_url.rstrip("/")
        if self.url.endswith("/v3"):
            self.url = self.url[:-3].rstrip("/")
        self.ttl = ttl
        self.cache = {}

    async def validate(self, session, token):
        import aiohttp
        if not token:
            return None
        hit = self.cache.get(token)
        if hit and hit[1] > time.time():
            return hit[0]
        try:
            async with session.get(
                    self.url + "/v3/auth/tokens",
                    headers={"X-Auth-Token": token, "X-Subject-Token": token},
                    timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status != 200:
                    return None
                body = await r.json()
        except Exception as exc:
            LOG.warning("cannot validate token with Keystone: %s", exc)
            return None
        tok = (body or {}).get("token") or {}
        user = (tok.get("user") or {}).get("name")
        if not user:
            return None
        who = {"user": user,
               "project": (tok.get("project") or {}).get("id"),
               "roles": sorted({r.get("name") for r in tok.get("roles") or []
                                if r.get("name")})}
        # Never cache past the token's own expiry.
        expires = time.time() + self.ttl
        try:
            exp = datetime.strptime(tok.get("expires_at", "")[:19],
                                    "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            expires = min(expires, exp.timestamp())
        except (ValueError, TypeError):
            pass
        self.cache[token] = (who, expires)
        if len(self.cache) > 2000:                 # tokens rotate; keep it bounded
            self.cache = {k: v for k, v in self.cache.items() if v[1] > time.time()}
        return who


# =========================================================================
# The service
# =========================================================================
class TwinApi:
    def __init__(self, opts):
        self.o = opts
        self.validator = TokenValidator(opts.keystone_url, opts.token_cache_ttl)
        self.write_roles = parse_roles(opts.write_roles)
        self.read_roles = parse_roles(opts.read_roles)
        self.set_at = {}            # (board_uuid, prop) -> epoch seconds
        self.channel = None
        self.session = None
        self.writes = self.records = self.record_failures = 0

    # ---- helpers ---------------------------------------------------------
    def thing_id(self, board_uuid):
        return "%s:%s" % (self.o.ditto_namespace, board_uuid)

    def ditto_auth(self):
        import aiohttp
        return aiohttp.BasicAuth(self.o.ditto_user, self.o.ditto_pass)

    def allowlist(self):
        # Re-read on every request. It is a few hundred bytes, and it means an
        # edited file takes effect without restarting anything.
        return load_allowlist(self.o.allowlist)

    async def get_thing(self, board_uuid):
        """(status, thing or None)."""
        import aiohttp
        url = "%s/api/2/things/%s" % (self.o.ditto_url, self.thing_id(board_uuid))
        async with self.session.get(
                url, auth=self.ditto_auth(),
                params={"fields": "thingId,attributes,features/%s,_revision" % FEATURE},
                timeout=aiohttp.ClientTimeout(total=self.o.timeout)) as r:
            if r.status != 200:
                return r.status, None
            return 200, await r.json()

    async def who(self, request, need_write):
        """(identity, error response). Exactly one of the two is None."""
        from aiohttp import web
        ident = await self.validator.validate(
            self.session, request.headers.get("X-Auth-Token"))
        if not ident:
            return None, web.json_response(
                {"error": "a valid Keystone token is required"}, status=401)
        needed = self.write_roles if need_write else self.read_roles
        if needed and not has_role(ident["roles"], needed):
            return None, web.json_response(
                {"error": "role required: one of %s" % ", ".join(sorted(needed))},
                status=403)
        return ident, None

    async def record(self, rec):
        """True when the record reached the audit queue."""
        import aio_pika
        if self.channel is None or self.channel.is_closed:
            self.record_failures += 1
            LOG.error("audit queue unavailable, NOT recorded: %s %s by %s",
                      rec["machine_action"], rec["machine"]["id"], rec["operator_name"])
            return False
        try:
            await self.channel.default_exchange.publish(
                aio_pika.Message(
                    body=json.dumps(rec, separators=(",", ":"), sort_keys=True).encode(),
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                routing_key=self.o.ingest_queue)
            self.records += 1
            return True
        except Exception as exc:
            self.record_failures += 1
            LOG.error("FAILED to record %s: %s", rec["machine_action"], exc)
            return False

    def audit_ready(self):
        return self.channel is not None and not self.channel.is_closed

    # ---- routes ----------------------------------------------------------
    async def health(self, request):
        from aiohttp import web
        return web.json_response({
            "ok": True, "audit_queue": self.audit_ready(),
            "writes": self.writes, "records": self.records,
            "record_failures": self.record_failures})

    async def status(self, request):
        from aiohttp import web
        board = request.match_info["uuid"]
        if not is_board_uuid(board):
            return web.json_response({"error": "not a board uuid"}, status=400)
        ident, err = await self.who(request, need_write=False)
        if err is not None:      # a Response is a mapping, and an empty one is falsy
            return err
        try:
            code, thing = await self.get_thing(board)
        except Exception as exc:
            return web.json_response({"error": "twin layer unreachable: %s" % exc},
                                     status=502)
        if thing is None:
            return web.json_response(
                {"error": "no twin for this board" if code == 404
                 else "twin layer answered %s" % code},
                status=404 if code == 404 else 502)
        tele = ((thing.get("features") or {}).get(FEATURE) or {})
        attrs = thing.get("attributes") or {}
        set_at = {p: t for (b, p), t in self.set_at.items() if b == board}
        rules = rules_for(self.allowlist(), attrs.get("boardType"), profile_of(attrs))
        return web.json_response({
            "uuid": board,
            "name": attrs.get("boardName"),
            "board_type": attrs.get("boardType"),
            "revision": thing.get("_revision"),
            "reported": tele.get("properties") or {},
            "desired": tele.get("desiredProperties") or {},
            "properties": property_status(
                tele.get("desiredProperties"), tele.get("properties"),
                set_at, time.time(), self.o.confirm_timeout),
            "allowed": rules,
            "can_write": has_role(ident["roles"], self.write_roles),
            "confirm_timeout": self.o.confirm_timeout,
        })

    async def set_desired(self, request):
        import aiohttp
        from aiohttp import web
        board = request.match_info["uuid"]
        if not is_board_uuid(board):
            return web.json_response({"error": "not a board uuid"}, status=400)
        ident, err = await self.who(request, need_write=True)
        if err is not None:      # a Response is a mapping, and an empty one is falsy
            return err
        if self.o.fail_closed and not self.audit_ready():
            return web.json_response(
                {"error": "refused: the audit log cannot record it right now"},
                status=503)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)
        props = (body or {}).get("properties") if isinstance(body, dict) else None

        try:
            code, thing = await self.get_thing(board)
        except Exception as exc:
            return web.json_response({"error": "twin layer unreachable: %s" % exc},
                                     status=502)
        if thing is None:
            return web.json_response({"error": "no twin for this board"},
                                     status=404 if code == 404 else 502)
        attrs = thing.get("attributes") or {}
        rules = rules_for(self.allowlist(), attrs.get("boardType"), profile_of(attrs))
        clean, errors = validate_properties(props, rules)
        if errors:
            # Refusals are recorded too: an attempt to set something forbidden
            # is exactly what an auditor wants to see.
            await self.record(build_twin_record(
                ident["user"], ident["project"], board, attrs.get("boardName"),
                "set", {"properties": json_safe(props) if isinstance(props, dict)
                        else None,
                        "refused": errors},
                422, str(uuid.uuid4())))
            return web.json_response({"error": "refused", "details": errors},
                                     status=422)

        if FEATURE not in (thing.get("features") or {}):
            return web.json_response(
                {"error": "this twin has no %s feature yet: the device has "
                          "never reported, so it could not act on a desired "
                          "value" % FEATURE}, status=409)

        cid = str(uuid.uuid4())
        # The merge is addressed AT desiredProperties, not at the thing root.
        # That matters twice: the bridge only forwards events whose path
        # contains /desiredProperties (a root merge would never reach the
        # device), and the audit logger classifies on the same path.
        url = "%s/api/2/things/%s/features/%s/desiredProperties" % (
            self.o.ditto_url, self.thing_id(board), FEATURE)
        try:
            async with self.session.patch(
                    url, auth=self.ditto_auth(), data=json.dumps(clean),
                    headers={"Content-Type": "application/merge-patch+json",
                             "correlation-id": cid},
                    timeout=aiohttp.ClientTimeout(total=self.o.timeout)) as r:
                status = r.status
                text = (await r.text())[:300]
        except Exception as exc:
            return web.json_response({"error": "twin layer unreachable: %s" % exc},
                                     status=502)

        ok = 200 <= status < 300
        if ok:
            self.writes += 1
            now = time.time()
            for p in clean:
                self.set_at[(board, p)] = now
        await self.record(build_twin_record(
            ident["user"], ident["project"], board, attrs.get("boardName"), "set",
            {"properties": clean}, status, cid))
        LOG.info("desired.set %s %s by %s -> %s", board, clean, ident["user"], status)
        if not ok:
            return web.json_response(
                {"error": "twin layer refused the change (%s)" % status,
                 "detail": text}, status=502)
        return web.json_response({"ok": True, "correlation_id": cid,
                                  "properties": clean})

    async def clear_desired(self, request):
        import aiohttp
        from aiohttp import web
        board = request.match_info["uuid"]
        prop = request.match_info["prop"]
        if not is_board_uuid(board):
            return web.json_response({"error": "not a board uuid"}, status=400)
        if not PROP_RE.match(prop or ""):
            return web.json_response({"error": "not a valid property name"},
                                     status=400)
        ident, err = await self.who(request, need_write=True)
        if err is not None:      # a Response is a mapping, and an empty one is falsy
            return err
        if self.o.fail_closed and not self.audit_ready():
            return web.json_response(
                {"error": "refused: the audit log cannot record it right now"},
                status=503)
        # Clearing is allowed for ANY property, on the allow list or not: it
        # only withdraws a request, and it is how a stale or mistaken desired
        # value written by some other route gets cleaned up.
        cid = str(uuid.uuid4())
        url = "%s/api/2/things/%s/features/%s/desiredProperties/%s" % (
            self.o.ditto_url, self.thing_id(board), FEATURE, prop)
        try:
            async with self.session.delete(
                    url, auth=self.ditto_auth(), headers={"correlation-id": cid},
                    timeout=aiohttp.ClientTimeout(total=self.o.timeout)) as r:
                status = r.status
        except Exception as exc:
            return web.json_response({"error": "twin layer unreachable: %s" % exc},
                                     status=502)
        self.set_at.pop((board, prop), None)
        await self.record(build_twin_record(
            ident["user"], ident["project"], board, None, "clear",
            {"property": prop}, status, cid))
        LOG.info("desired.clear %s %s by %s -> %s", board, prop, ident["user"], status)
        if status == 404:
            return web.json_response({"error": "nothing desired for %s" % prop},
                                     status=404)
        if not 200 <= status < 300:
            return web.json_response(
                {"error": "twin layer refused the change (%s)" % status}, status=502)
        return web.json_response({"ok": True, "correlation_id": cid,
                                  "property": prop})

    # ---- lifecycle -------------------------------------------------------
    async def connect_amqp(self):
        """Keep trying in the background. The service answers reads, and in
        fail-open mode writes, while the broker is still coming up."""
        import aio_pika
        import urllib.parse as up
        url = ("amqp://%s:%s@%s:%d/%s" % (
            up.quote(self.o.amqp_user, safe=""), up.quote(self.o.amqp_pass, safe=""),
            self.o.amqp_host, self.o.amqp_port, up.quote(self.o.amqp_vhost, safe="")))
        while True:
            try:
                conn = await aio_pika.connect_robust(url)
                self.channel = await conn.channel()
                await self.channel.declare_queue(self.o.ingest_queue, durable=True)
                LOG.info("audit queue %s ready", self.o.ingest_queue)
                return
            except Exception as exc:
                LOG.warning("audit queue not reachable yet: %s", exc)
                await asyncio.sleep(5)

    async def heartbeat_loop(self):
        from pathlib import Path
        hb = Path(self.o.heartbeat_file)
        while True:
            # Healthy means: able to record. Serving without recording is the
            # one state this service must not look healthy in.
            if self.audit_ready():
                try:
                    hb.touch()
                except OSError as exc:
                    LOG.warning("cannot write heartbeat: %s", exc)
            await asyncio.sleep(5)

    async def run(self):
        import aiohttp
        from aiohttp import web
        self.session = aiohttp.ClientSession()
        app = web.Application(client_max_size=64 * 1024)
        app.router.add_get("/health", self.health)
        app.router.add_get("/twin/{uuid}/status", self.status)
        app.router.add_post("/twin/{uuid}/desired", self.set_desired)
        app.router.add_delete("/twin/{uuid}/desired/{prop}", self.clear_desired)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", self.o.port).start()
        LOG.info("listening on :%d; ditto=%s write_roles=%s allowlist=%s",
                 self.o.port, self.o.ditto_url, sorted(self.write_roles),
                 self.o.allowlist)
        if not self.write_roles:
            LOG.warning("TWIN_WRITE_ROLES is empty: every write will be refused")
        asyncio.ensure_future(self.connect_amqp())
        asyncio.ensure_future(self.heartbeat_loop())
        await asyncio.Event().wait()


def build_parser():
    e = os.environ.get
    p = argparse.ArgumentParser(description="Twin command service for Horizon.")
    p.add_argument("--port", type=int, default=int(e("TWIN_API_PORT", 8892)))
    p.add_argument("--keystone-url", default=e("OS_AUTH_URL", "http://keystone:5000"))
    p.add_argument("--token-cache-ttl", type=float,
                   default=float(e("TOKEN_CACHE_TTL", 60)))
    p.add_argument("--write-roles", default=e("TWIN_WRITE_ROLES", "admin"),
                   help="comma separated; a writer needs at least one")
    p.add_argument("--read-roles", default=e("TWIN_READ_ROLES", ""),
                   help="comma separated; empty means any valid token")
    p.add_argument("--allowlist", default=e("TWIN_ALLOWLIST",
                                            "/etc/s4t/twin_allowlist.json"))
    p.add_argument("--confirm-timeout", type=float,
                   default=float(e("TWIN_CONFIRM_TIMEOUT", 60)))
    p.add_argument("--timeout", type=float, default=float(e("TWIN_DITTO_TIMEOUT", 6)))
    p.add_argument("--fail-closed", action="store_true",
                   default=e("TWIN_FAIL_CLOSED", "") == "1")
    p.add_argument("--ditto-url", default=e("DITTO_URL", "http://ditto-nginx:80"))
    p.add_argument("--ditto-user", default=e("DITTO_USER", "s4t"))
    p.add_argument("--ditto-pass", default=e("DITTO_PASS", ""))
    p.add_argument("--ditto-namespace", default=e("DITTO_NAMESPACE", "s4t"))
    p.add_argument("--amqp-host", default=e("S4T_AMQP_HOST", "rabbitmq"))
    p.add_argument("--amqp-port", type=int, default=int(e("S4T_AMQP_PORT", 5672)))
    p.add_argument("--amqp-user", default=e("S4T_AMQP_USER", "openstack"))
    p.add_argument("--amqp-pass", default=e("S4T_AMQP_PASS", "unime"))
    p.add_argument("--amqp-vhost", default=e("S4T_AMQP_VHOST", "/"))
    p.add_argument("--ingest-queue", default=e("AUDIT_OPERATOR_QUEUE",
                                               "s4t.audit.ingest"))
    p.add_argument("--heartbeat-file", default=e("HEARTBEAT_FILE", "/tmp/twin-api-alive"))
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv=None):
    opts = build_parser().parse_args(argv)
    opts.ditto_url = opts.ditto_url.rstrip("/")
    logging.basicConfig(
        level=logging.DEBUG if opts.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    if not opts.ditto_pass:
        LOG.error("DITTO_PASS is not set")
        return 2
    try:
        asyncio.run(TwinApi(opts).run())
    except KeyboardInterrupt:
        LOG.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
