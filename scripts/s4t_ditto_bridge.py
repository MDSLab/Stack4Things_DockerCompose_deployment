#!/usr/bin/env python3
"""
s4t_ditto_bridge.py

The integration between Stack4Things and Eclipse Ditto.

Ditto speaks AMQP 0.9.1 but not WAMP. Stack4Things speaks WAMP but not AMQP.
This process is the only thing that speaks both. It runs as a standalone
component, connecting to Crossbar as an ordinary WAMP client and to the
RabbitMQ instance that is already part of the deployment, so no IoTronic
image is rebuilt and no existing service definition is edited.

    board --WAMP--> crossbar --> [bridge] --AMQP--> rabbitmq --> Ditto
    board <--WAMP-- crossbar <-- [bridge] <--AMQP-- rabbitmq <-- Ditto

DESIGN
------
Inbound the bridge is deliberately dumb. It receives a telemetry event,
serialises it to JSON, and publishes it. It does not build Ditto Protocol
messages and it does not know what a Thing is. All reshaping happens in the
JavaScript payload mapping attached to the Ditto connection, which means the
device wire format can change without redeploying this process.

Outbound it has to be less dumb, because it must decide whether a command is
worth sending at all. See compute_delta() and the comment above it.

PHASES
------
Phase 1 is the inbound direction: run with --no-outbound.
Phase 2 turns on the outbound direction: drop the flag.

DEPENDENCIES
------------
    pip install autobahn aio-pika

USAGE
-----
    # Phase 1: telemetry only
    ./s4t_ditto_bridge.py --no-outbound

    # Phase 2: both directions
    ./s4t_ditto_bridge.py

    # against containers by name, from inside the s4t network
    ./s4t_ditto_bridge.py --crossbar-host crossbar --amqp-host rabbitmq

Every setting can also come from an environment variable, see build_parser().
"""

import argparse
import asyncio
import json
import logging
import os
import ssl
import sys
import time

LOG = logging.getLogger("bridge")

# WAMP topic convention. Four dot separated components in both directions so
# that a wildcard subscription can be written unambiguously.
TELEMETRY_TOPIC = "s4t.telemetry.{uuid}.report"
COMMAND_TOPIC = "s4t.command.{uuid}.apply"

# A WAMP wildcard pattern must have the SAME number of components as the
# topics it matches, with empty strings in the wildcard positions.
# "s4t.telemetry..report" has four components and matches
# "s4t.telemetry.<uuid>.report" but not "s4t.command.<uuid>.apply", so this
# subscription can never be woken by the bridge's own outbound publications.
TELEMETRY_PATTERN = "s4t.telemetry..report"


