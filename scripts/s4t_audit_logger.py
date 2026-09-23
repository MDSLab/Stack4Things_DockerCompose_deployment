#!/usr/bin/env python3
"""
s4t_audit_logger.py

The audit logger for Stack4Things. Captures everything that happens to a
machine, stores it durably, and serves it to the ledger developer four ways.

    ditto.twin.events   ---\\                      /--- GET  /audit/events
                            >--- [ this ] ---------+--- GET  /audit/events/{id}
    s4t.audit.ingest    ---/       |               +--- GET  /audit/stream  (SSE)
    (operator actions            SQLite            +--- WS   /audit/ws
     from the proxy)               |               \\--- queue s4t.audit
                                   |
                              one writer, so
                              the sequence is
                              authoritative

STAGES
------
L1  consume twin events, publish audit records            DONE
L2  durable store and a gapless sequence                  this file
L3  query API, plus SSE and WebSocket for realtime        this file
L4  operator identity                                     s4t_audit_proxy.py
L5  retention and failure behaviour                       this file

WHY ONE WRITER
--------------
Operator actions are captured by a separate proxy process, but that process
does NOT write to the database. It publishes to `s4t.audit.ingest` and this
service consumes it. Two writers would mean two sequence generators, and the
sequence is the ledger developer's cursor: it has to come from one place or it
is not a cursor at all.

WHY THE RAW BYTES ARE STORED
----------------------------
The ledger developer hashes these records himself. If a record serialises one
way on the queue and another way through the API, every hash he anchors from
one path fails against the other, and the symptom looks like a ledger fault.
So the exact bytes are stored in the `record` column and served verbatim,
rather than rebuilt from the columns and hoped to match.

DEPENDENCIES
------------
    pip install aio-pika aiohttp

USAGE
-----
    ./s4t_audit_logger.py
    ./s4t_audit_logger.py --db /data/audit.db --api-port 8890 -v
"""

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone

LOG = logging.getLogger("audit")

SCHEMA_VERSION = "1.0"

TELEMETRY_PATH = "/features/telemetry/properties"
DESIRED_MARKER = "/desiredProperties"
PREDICTION_PATH = "/features/prediction"

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
  sequence       INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id       TEXT    NOT NULL UNIQUE,
  timestamp      TEXT    NOT NULL,
  operator_name  TEXT,
  machine_name   TEXT    NOT NULL,
  machine_action TEXT    NOT NULL,
  actor_type     TEXT    NOT NULL,
  machine_id     TEXT,                     -- null for board.create: the board
                                           -- has no identifier until it exists
  source         TEXT    NOT NULL,
  twin_revision  INTEGER,
  record         TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ts      ON audit_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_machine ON audit_events(machine_id, sequence);
