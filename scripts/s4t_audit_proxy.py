#!/usr/bin/env python3
"""
s4t_audit_proxy.py

Stage L4. Captures WHO performed an action on a machine.

    Horizon --> [ this ] --> iotronic-conductor:8812
                    |
                    +--> queue s4t.audit.ingest --> the audit logger

WHY A PROXY, AND WHY THIS IS THE ONLY PLACE IT CAN BE DONE
----------------------------------------------------------
Nothing in the deployment records who did anything. Ditto authenticates every
twin write as one shared subject, `nginx:s4t`, so its history says what changed
and never who changed it. The IoTronic `boards` table records who OWNS a board,
not who acted on it. And no action log exists anywhere in the schema.

Operator actions happen in Horizon and reach the conductor over HTTP with a
Keystone token attached, because the conductor runs with auth_strategy=keystone.
That token is the only place a real human identity exists, so this is where the
capture has to happen.

HOW IT IS INSERTED WITHOUT CHANGING ANYTHING
--------------------------------------------
Horizon does not hold a conductor URL. It resolves the service through the
Keystone catalog. Repointing only the PUBLIC endpoint at this proxy is a single
command and leaves internal and admin traffic going straight to the conductor:

    openstack endpoint set <public-endpoint-id> \\
        --url http://s4t-audit-proxy:8812

No image is rebuilt, no service definition is edited, and Horizon is untouched.

THE TOKEN IS VALIDATED, NOT TRUSTED
-----------------------------------
The username comes from asking Keystone to validate the token, not from a
client supplied header. A header could be set by anyone able to reach this
port, which would make the whole attribution claim false. Test T3 checks this
by performing the same action as two different users.

FAILURE MODE
------------
Default is FAIL OPEN: if recording fails the request is still forwarded and the
loss is counted, so a logging fault cannot stop machine operation. The audit
log's `sequence` makes the loss visible rather than silent. Set
AUDIT_FAIL_CLOSED=1 to refuse requests that cannot be recorded.

DEPENDENCIES
------------
    pip install aiohttp aio-pika

USAGE
-----
    ./s4t_audit_proxy.py
    ./s4t_audit_proxy.py --upstream http://iotronic-conductor:8812 -v
"""

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
import uuid
from datetime import datetime, timezone

LOG = logging.getLogger("audit-proxy")

SCHEMA_VERSION = "1.0"
HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer",
              "proxy-authorization", "proxy-authenticate", "upgrade", "host"}


# =========================================================================
# Pure functions
# =========================================================================
def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def classify_request(method, path):
    """(machine_action, machine_id or None).

    A closed vocabulary, so the ledger side can filter without parsing free
    text. Anything unrecognised still produces a record, because an action
    nobody anticipated is exactly what an auditor wants to see.

    ORDER IS LOAD BEARING. A plugin and a service both live UNDER a board, so
    `/v1/boards/<id>/plugins/<id>` contains both "/boards" and "/plugins". The
    first version of this function tested "/boards" first, which classified
    every plugin removal as `board.delete` and every service call on a board as
    a board operation. Nothing failed, no record was lost, and the vocabulary
    silently collapsed into the wrong verbs. The vocabulary test found it.
    Match the most specific marker first, and keep the branches exclusive.

    The identifier taken is the FIRST UUID in the path, which on a nested path
    is the board. That is deliberate: `machine_id` answers "which machine was
    this done to", not "which plugin was it done with". The plugin identifier
    stays visible in `action.target`.
    """
    m = UUID_RE.search(path or "")
    machine_id = m.group(0) if m else None
    p = (path or "").lower()
    is_action = "/actions" in p or "/action" in p

    if "/plugins" in p:
        if is_action:
            return "plugin.execute", machine_id
        if method == "POST":
            return "plugin.inject", machine_id
        if method == "PUT":
            return "plugin.execute", machine_id
        if method == "DELETE":
            return "plugin.remove", machine_id
        if method == "GET":
            return "plugin.read", machine_id
    elif "/services" in p:
        if method in ("POST", "PUT"):
            return "service.expose", machine_id
        if method == "DELETE":
            return "service.remove", machine_id
        if method == "GET":
            return "service.read", machine_id
    elif "/boards" in p:
        if is_action:
            return "board.action", machine_id
        if method == "POST" and not machine_id:
            return "board.create", None
        if method == "DELETE":
            return "board.delete", machine_id
        if method in ("PATCH", "PUT"):
            return "board.update", machine_id
        if method == "GET":
            return "board.read", machine_id
    elif is_action:
        return "board.action", machine_id

    return f"api.{method.lower()}", machine_id


