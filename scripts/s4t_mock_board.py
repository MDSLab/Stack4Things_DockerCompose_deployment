#!/usr/bin/env python3
"""
s4t_mock_board.py

Stands in for a physical board, or for lightning-rod, when no hardware is
attached. It speaks the same WAMP topics a real board would, so nothing
downstream can tell the difference: the bridge, Ditto, the Explorer UI and
the Phase 3 predictor all see an ordinary device.

    publishes  s4t.telemetry.<uuid>.report   with board_uuid, state, ts
    subscribes s4t.command.<uuid>.apply      and applies the delta it receives

Run it against a board UUID that already exists in the `boards` table, so the
twin the provisioner creates and the telemetry this produces line up.

DEPENDENCIES
------------
    pip install autobahn

USAGE
-----
    ./s4t_mock_board.py <board_uuid>
    ./s4t_mock_board.py <board_uuid> --host crossbar --interval 2
    ./s4t_mock_board.py <board_uuid> --drift 0.8 --spike-every 20
"""

import argparse
import asyncio
import random
import ssl
import sys
import time


def insecure_ssl_context():
    """Crossbar here serves TLS only with a self signed certificate from the
    local ca_service, so verification is disabled."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def publish(session, topic, **kwargs):
    """session.publish() returns an awaitable only when an acknowledgement is
    requested. A plain fire and forget publish returns None in this version of
    autobahn, and awaiting None raises TypeError."""
    result = session.publish(topic, **kwargs)
    if result is not None:
        await result


def build_component(args, state):
    from autobahn.asyncio.component import Component

    comp = Component(
        transports=[{
            "type": "websocket",
            "url": f"wss://{args.host}:{args.port}/",
            "endpoint": {"type": "tcp", "host": args.host, "port": args.port,
                         "tls": insecure_ssl_context()},
            "max_retries": -1,
        }],
        realm=args.realm,
    )

    @comp.on_join
    async def joined(session, details):
        print(f"[board {args.board_uuid[:8]}] joined realm '{args.realm}'")

        def apply_delta(delta):
            if not isinstance(delta, dict):
                print(f"[board] ignoring malformed command: {delta!r}")
                return
            state.update(delta)
            print(f"[board] applied {delta}, state now {state}")

        # Subscribing here also picks up a retained command published while
        # this board was offline, which is how an outstanding instruction
        # survives a disconnect.
        await session.subscribe(lambda delta: apply_delta(delta),
                                f"s4t.command.{args.board_uuid}.apply")
        print(f"[board] listening on s4t.command.{args.board_uuid}.apply")

        tick = 0
        while True:
            tick += 1
            state["temperature"] = round(
                state["temperature"] + random.uniform(-args.drift, args.drift), 2)
            if args.spike_every and tick % args.spike_every == 0:
                state["temperature"] = round(state["temperature"] + args.spike, 2)
                print(f"[board] injected a fault spike")

            await publish(session,
                          f"s4t.telemetry.{args.board_uuid}.report",
                          board_uuid=args.board_uuid,
                          state=dict(state),
                          ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            print(f"[board] reported {state}")
            await asyncio.sleep(args.interval)

    return comp


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("board_uuid", help="A uuid from the boards table")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=8181)
    p.add_argument("--realm", default="s4t")
    p.add_argument("--interval", type=float, default=5.0, help="Seconds between reports")
    p.add_argument("--start-temp", type=float, default=21.0)
    p.add_argument("--drift", type=float, default=0.5,
                   help="Maximum random change per report")
    p.add_argument("--spike-every", type=int, default=0,
                   help="Inject a fault spike every N reports. 0 disables. "
                        "Useful for testing anomaly detection in Phase 3.")
    p.add_argument("--spike", type=float, default=25.0, help="Size of the spike")
    args = p.parse_args(argv)

    state = {"temperature": args.start_temp, "fan_on": False}

    from autobahn.asyncio.component import Component  # noqa: F401  (import check)
    comp = build_component(args, state)

    async def run():
        loop = asyncio.get_running_loop()
        await comp.start(loop)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n[board] stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