CREATE INDEX IF NOT EXISTS idx_action  ON audit_events(machine_action, sequence);
CREATE INDEX IF NOT EXISTS idx_actor   ON audit_events(actor_type, sequence);
CREATE INDEX IF NOT EXISTS idx_op      ON audit_events(operator_name, sequence);
"""


# =========================================================================
# Pure functions. Unit testable with no broker, no database and no network.
# =========================================================================
def canonical(record):
    """One serialisation, used for storage, the queue and both realtime
    transports. Sorted keys and no whitespace, so the bytes are reproducible."""
    return json.dumps(record, separators=(",", ":"), sort_keys=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_topic(topic):
    """'s4t/<name>/things/twin/events/merged' -> (namespace, name, action)."""
    parts = (topic or "").split("/")
    if len(parts) != 6:
        return None, None, None
    ns, name, group, channel, criterion, action = parts
    if group != "things" or channel != "twin" or criterion != "events":
        return None, None, None
    if not ns or not name:
        return None, None, None
    return ns, name, action


def classify(path, topic_action):
    """(machine_action, actor_type).

    `unknown` is deliberate for desired property changes. Somebody set that
    value, but a twin event does not say who. Calling it human would assert
    something unsupported and calling it system would be false.
    """
    path = path or "/"
    if topic_action == "deleted" and path == "/":
        return "twin.delete", "system"
    if topic_action == "created" and path == "/":
        return "twin.create", "system"
    if DESIRED_MARKER in path:
        return "desired.set", "unknown"
    if path.startswith(TELEMETRY_PATH):
        return "telemetry.report", "device"
    if path.startswith(PREDICTION_PATH):
        return "prediction.write", "system"
    if path.startswith("/attributes"):
        return "twin.update", "system"
    return "twin.modify", "system"


def machine_name_from(extra, fallback):
    name = (((extra or {}).get("attributes") or {}).get("boardName"))
    return name if isinstance(name, str) and name else fallback


def build_record(topic, path, value, extra, revision, occurred_at):
    """One Ditto twin event -> one audit record without its sequence."""
    ns, name, topic_action = parse_topic(topic)
    if ns is None:
        return None
    action_verb, actor_type = classify(path, topic_action)
    machine_name = machine_name_from(extra, name)
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(uuid.uuid4()),
        "timestamp": occurred_at or now_iso(),
        "operator_name": None,
        "machine_name": machine_name,
        "machine_action": action_verb,
        "actor": {"type": actor_type,
                  "id": name if actor_type == "device" else None,
                  "name": machine_name if actor_type == "device" else None},
        "machine": {"id": name, "name": machine_name, "namespace": ns},
        "action": {"verb": action_verb.split(".", 1)[-1],
                   "target": path or "/",
                   "detail": value if isinstance(value, (dict, list)) else {"value": value},
                   "outcome": "success"},
        "source": "ditto",
        "twin_revision": revision,
    }


def record_from_twin_event(body):
    """Decode one Ditto twin event, or None if unusable."""
    try:
        evt = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(evt, dict):
        return None
    headers = evt.get("headers") or {}
    revision = evt.get("revision")
    if revision is None:
        revision = headers.get("ditto-revision") or headers.get("revision")
    return build_record(
        topic=evt.get("topic"), path=evt.get("path"), value=evt.get("value"),
        extra=evt.get("extra"), revision=revision,
        occurred_at=evt.get("timestamp") or headers.get("ditto-timestamp"))


def record_from_operator_event(body):
    """Decode a record produced by the conductor proxy.

    The proxy has already resolved the Keystone username, so it sends a nearly
    complete record. This validates it rather than trusting it: a malformed
    record from our own component is still a malformed record.
    """
    try:
        rec = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(rec, dict):
        return None
    required = ("timestamp", "machine_name", "machine_action", "actor",
                "machine", "action", "source")
    if any(k not in rec for k in required):
        return None
    rec.setdefault("schema_version", SCHEMA_VERSION)
    rec.setdefault("event_id", str(uuid.uuid4()))
    rec.setdefault("operator_name", None)
    rec.setdefault("twin_revision", None)
    return rec


def build_query(params):
    """Translate query parameters into SQL. Pure, so the filter logic is
    testable without a database, and parameterised, so it cannot be injected."""
    where, args = [], []
    def eq(col, key):
        v = params.get(key)
        if v:
            where.append(f"{col} = ?"); args.append(v)

    if params.get("from_seq"):
        where.append("sequence > ?"); args.append(int(params["from_seq"]))
    if params.get("to_seq"):
        where.append("sequence <= ?"); args.append(int(params["to_seq"]))
    if params.get("from"):
        where.append("timestamp >= ?"); args.append(params["from"])
    if params.get("to"):
        where.append("timestamp <= ?"); args.append(params["to"])
    eq("machine_id", "machine_id")
    eq("machine_name", "machine_name")
    eq("machine_action", "machine_action")
    eq("actor_type", "actor_type")
    eq("operator_name", "operator_name")

    limit = int(params.get("limit") or 1000)
    limit = max(1, min(limit, 10000))          # T11: a client cannot ask for everything
    sql = "SELECT sequence, record FROM audit_events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    # ordering is fixed. A query API that can reorder is one whose results
    # cannot be compared between two calls.
    sql += " ORDER BY sequence ASC LIMIT ?"
    args.append(limit)
    return sql, args, limit


# =========================================================================
# Storage
# =========================================================================
class Store:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")       # survives an unclean stop
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._migrate_machine_id_nullable()

    def _migrate_machine_id_nullable(self):
        """The first version of this schema had machine_id NOT NULL.

        That made a `board.create` record impossible to insert, because a board
        has no identifier until it exists. Worse, the message was requeued on
        failure, so one board creation would wedge the consumer in a loop and
        stop every other record with it. SQLite cannot drop a constraint, so
        the table is rebuilt when the old one is found. Sequence values are
        preserved, which matters because they are the consumer's cursor.
        """
        cols = self.db.execute("PRAGMA table_info(audit_events)").fetchall()
        notnull = {c[1]: c[3] for c in cols}
        if notnull.get("machine_id") != 1:
            return
        LOG.warning("migrating audit_events so machine_id may be null")
        self.db.executescript("""
            PRAGMA foreign_keys=off;
            BEGIN;
            CREATE TABLE audit_events_new (
              sequence       INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id       TEXT    NOT NULL UNIQUE,
              timestamp      TEXT    NOT NULL,
              operator_name  TEXT,
              machine_name   TEXT    NOT NULL,
              machine_action TEXT    NOT NULL,
              actor_type     TEXT    NOT NULL,
              machine_id     TEXT,
              source         TEXT    NOT NULL,
              twin_revision  INTEGER,
              record         TEXT    NOT NULL
            );
            INSERT INTO audit_events_new SELECT * FROM audit_events;
            DROP TABLE audit_events;
            ALTER TABLE audit_events_new RENAME TO audit_events;
            COMMIT;
            PRAGMA foreign_keys=on;
        """)
        self.db.executescript(SCHEMA)          # recreate the indexes
        self.db.commit()
        LOG.warning("migration complete, sequence values preserved")

    def append(self, record):
        """Insert, take the sequence SQLite assigned, put it in the record, and
        store the exact bytes that will also go on the queue and the streams."""
        cur = self.db.execute(
            "INSERT INTO audit_events (event_id, timestamp, operator_name,"
            " machine_name, machine_action, actor_type, machine_id, source,"
            " twin_revision, record) VALUES (?,?,?,?,?,?,?,?,?,'')",
            (record["event_id"], record["timestamp"], record.get("operator_name"),
             record["machine_name"], record["machine_action"],
             record["actor"]["type"], record["machine"]["id"], record["source"],
             record.get("twin_revision")))
        seq = cur.lastrowid
        record["sequence"] = seq
        blob = canonical(record)
        self.db.execute("UPDATE audit_events SET record = ? WHERE sequence = ?",
                        (blob, seq))
        self.db.commit()
        return seq, blob

    def query(self, params):
        sql, args, limit = build_query(params)
        rows = self.db.execute(sql, args).fetchall()
        return rows, limit

    def by_id(self, event_id):
        r = self.db.execute("SELECT record FROM audit_events WHERE event_id = ?",
                            (event_id,)).fetchone()
        return r[0] if r else None

    def stats(self):
        r = self.db.execute(
            "SELECT COUNT(*), MIN(sequence), MAX(sequence), MIN(timestamp),"
            " MAX(timestamp) FROM audit_events").fetchone()
        by_actor = dict(self.db.execute(
            "SELECT actor_type, COUNT(*) FROM audit_events GROUP BY actor_type"
        ).fetchall())
        return {"count": r[0], "min_sequence": r[1], "max_sequence": r[2],
                "first_timestamp": r[3], "last_timestamp": r[4],
                "by_actor_type": by_actor, "schema_version": SCHEMA_VERSION}

    def prune(self, keep_days):
        """L5 retention. Deleting old rows never renumbers the sequence, so a
        consumer's cursor stays meaningful across a prune."""
        if not keep_days:
            return 0
        cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
        cut_iso = datetime.fromtimestamp(cutoff, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")
        cur = self.db.execute("DELETE FROM audit_events WHERE timestamp < ?", (cut_iso,))
        self.db.commit()
        return cur.rowcount


# =========================================================================
# Realtime fan out
# =========================================================================
class Subscribers:
    """SSE and WebSocket clients. Both receive the identical canonical bytes
    the queue and the API serve, so a consumer can switch transport without
    changing anything about how it parses or hashes."""

    def __init__(self):
        self.sse = set()
        self.ws = set()

    async def broadcast(self, blob):
        for q in list(self.sse):
            try:
                q.put_nowait(blob)
            except asyncio.QueueFull:
                # a slow client must not stall the writer
                self.sse.discard(q)
                LOG.warning("dropped a slow SSE subscriber")
        for ws in list(self.ws):
            try:
                await ws.send_str(blob)
            except Exception:
                self.ws.discard(ws)

    def counts(self):
        return {"sse": len(self.sse), "ws": len(self.ws)}


# =========================================================================
# HTTP API
# =========================================================================
def make_app(store, subs, opts):
    from aiohttp import web, WSMsgType

    def authorised(request):
        if not opts.api_token:
            return True                       # no token configured, open reads
        header = request.headers.get("Authorization", "")
        return header == f"Bearer {opts.api_token}"

    @web.middleware
    async def auth_mw(request, handler):
        if request.path == "/health":
            return await handler(request)
        if not authorised(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def health(request):
        return web.json_response({"ok": True, "schema_version": SCHEMA_VERSION,
                                  "subscribers": subs.counts()})

    async def events(request):
        rows, limit = store.query(dict(request.query))
        items = [json.loads(r[1]) for r in rows]
        last = rows[-1][0] if rows else request.query.get("from_seq", 0)
        return web.json_response({
            "count": len(items),
            "next_seq": int(last or 0),
            "has_more": len(rows) == limit,
            "items": items,
        }, dumps=lambda o: json.dumps(o, separators=(",", ":"), sort_keys=True))

    async def event_by_id(request):
        blob = store.by_id(request.match_info["event_id"])
        if blob is None:
            return web.json_response({"error": "not found"}, status=404)
        # served verbatim, so it is byte identical to the queue message
        return web.Response(body=blob.encode(), content_type="application/json")

    async def stats(request):
        return web.json_response(store.stats())

    async def stream(request):
        """Server Sent Events. Optionally replays from a sequence first, so a
        consumer can catch up and then stay live without a gap between the two.
        """
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        })
        await resp.prepare(request)
        q = asyncio.Queue(maxsize=opts.subscriber_backlog)
        subs.sse.add(q)
        try:
            if request.query.get("from_seq"):
                rows, _ = store.query({"from_seq": request.query["from_seq"],
                                       "limit": 10000})
                for seq, blob in rows:
                    await resp.write(f"id: {seq}\ndata: {blob}\n\n".encode())
            while True:
                try:
                    blob = await asyncio.wait_for(q.get(), timeout=20)
                    seq = json.loads(blob).get("sequence")
                    await resp.write(f"id: {seq}\ndata: {blob}\n\n".encode())
                except asyncio.TimeoutError:
                    await resp.write(b": keep-alive\n\n")   # keeps proxies open
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            subs.sse.discard(q)
        return resp

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        subs.ws.add(ws)
        try:
            if request.query.get("from_seq"):
                rows, _ = store.query({"from_seq": request.query["from_seq"],
                                       "limit": 10000})
                for _seq, blob in rows:
                    await ws.send_str(blob)
            async for msg in ws:
                if msg.type == WSMsgType.TEXT and msg.data == "ping":
                    await ws.send_str('{"pong":true}')
        except Exception:
            pass
        finally:
            subs.ws.discard(ws)
        return ws

    app = web.Application(middlewares=[auth_mw])
    app.add_routes([
        web.get("/health", health),
        web.get("/audit/events", events),
        web.get("/audit/events/{event_id}", event_by_id),
        web.get("/audit/stats", stats),
        web.get("/audit/stream", stream),
        web.get("/audit/ws", ws_handler),
    ])
    return app