def build_operator_record(user, project, method, path, status, machine_name=None,
                          detail=None, correlation_id=None):
    """One operator request -> one audit record, minus the sequence, which the
    logger assigns because it is the single writer."""
    action, machine_id = classify_request(method, path)
    outcome = "success" if 200 <= int(status) < 400 else "failure"
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(uuid.uuid4()),
        "timestamp": now_iso(),
        "operator_name": user,
        "machine_name": machine_name or machine_id or "unknown",
        "machine_action": action,
        "actor": {"type": "human", "id": None, "name": user, "project": project},
        "machine": {"id": machine_id, "name": machine_name or machine_id,
                    "namespace": None},
        "action": {"verb": action.split(".", 1)[-1], "target": path,
                   "detail": detail or {"method": method},
                   "outcome": outcome, "status_code": int(status)},
        "source": "horizon",
        "twin_revision": None,
        "correlation_id": correlation_id,
    }


# =========================================================================
# Keystone token resolution, cached
# =========================================================================
class TokenResolver:
    """Resolves an X-Auth-Token to a username by asking Keystone.

    Cached for the token's lifetime, because Horizon sends the same token on
    every request and validating each one would put a Keystone round trip in
    the path of every operator click.
    """

    def __init__(self, keystone_url, ttl):
        self.url = keystone_url.rstrip("/")
        self.ttl = ttl
        self.cache = {}

    async def resolve(self, session, token):
        if not token:
            return None, None
        hit = self.cache.get(token)
        if hit and hit[2] > time.time():
            return hit[0], hit[1]
        try:
            async with session.get(
                f"{self.url}/v3/auth/tokens",
                headers={"X-Auth-Token": token, "X-Subject-Token": token},
                timeout=__import__("aiohttp").ClientTimeout(total=5),
            ) as r:
                if r.status != 200:
                    LOG.warning("token validation returned %s", r.status)
                    return None, None
                body = await r.json()
        except Exception as exc:
            LOG.warning("cannot validate token with Keystone: %s", exc)
            return None, None

        tok = (body or {}).get("token") or {}
        user = (tok.get("user") or {}).get("name")
        project = (tok.get("project") or {}).get("id")
        if user:
            self.cache[token] = (user, project, time.time() + self.ttl)
        return user, project