# =========================================================================
# Pure functions. No I/O, so these are unit testable without a broker.
# =========================================================================
def build_inbound_payload(board_uuid, state, ts=None):
    """The JSON the bridge hands to Ditto for one telemetry report.

    Deliberately not Ditto Protocol. The connection's JavaScript payload
    mapping turns this into a merge command, so the shape below can change
    without touching this process.

    Returns None if the event is unusable, which is how malformed traffic on
    an anonymous WAMP bus gets dropped at the edge instead of becoming a
    confusing Ditto error later.
    """
    if not board_uuid or not isinstance(board_uuid, str):
        return None
    if not isinstance(state, dict) or not state:
        return None
    return {
        "board_uuid": board_uuid,
        "state": state,
        "ts": ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def board_uuid_from_topic(topic):
    """'s4t.telemetry.<uuid>.report' -> '<uuid>', or None."""
    parts = (topic or "").split(".")
    if len(parts) == 4 and parts[0] == "s4t" and parts[1] == "telemetry" \
            and parts[3] == "report" and parts[2]:
        return parts[2]
    return None


def parse_ditto_event(evt):
    """Pull what the outbound path needs out of one Ditto Protocol event.

    Returns (board_uuid, desired, reported) or None if this event is not a
    desired property change we should act on.

    Ditto's RQL target filters match on the state of the thing, not on which
    path changed, so a twin with any pending desire emits on every telemetry
    report too. The path check here is what stops those from being treated as
    commands.
    """
    if not isinstance(evt, dict):
        return None
    path = evt.get("path") or ""
    if "/desiredProperties" not in path:
        return None

    topic = evt.get("topic") or ""
    parts = topic.split("/")
    if len(parts) < 2 or not parts[1]:
        return None
    board_uuid = parts[1]

    value = evt.get("value")
    tail = path.rsplit("/", 1)[-1]
    if tail == "desiredProperties":
        desired = value if isinstance(value, dict) else None
    else:
        # a single desired property was set, so value is that property's value
        desired = {tail: value}
    if not isinstance(desired, dict) or not desired:
        return None

    # attached by the target's extraFields, see the Phase 2 guide
    reported = (((evt.get("extra") or {})
                 .get("features") or {})
                .get("telemetry") or {}).get("properties") or {}
    if not isinstance(reported, dict):
        reported = {}

    return board_uuid, desired, reported


def compute_delta(desired, reported):
    """Keys where the twin's desire and the board's report disagree.

    Forwarding `desired` wholesale would emit a command on every telemetry
    report for as long as a desire is set, forever, including long after the
    board has complied. Comparing instead gives convergence for free and is
    self terminating: the board complies, reports the new value, the next
    event produces an empty delta, and the traffic stops with no
    acknowledgement protocol and no bookkeeping.
    """
    return {k: v for k, v in desired.items() if reported.get(k) != v}


# =========================================================================
# I/O
# =========================================================================
def insecure_ssl_context():
    """Crossbar in this deployment serves TLS only, with a self signed
    certificate issued by the local ca_service, so verification is disabled
    exactly as conf_wagent/iotronic.conf already does with skip_cert_verify."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class Bridge:
    def __init__(self, opts):
        self.o = opts
        self.session = None       # set once WAMP joins AND subscribes
        self.subscription = None  # None => receiving nothing => not healthy
        self.amqp_channel = None
        self.sent_in = self.sent_out = self.dropped = 0

    # ------------------------------------------------------------- inbound
    def on_telemetry(self, *args, **kwargs):
        """WAMP subscription handler. Boards publish with keyword arguments."""
        board_uuid = kwargs.get("board_uuid")
        state = kwargs.get("state")
        ts = kwargs.get("ts")

        payload = build_inbound_payload(board_uuid, state, ts)
        if payload is None:
            self.dropped += 1
            LOG.warning("dropped malformed telemetry: board_uuid=%r state=%r",
                        board_uuid, state)
            return
        asyncio.ensure_future(self._publish_inbound(payload))

    async def _publish_inbound(self, payload):
        import aio_pika
        try:
            # Publishing to the default exchange with the queue name as the
            # routing key delivers straight to that queue, so no exchange of
            # our own is needed for the inbound direction.
            await self.amqp_channel.default_exchange.publish(
                aio_pika.Message(
                    body=json.dumps(payload).encode(),
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=self.o.inbound_queue,
            )
            self.sent_in += 1
            LOG.info("-> ditto  %s %s", payload["board_uuid"], payload["state"])
        except Exception as exc:
            LOG.error("failed to publish telemetry for %s: %s",
                      payload.get("board_uuid"), exc)

    # ------------------------------------------------------------ outbound
    async def on_ditto_command(self, message):
        async with message.process():
            try:
                evt = json.loads(message.body.decode())
            except ValueError:
                LOG.warning("outbound message is not JSON, ignoring")
                return

            parsed = parse_ditto_event(evt)
            if parsed is None:
                LOG.debug("outbound event is not a desired property change")
                return
            board_uuid, desired, reported = parsed

            delta = compute_delta(desired, reported)
            if not delta:
                LOG.info("   board %s already converged, nothing to send",
                         board_uuid)
                return

            if self.session is None:
                LOG.error("WAMP session not joined yet, cannot deliver to %s",
                          board_uuid)
                return

            from autobahn.wamp.types import PublishOptions
            topic = COMMAND_TOPIC.format(uuid=board_uuid)
            # retain=True so a board that reconnects between messages still
            # receives the outstanding command immediately, rather than
            # waiting for whatever causes the next event.
            result = self.session.publish(
                topic, delta, options=PublishOptions(retain=True))
            # A plain publish returns None rather than an awaitable in this
            # version of autobahn, and awaiting None raises TypeError.
            if result is not None:
                await result
            self.sent_out += 1
            LOG.info("<- board  %s %s", board_uuid, delta)

    # -------------------------------------------------------------- set up
    def build_wamp_component(self):
        from autobahn.asyncio.component import Component
        comp = Component(
            transports=[{
                "type": "websocket",
                "url": f"wss://{self.o.crossbar_host}:{self.o.crossbar_port}/",
                "endpoint": {
                    "type": "tcp",
                    "host": self.o.crossbar_host,
                    "port": self.o.crossbar_port,
                    "tls": insecure_ssl_context(),
                },
                "max_retries": -1,
                # Pinned rather than left to autobahn's default of
                # ['cbor', 'json']. CBOR needs the cbor2 package; without it
                # every connect fails with "could not create serializer for
                # cbor". cbor2 is in the image, but this stays configurable so
                # a deployment that loses it can fall back with
                # S4T_WAMP_SERIALIZERS=json and no rebuild.
                "serializers": self.o.wamp_serializers,
            }],
            realm=self.o.realm,
        )

        @comp.on_join
        async def joined(session, details):
            # autobahn asserts isinstance(options, SubscribeOptions); a plain
            # dict raises a bare AssertionError inside the join callback, which
            # autobahn logs and swallows, leaving a joined session subscribed
            # to nothing.
            from autobahn.wamp.types import SubscribeOptions
            try:
                self.subscription = await session.subscribe(
                    self.on_telemetry, TELEMETRY_PATTERN,
                    options=SubscribeOptions(match="wildcard"))
            except Exception:
                # Do NOT set self.session: a bridge that cannot subscribe
                # forwards no telemetry and must not report itself healthy.
                self.subscription = None
                LOG.exception("WAMP subscribe to %s FAILED; the bridge is "
                              "connected but deaf, leaving the session",
                              TELEMETRY_PATTERN)
                await session.leave()
                return
            self.session = session
            LOG.info("WAMP joined realm '%s', subscribed to %s",
                     self.o.realm, TELEMETRY_PATTERN)

        @comp.on_leave
        def left(session, details):
            self.session = None
            self.subscription = None
            LOG.warning("WAMP session left: %s", getattr(details, "reason", details))

        return comp

    async def setup_amqp(self):
        import aio_pika
        # The path after the port is the VHOST, URL encoded. A bare trailing
        # slash means the EMPTY vhost, which RabbitMQ rejects with
        # 530 NOT_ALLOWED. aio-pika happens to fall back to "/" for an empty
        # path, but the Java client Ditto uses does not, so both ends are
        # written explicitly here to keep them provably identical.
        import urllib.parse as _u
        url = (f"amqp://{_u.quote(self.o.amqp_user, safe='')}"
               f":{_u.quote(self.o.amqp_pass, safe='')}"
               f"@{self.o.amqp_host}:{self.o.amqp_port}"
               f"/{_u.quote(self.o.amqp_vhost, safe='')}")
        conn = await aio_pika.connect_robust(url)
        self.amqp_channel = await conn.channel()

        # Inbound: Ditto's connection source consumes this queue.
        await self.amqp_channel.declare_queue(self.o.inbound_queue, durable=True)
        LOG.info("AMQP queue '%s' ready (telemetry into Ditto)", self.o.inbound_queue)

        if not self.o.no_outbound:
            # Outbound: Ditto's connection target publishes to
            # "<exchange>/<routing key>", so both must exist and the queue we
            # consume has to be bound to them.
            exchange = await self.amqp_channel.declare_exchange(
                self.o.outbound_exchange, aio_pika.ExchangeType.DIRECT, durable=True)
            queue = await self.amqp_channel.declare_queue(
                self.o.outbound_queue, durable=True)
            await queue.bind(exchange, routing_key=self.o.outbound_key)
            await queue.consume(self.on_ditto_command)
            LOG.info("AMQP exchange '%s' key '%s' -> queue '%s' ready "
                     "(commands out of Ditto)", self.o.outbound_exchange,
                     self.o.outbound_key, self.o.outbound_queue)
        else:
            LOG.info("outbound direction disabled (--no-outbound), Phase 1 mode")

        return conn

    async def heartbeat_loop(self):
        """Touch a file ONLY while both connections are genuinely live.

        A container healthcheck that merely proves the process exists is
        nearly useless: the bridge can be running with a dropped WAMP session
        and forwarding nothing. Gating the touch on the handles being present
        makes `docker compose ps` mean "connected and working".

        The subscription is checked too, not just the session. A failed
        subscribe leaves a perfectly good WAMP session that receives nothing,
        and that state reported itself healthy until this check was added.
        """
        from pathlib import Path
        hb = Path(self.o.heartbeat_file)
        while True:
            if (self.session is not None and self.subscription is not None
                    and self.amqp_channel is not None):
                try:
                    hb.touch()
                except OSError as exc:
                    LOG.warning("cannot write heartbeat %s: %s", hb, exc)
            await asyncio.sleep(self.o.heartbeat_interval)

    async def report_loop(self):
        while True:
            await asyncio.sleep(self.o.stats_interval)
            LOG.info("stats: telemetry_in=%d commands_out=%d dropped=%d",
                     self.sent_in, self.sent_out, self.dropped)


async def amain(opts):
    bridge = Bridge(opts)
    loop = asyncio.get_running_loop()

    amqp_conn = await bridge.setup_amqp()

    comp = bridge.build_wamp_component()
    # Component.start() returns a future that resolves when the component is
    # done. Using it rather than autobahn's run() keeps this loop ours, so
    # aio-pika can share it. It also avoids autobahn's run(), which calls the
    # old asyncio.get_event_loop() API and raises RuntimeError on Python 3.12+.
    wamp_done = comp.start(loop)

    stats = asyncio.ensure_future(bridge.report_loop())
    beat = asyncio.ensure_future(bridge.heartbeat_loop())
    try:
        await wamp_done
    finally:
        stats.cancel()
        beat.cancel()
        await amqp_conn.close()


def build_parser():
    p = argparse.ArgumentParser(
        description="Bridge Stack4Things WAMP telemetry into Eclipse Ditto over AMQP.")

    g = p.add_argument_group("Crossbar / WAMP")
    g.add_argument("--crossbar-host", default=os.environ.get("S4T_CROSSBAR_HOST", "localhost"))
    g.add_argument("--crossbar-port", type=int,
                   default=int(os.environ.get("S4T_CROSSBAR_PORT", "8181")))
    g.add_argument("--realm", default=os.environ.get("S4T_WAMP_REALM", "s4t"))
    g.add_argument("--wamp-serializers",
                   default=os.environ.get("S4T_WAMP_SERIALIZERS", "cbor,json"),
                   type=lambda v: [x.strip() for x in v.split(",") if x.strip()],
                   help="Comma separated, in preference order. 'cbor' requires cbor2.")

    g = p.add_argument_group("RabbitMQ / AMQP")
    g.add_argument("--amqp-host", default=os.environ.get("S4T_AMQP_HOST", "localhost"))
    g.add_argument("--amqp-port", type=int, default=int(os.environ.get("S4T_AMQP_PORT", "5672")))
    g.add_argument("--amqp-user", default=os.environ.get("S4T_AMQP_USER", "openstack"))
    g.add_argument("--amqp-pass", default=os.environ.get("S4T_AMQP_PASS", "unime"))
    g.add_argument("--amqp-vhost", default=os.environ.get("S4T_AMQP_VHOST", "/"),
                   help="Default RabbitMQ vhost is '/', encoded to %%2F in the URI")
    g.add_argument("--inbound-queue", default=os.environ.get("DITTO_INBOUND_QUEUE", "ditto.inbound"),
                   help="Queue Ditto's connection source consumes")
    g.add_argument("--outbound-exchange", default=os.environ.get("DITTO_OUTBOUND_EXCHANGE", "ditto"))
    g.add_argument("--outbound-key", default=os.environ.get("DITTO_OUTBOUND_KEY", "outbound"))
    g.add_argument("--outbound-queue", default=os.environ.get("DITTO_OUTBOUND_QUEUE", "ditto.commands"),
                   help="Queue this bridge consumes commands from")

    g = p.add_argument_group("Behaviour")
    g.add_argument("--no-outbound", action="store_true",
                   help="Phase 1 mode: carry telemetry into Ditto only, do not "
                        "consume or deliver commands")
    g.add_argument("--stats-interval", type=float, default=60.0)
    g.add_argument("--heartbeat-file",
                   default=os.environ.get("HEARTBEAT_FILE", "/tmp/bridge-alive"),
                   help="Touched while the WAMP session and AMQP channel are both live. "
                        "The container healthcheck tests its age.")
    g.add_argument("--heartbeat-interval", type=float,
                   default=float(os.environ.get("HEARTBEAT_INTERVAL", "5")))
    g.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv=None):
    opts = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if opts.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    LOG.info("bridge starting: crossbar=%s:%s realm=%s amqp=%s:%s outbound=%s",
             opts.crossbar_host, opts.crossbar_port, opts.realm,
             opts.amqp_host, opts.amqp_port, not opts.no_outbound)
    try:
        asyncio.run(amain(opts))
    except KeyboardInterrupt:
        LOG.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