# =========================================================================
# The service
# =========================================================================
class AuditLogger:
    def __init__(self, opts):
        self.o = opts
        self.store = Store(opts.db)
        self.subs = Subscribers()
        self.channel = None
        self.consuming = 0
        self.written = self.dropped = self.failed = 0

    async def run(self):
        import aio_pika
        from aiohttp import web
        import urllib.parse as up

        url = (f"amqp://{up.quote(self.o.amqp_user, safe='')}"
               f":{up.quote(self.o.amqp_pass, safe='')}"
               f"@{self.o.amqp_host}:{self.o.amqp_port}"
               f"/{up.quote(self.o.amqp_vhost, safe='')}")

        st = self.store.stats()
        LOG.info("audit logger starting: db=%s records=%d last_sequence=%s",
                 self.o.db, st["count"], st["max_sequence"])

        runner = web.AppRunner(make_app(self.store, self.subs, self.o))
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", self.o.api_port).start()
        LOG.info("API listening on :%d  (REST, SSE at /audit/stream, "
                 "WebSocket at /audit/ws)", self.o.api_port)

        # The dashboard aggregator, on its own port and deliberately optional.
        #
        # It is imported HERE rather than at module scope so that a missing or
        # broken dashboard module cannot stop the audit service from starting.
        # The ledger depends on the log; nothing depends on the dashboard.
        #
        # The port is never published to the host. Its only client is the
        # Horizon panel, which runs on this Docker network.
        dash_port = int(os.environ.get("DASHBOARD_PORT", 0))
        if dash_port:
            try:
                from s4t_dashboard import make_dashboard_app
                dash = web.AppRunner(make_dashboard_app(self.store))
                await dash.setup()
                await web.TCPSite(dash, "0.0.0.0", dash_port).start()
                LOG.info("dashboard aggregator listening on :%d (internal "
                         "network only, GET /data)", dash_port)
            except Exception as exc:
                LOG.error("dashboard not started, the audit service continues "
                          "normally: %s", exc)

        conn = await aio_pika.connect_robust(url)
        async with conn:
            self.channel = await conn.channel()
            await self.channel.set_qos(prefetch_count=self.o.prefetch)
            await self.channel.declare_queue(self.o.audit_queue, durable=True)

            twin = await self.channel.declare_queue(self.o.source_queue, durable=True)
            oper = await self.channel.declare_queue(self.o.operator_queue, durable=True)
            await twin.consume(self.on_twin_event)
            await oper.consume(self.on_operator_event)
            self.consuming = 2
            LOG.info("consuming '%s' and '%s', publishing '%s'",
                     self.o.source_queue, self.o.operator_queue, self.o.audit_queue)

            tasks = [asyncio.ensure_future(t) for t in
                     (self.heartbeat_loop(), self.stats_loop(), self.retention_loop())]
            try:
                await asyncio.Future()
            finally:
                for t in tasks:
                    t.cancel()

    async def on_twin_event(self, message):
        await self._handle(message, record_from_twin_event)

    async def on_operator_event(self, message):
        await self._handle(message, record_from_operator_event)

    async def _handle(self, message, decode):
        import aio_pika
        # Do not acknowledge before the record is stored AND published. Ditto
        # acknowledges messages whose conversion failed and discards them, which
        # is a defect this project already found once. An audit logger that
        # repeated it would drop the evidence it exists to keep.
        async with message.process(requeue=True):
            record = decode(message.body)
            if record is None:
                self.dropped += 1
                LOG.warning("unusable message dropped: %s",
                            message.body[:160].decode(errors="replace"))
                return
            try:
                seq, blob = self.store.append(record)
            except Exception as exc:
                # Requeueing a record the store will never accept wedges this
                # consumer and takes every other record down with it. Ack it,
                # say so loudly, and let the gap in the sequence show the loss.
                # A dead letter queue is the proper home for these and is on
                # the stage E list.
                self.failed += 1
                LOG.error("POISON RECORD dropped after a storage failure: %s | %s",
                          exc, json.dumps(record)[:300])
                return
            await self.channel.default_exchange.publish(
                aio_pika.Message(body=blob.encode(),
                                 content_type="application/json",
                                 delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                routing_key=self.o.audit_queue)
            await self.subs.broadcast(blob)
            self.written += 1
            LOG.info("audit #%d %s %s operator=%s", seq, record["machine_action"],
                     record["machine_name"], record.get("operator_name"))

    async def heartbeat_loop(self):
        """Health means consuming both queues, able to publish, and able to
        write. A process that merely exists proves nothing."""
        from pathlib import Path
        hb = Path(self.o.heartbeat_file)
        while True:
            healthy = (self.consuming == 2
                       and self.channel is not None
                       and not self.channel.is_closed)
            if healthy:
                try:
                    self.store.db.execute("SELECT 1").fetchone()
                    hb.touch()
                except Exception as exc:
                    LOG.warning("store unhealthy, not touching heartbeat: %s", exc)
            await asyncio.sleep(self.o.heartbeat_interval)

    async def stats_loop(self):
        while True:
            await asyncio.sleep(self.o.stats_interval)
            LOG.info("stats: written=%d dropped=%d failed=%d subscribers=%s",
                     self.written, self.dropped, self.failed,
                     self.subs.counts())

    async def retention_loop(self):
        while True:
            await asyncio.sleep(self.o.retention_interval)
            n = self.store.prune(self.o.retention_days)
            if n:
                LOG.info("retention: pruned %d record(s) older than %d days",
                         n, self.o.retention_days)


def build_parser():
    p = argparse.ArgumentParser(description="Stack4Things audit logger.")
    g = p.add_argument_group("AMQP")
    g.add_argument("--amqp-host", default=os.environ.get("S4T_AMQP_HOST", "rabbitmq"))
    g.add_argument("--amqp-port", type=int, default=int(os.environ.get("S4T_AMQP_PORT", 5672)))
    g.add_argument("--amqp-user", default=os.environ.get("S4T_AMQP_USER", "openstack"))
    g.add_argument("--amqp-pass", default=os.environ.get("S4T_AMQP_PASS", "unime"))
    g.add_argument("--amqp-vhost", default=os.environ.get("S4T_AMQP_VHOST", "/"))
    g.add_argument("--source-queue", default=os.environ.get("AUDIT_SOURCE_QUEUE",
                                                            "ditto.twin.events"))
    g.add_argument("--operator-queue", default=os.environ.get("AUDIT_OPERATOR_QUEUE",
                                                              "s4t.audit.ingest"))
    g.add_argument("--audit-queue", default=os.environ.get("AUDIT_QUEUE", "s4t.audit"))
    g.add_argument("--prefetch", type=int, default=int(os.environ.get("AUDIT_PREFETCH", 50)))

    g = p.add_argument_group("store and API")
    g.add_argument("--db", default=os.environ.get("AUDIT_DB", "/data/audit.db"))
    g.add_argument("--api-port", type=int, default=int(os.environ.get("AUDIT_API_PORT", 8890)))
    g.add_argument("--api-token", default=os.environ.get("AUDIT_API_TOKEN", ""))
    g.add_argument("--subscriber-backlog", type=int,
                   default=int(os.environ.get("AUDIT_SUBSCRIBER_BACKLOG", 1000)))
    g.add_argument("--retention-days", type=int,
                   default=int(os.environ.get("AUDIT_RETENTION_DAYS", 0)),
                   help="0 disables pruning")
    g.add_argument("--retention-interval", type=float, default=3600.0)

    g = p.add_argument_group("operational")
    g.add_argument("--heartbeat-file", default=os.environ.get("HEARTBEAT_FILE",
                                                              "/tmp/audit-alive"))
    g.add_argument("--heartbeat-interval", type=float, default=5.0)
    g.add_argument("--stats-interval", type=float, default=60.0)
    g.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv=None):
    opts = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if opts.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(AuditLogger(opts).run())
    except KeyboardInterrupt:
        LOG.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