# =========================================================================
# Keystone catalog binding
# =========================================================================
class CatalogBinder:
    """Points the PUBLIC `iot` endpoint at this proxy, and puts it back on exit.

    OFF BY DEFAULT. READ THIS BEFORE ENABLING IT.
    ---------------------------------------------
    In this deployment the catalog is set by Keystone's own setup block, via
    IOT_PUBLIC_ENDPOINT_URL, which the audit overlay points here. That is the
    better place: Keystone already owns the catalog and already holds the
    rights to change it, whereas this process sits in the request path of every
    operator action and giving it the power to rewrite service discovery widens
    the blast radius of any fault or compromise in it.

    What this class buys, and the only reason it still exists, is the RESTORE.
    Whatever writes the catalog is the only thing that can put it back on
    SIGTERM, and without that, stopping this container leaves Horizon pointing
    at an address nothing answers on. Keystone cannot do that, because it does
    not know when this container stops.

    So: enable it (AUDIT_BIND_ENDPOINT=1, plus OS_USERNAME and OS_PASSWORD) only
    if you want stop and restart to be self-healing and accept the credential in
    return. Otherwise leave it off and rely on `restart: unless-stopped` and
    check 1 of verify_audit_logger.sh, which fails when the catalog does not
    point here.

    WHY RESTORING MATTERS MORE THAN BINDING
    ---------------------------------------
    The catalog entry outlives this container. Without a restore, stopping the
    proxy leaves Horizon pointed at an address nothing answers on, and the
    dashboard breaks completely with an error that says nothing about the
    audit log. Restoring on shutdown means `docker compose stop` leaves a
    working system.

    This runs on SIGTERM, which covers `stop`, `down` and `restart`. It cannot
    run on SIGKILL or a hard host failure, so the catalog can still be left
    dangling; `verify_audit_logger.sh` checks the endpoint for that reason.

    SCOPE
    -----
    It touches exactly one object: the `public` interface of the `iot` service.
    It reads the current value first and does nothing if it already matches, so
    it is idempotent and safe on every restart. Credentials come from the
    environment and are never logged.
    """

    def __init__(self, opts):
        self.o = opts
        self.auth = opts.keystone_url.rstrip("/")
        if self.auth.endswith("/v3"):
            self.auth = self.auth[:-3].rstrip("/")
        self.endpoint_id = None
        self.original_url = None

    async def _token(self, session):
        """A project scoped admin token. Returns None rather than raising: a
        catalog that cannot be bound must not stop the proxy from forwarding."""
        body = {"auth": {
            "identity": {"methods": ["password"], "password": {"user": {
                "name": self.o.os_username,
                "domain": {"name": self.o.os_user_domain},
                "password": self.o.os_password}}},
            "scope": {"project": {
                "name": self.o.os_project,
                "domain": {"name": self.o.os_project_domain}}}}}
        async with session.post(f"{self.auth}/v3/auth/tokens", json=body) as r:
            if r.status not in (200, 201):
                LOG.warning("catalog: Keystone refused the admin login (%s)", r.status)
                return None
            return r.headers.get("X-Subject-Token")

    async def _find(self, session, token):
        """(endpoint_id, current_url) for the public endpoint of the service."""
        h = {"X-Auth-Token": token}
        async with session.get(f"{self.auth}/v3/services",
                               params={"type": self.o.service_type}, headers=h) as r:
            services = (await r.json()).get("services", [])
        if not services:
            LOG.warning("catalog: no service of type '%s' exists", self.o.service_type)
            return None, None
        sid = services[0]["id"]
        async with session.get(f"{self.auth}/v3/endpoints",
                               params={"service_id": sid, "interface": "public"},
                               headers=h) as r:
            eps = (await r.json()).get("endpoints", [])
        if self.o.os_region:
            eps = [e for e in eps if e.get("region") in (None, self.o.os_region)] or eps
        if not eps:
            LOG.warning("catalog: '%s' has no public endpoint to repoint",
                        self.o.service_type)
            return None, None
        return eps[0]["id"], eps[0].get("url")

    async def _set(self, session, token, url):
        async with session.patch(
                f"{self.auth}/v3/endpoints/{self.endpoint_id}",
                json={"endpoint": {"url": url}},
                headers={"X-Auth-Token": token}) as r:
            if r.status not in (200, 201):
                LOG.error("catalog: could not set the endpoint (%s)", r.status)
                return False
        return True

    async def bind(self, session):
        try:
            token = await self._token(session)
            if not token:
                return
            self.endpoint_id, self.original_url = await self._find(session, token)
            if not self.endpoint_id:
                return
            if self.original_url == self.o.public_url:
                LOG.info("catalog: public '%s' endpoint already points here (%s)",
                         self.o.service_type, self.o.public_url)
                # Remember the conductor, not ourselves, or a restart would
                # "restore" the catalog to the proxy and Horizon would break
                # the moment this container stopped.
                self.original_url = self.o.upstream
                return
            if await self._set(session, token, self.o.public_url):
                LOG.info("catalog: public '%s' endpoint moved %s -> %s",
                         self.o.service_type, self.original_url, self.o.public_url)
        except Exception as exc:
            LOG.error("catalog: binding failed, operator actions will NOT be "
                      "recorded until the endpoint is repointed: %s", exc)

    async def restore(self, session):
        if not self.endpoint_id or not self.original_url:
            return
        try:
            token = await self._token(session)
            if token and await self._set(session, token, self.original_url):
                LOG.info("catalog: public '%s' endpoint restored to %s",
                         self.o.service_type, self.original_url)
        except Exception as exc:
            LOG.error("catalog: restore failed, Horizon may be left pointing at "
                      "a stopped container. Repoint it by hand: %s", exc)


# =========================================================================
# The proxy
# =========================================================================
class Proxy:
    def __init__(self, opts):
        self.o = opts
        self.resolver = TokenResolver(opts.keystone_url, opts.token_cache_ttl)
        self.binder = CatalogBinder(opts)
        self.channel = None
        self.stop = None
        self.forwarded = self.recorded = self.record_failures = 0

    def _handle_signals(self):
        """SIGTERM is what `docker compose stop` sends. Catching it is the only
        chance to put the catalog back before the container disappears."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stop.set)
            except NotImplementedError:          # not available on every platform
                pass

    async def run(self):
        import aio_pika
        import aiohttp
        from aiohttp import web
        import urllib.parse as up

        url = (f"amqp://{up.quote(self.o.amqp_user, safe='')}"
               f":{up.quote(self.o.amqp_pass, safe='')}"
               f"@{self.o.amqp_host}:{self.o.amqp_port}"
               f"/{up.quote(self.o.amqp_vhost, safe='')}")
        conn = await aio_pika.connect_robust(url)
        self.channel = await conn.channel()
        await self.channel.declare_queue(self.o.ingest_queue, durable=True)
        LOG.info("proxy starting: upstream=%s ingest=%s fail_closed=%s",
                 self.o.upstream, self.o.ingest_queue, self.o.fail_closed)

        self.session = aiohttp.ClientSession()
        app = web.Application(client_max_size=self.o.max_body)
        app.router.add_route("*", "/{tail:.*}", self.handle)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", self.o.port).start()
        LOG.info("listening on :%d, forwarding to %s", self.o.port, self.o.upstream)

        self.stop = asyncio.Event()
        self._handle_signals()
        asyncio.ensure_future(self.heartbeat_loop())

        # Bind AFTER the socket is accepting. Repointing the catalog at a port
        # that is not yet listening would send Horizon to a closed door.
        if self.o.bind_endpoint:
            await self.binder.bind(self.session)
        else:
            LOG.info("catalog binding disabled (this process holds no "
                     "credentials). Keystone sets the public endpoint from "
                     "IOT_PUBLIC_ENDPOINT_URL; if it does not point here, "
                     "nothing operator-initiated can be recorded")

        await self.stop.wait()

        LOG.info("shutting down")
        if self.o.bind_endpoint:
            try:
                await asyncio.wait_for(self.binder.restore(self.session), timeout=8)
            except asyncio.TimeoutError:
                LOG.error("catalog: restore timed out; Horizon may still point here")
        await runner.cleanup()
        await self.session.close()

    async def handle(self, request):
        from aiohttp import web
        body = await request.read()
        token = request.headers.get("X-Auth-Token")
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in HOP_BY_HOP}
        target = self.o.upstream.rstrip("/") + request.rel_url.path_qs
        correlation = request.headers.get("X-Openstack-Request-Id") or str(uuid.uuid4())

        try:
            async with self.session.request(
                request.method, target, headers=headers, data=body,
                allow_redirects=False,
            ) as up_resp:
                payload = await up_resp.read()
                status = up_resp.status
                out_headers = {k: v for k, v in up_resp.headers.items()
                               if k.lower() not in HOP_BY_HOP
                               and k.lower() != "content-encoding"}
        except Exception as exc:
            LOG.error("upstream unreachable: %s", exc)
            return web.json_response({"error": "upstream unreachable"}, status=502)

        self.forwarded += 1

        recorded = await self.record(request, token, status, correlation)
        if not recorded and self.o.fail_closed:
            return web.json_response(
                {"error": "action refused: it could not be recorded in the audit log"},
                status=503)

        return web.Response(body=payload, status=status, headers=out_headers)

    async def record(self, request, token, status, correlation):
        """Returns True when the record reached the queue."""
        import aio_pika
        # Reads are the overwhelming majority of Horizon traffic and are not
        # machine actions. Recording them would bury the operator actions the
        # ledger cares about under thousands of page loads.
        if request.method == "GET" and not self.o.record_reads:
            return True
        try:
            user, project = await self.resolver.resolve(self.session, token)
            rec = build_operator_record(
                user=user, project=project, method=request.method,
                path=request.rel_url.path, status=status,
                detail={"method": request.method, "query": dict(request.query)},
                correlation_id=correlation)
            await self.channel.default_exchange.publish(
                aio_pika.Message(
                    body=json.dumps(rec, separators=(",", ":"), sort_keys=True).encode(),
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                routing_key=self.o.ingest_queue)
            self.recorded += 1
            LOG.info("recorded %s %s by %s -> %s", request.method,
                     request.rel_url.path, user, status)
            return True
        except Exception as exc:
            self.record_failures += 1
            LOG.error("FAILED to record %s %s: %s", request.method,
                      request.rel_url.path, exc)
            return False

    async def heartbeat_loop(self):
        from pathlib import Path
        hb = Path(self.o.heartbeat_file)
        while True:
            if self.channel is not None and not self.channel.is_closed:
                try:
                    hb.touch()
                except OSError as exc:
                    LOG.warning("cannot write heartbeat: %s", exc)
            await asyncio.sleep(5)


def build_parser():
    p = argparse.ArgumentParser(description="Audit proxy in front of the conductor.")
    p.add_argument("--port", type=int, default=int(os.environ.get("PROXY_PORT", 8812)))
    p.add_argument("--upstream", default=os.environ.get(
        "PROXY_UPSTREAM", "http://iotronic-conductor:8812"))
    p.add_argument("--keystone-url", default=os.environ.get(
        "OS_AUTH_URL", "http://keystone:5000"))
    p.add_argument("--token-cache-ttl", type=float,
                   default=float(os.environ.get("TOKEN_CACHE_TTL", 900)))
    p.add_argument("--record-reads", action="store_true",
                   default=os.environ.get("AUDIT_RECORD_READS", "") == "1")
    p.add_argument("--fail-closed", action="store_true",
                   default=os.environ.get("AUDIT_FAIL_CLOSED", "") == "1")
    p.add_argument("--max-body", type=int, default=16 * 1024 * 1024)

    c = p.add_argument_group(
        "Keystone catalog binding",
        "Repoints the public endpoint of the service at this proxy on startup "
        "and restores it on SIGTERM. Needs admin credentials.")
    c.add_argument("--no-bind-endpoint", dest="bind_endpoint", action="store_false",
                   default=os.environ.get("AUDIT_BIND_ENDPOINT", "1") != "0")
    c.add_argument("--service-type", default=os.environ.get("AUDIT_SERVICE_TYPE", "iot"))
    c.add_argument("--public-url", default=os.environ.get("AUDIT_PUBLIC_URL", ""))
    c.add_argument("--os-username", default=os.environ.get("OS_USERNAME", "admin"))
    c.add_argument("--os-password", default=os.environ.get("OS_PASSWORD", ""))
    c.add_argument("--os-project", default=os.environ.get("OS_PROJECT_NAME", "admin"))
    c.add_argument("--os-user-domain",
                   default=os.environ.get("OS_USER_DOMAIN_NAME", "Default"))
    c.add_argument("--os-project-domain",
                   default=os.environ.get("OS_PROJECT_DOMAIN_NAME", "Default"))
    c.add_argument("--os-region", default=os.environ.get("REGION_NAME", ""))

    g = p.add_argument_group("AMQP")
    g.add_argument("--amqp-host", default=os.environ.get("S4T_AMQP_HOST", "rabbitmq"))
    g.add_argument("--amqp-port", type=int, default=int(os.environ.get("S4T_AMQP_PORT", 5672)))
    g.add_argument("--amqp-user", default=os.environ.get("S4T_AMQP_USER", "openstack"))
    g.add_argument("--amqp-pass", default=os.environ.get("S4T_AMQP_PASS", "unime"))
    g.add_argument("--amqp-vhost", default=os.environ.get("S4T_AMQP_VHOST", "/"))
    g.add_argument("--ingest-queue", default=os.environ.get("AUDIT_OPERATOR_QUEUE",
                                                            "s4t.audit.ingest"))
    p.add_argument("--heartbeat-file", default=os.environ.get("HEARTBEAT_FILE",
                                                              "/tmp/proxy-alive"))
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv=None):
    opts = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if opts.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    # Default the advertised URL to this container's own name and port, so the
    # value in the catalog cannot drift from the port actually being served.
    if not opts.public_url:
        opts.public_url = f"http://s4t-audit-proxy:{opts.port}"
    if opts.bind_endpoint and not opts.os_password:
        opts.bind_endpoint = False
        LOG.warning("no OS_PASSWORD, so the catalog will not be bound "
                    "automatically; repoint the public endpoint by hand")
    try:
        asyncio.run(Proxy(opts).run())
    except KeyboardInterrupt:
        LOG.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
