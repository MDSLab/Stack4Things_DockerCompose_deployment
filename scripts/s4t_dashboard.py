#!/usr/bin/env python3
"""
s4t_dashboard.py

The read only aggregator behind the IoT Overview panel in Horizon.

    boards table  ---\\
    Ditto twins   ----+---> [ join on board UUID ] ---> GET /data ---> Horizon panel
    audit store   ---/

WHY IT EXISTS
-------------
Three systems each hold one third of the answer to "what is this machine doing
and who touched it". The registry knows the board exists and whether an agent is
connected. The twin knows what the device last reported. The audit log knows who
acted on it. Nobody has ever seen all three at once.

WHY IT IS A SERVER AND NOT JAVASCRIPT
-------------------------------------
A browser could call all three directly, but only by carrying the Ditto password
and the audit token in the page source. This process holds those credentials and
hands out one joined document, so nothing secret reaches a browser and there is
no cross origin problem to solve.

It listens on its own port with NO published host mapping. The only client is the
Horizon panel, which runs in a container on the same Docker network. A port that
exists only inside the network is a much smaller claim than one bound to the host.

WHY IT IS A SEPARATE MODULE
---------------------------
So it can fail without consequence. `s4t_audit_logger.py` imports it inside a
conditional, and the audit service that the ledger depends on keeps running
whether or not this module loads at all. The dashboard is convenience; the log
is not.

READ ONLY
---------
Every route is a GET, nothing here writes to the audit store, and the database
user is the same non root `iotronic` account the provisioner already uses.

USAGE
-----
    Set DASHBOARD_PORT in the environment of s4t-audit-logger. Unset means off.
"""

import base64
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

LOG = logging.getLogger("audit.dashboard")

BOARD_SQL = (
    "SELECT uuid, name, type, status, fleet, owner, project, lr_version "
    "FROM boards WHERE uuid IS NOT NULL ORDER BY name"
)


# =========================================================================
# Configuration, read from the environment so the logger's argparse is
# untouched. One less reason for a dashboard change to alter the audit path.
# =========================================================================
class Config:
    def __init__(self, env=None):
        e = env if env is not None else os.environ
        self.port = int(e.get("DASHBOARD_PORT", 0))
        self.ditto_url = e.get("DITTO_URL", "http://ditto-nginx:80").rstrip("/")
        self.ditto_user = e.get("DITTO_USER", "s4t")
        self.ditto_pass = e.get("DITTO_PASS", "")
        self.namespace = e.get("DITTO_NAMESPACE", "s4t")
        self.db_host = e.get("S4T_DB_HOST", "iotronic-db")
        self.db_port = int(e.get("S4T_DB_PORT", 3306))
        self.db_user = e.get("S4T_DB_USER", "iotronic")
        self.db_pass = e.get("S4T_DB_PASS", "")
        self.db_name = e.get("S4T_DB_NAME", "iotronic")
        self.timeout = float(e.get("DASHBOARD_TIMEOUT", 6))
        self.stale_after = float(e.get("DASHBOARD_STALE_SECONDS", 120))


# =========================================================================
# Pure functions. No database, no network, so the logic that decides what the
# screen says is testable on its own.
# =========================================================================
def _parse_iso(ts):
    """Tolerant ISO 8601 parse. Returns None rather than raising, because one
    malformed timestamp must not blank the whole dashboard."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def age_seconds(ts, now=None):
    """Seconds since `ts`, or None when it cannot be read."""
    dt = _parse_iso(ts)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - dt).total_seconds())


def human_age(seconds):
    """Seconds as something a person can read at a glance.

    Computed here rather than in the page so that the server rendered table and
    the refreshed one cannot disagree. A raw '2081406s' is technically correct
    and tells a reader nothing.
    """
    if seconds is None:
        return None
    s = int(seconds)
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm" % (s // 60)
    if s < 86400:
        return "%dh" % (s // 3600)
    d = s // 86400
    return "%dd" % d if d < 365 else "%dy" % (d // 365)


def twin_index(things, namespace):
    """Ditto search results keyed by board UUID.

    A thingId is `<namespace>:<uuid>` and the uuid half is the board uuid, the
    same string the registry and the audit store use. That shared key is why
    this join needs no mapping table.
    """
    out = {}
    for t in things or []:
        tid = t.get("thingId") or ""
        if ":" not in tid:
            continue
        ns, _, name = tid.partition(":")
        if namespace and ns != namespace:
            continue
        out[name] = t
    return out


def twin_view(thing, now=None, stale_after=120.0):
    """The part of a twin the screen actually shows."""
    if not thing:
        return {"present": False, "revision": None, "telemetry": None,
                "desired": None, "last_change": None, "age_seconds": None,
                "stale": None}
    feats = thing.get("features") or {}
    tele = (feats.get("telemetry") or {})
    meta = thing.get("_metadata") or {}
    last = (thing.get("_modified")
            or ((meta.get("features") or {}).get("telemetry") or {}).get("modified"))
    age = age_seconds(last, now)
    return {
        "present": True,
        "revision": thing.get("_revision"),
        "telemetry": tele.get("properties"),
        "desired": tele.get("desiredProperties"),
        "attributes": thing.get("attributes"),
        "features": sorted(feats.keys()),
        "last_change": last,
        "age_seconds": age,
        "age_human": human_age(age),
        # `stale` is None, not False, when there is no timestamp to judge by.
        # Reporting "fresh" on the strength of no evidence is how a dashboard
        # ends up lying quietly.
        "stale": None if age is None else age > stale_after,
    }


def join(boards, things, audit_by_board, cfg, now=None):
    """boards + twins + audit summaries -> what the panel renders.

    Every board appears, including ones with no twin and no audit history,
    because a board that is missing its twin is exactly what an operator needs
    to see. Dropping it would make the failure invisible.
    """
    idx = twin_index(things, cfg.namespace)
    rows = []
    for b in boards or []:
        uuid = b.get("uuid")
        rows.append({
            "uuid": uuid,
            "name": b.get("name"),
            "type": b.get("type"),
            "status": b.get("status"),
            "fleet": b.get("fleet"),
            "lr_version": b.get("lr_version"),
            # TWO different claims, kept apart on purpose.
            #
            # `agent_seen` means an agent has EVER connected: lr_version can
            # only be written by software running on the device, so it is
            # strong evidence, but it is evidence about the past.
            #
            # `online` is the current state. Camera_03 has a version and is
            # offline, so counting versions as live agents overstated things
            # by one on the first version of this screen.
            "agent_seen": bool(b.get("lr_version")),
            "online": (b.get("status") or "").lower() == "online",
            "twin": twin_view(idx.get(uuid), now, cfg.stale_after),
            "audit": audit_by_board.get(uuid) or {
                "count": 0, "last_event": None, "last_action": None,
                "last_operator": None, "operator_count": 0},
        })
    return rows


def summarise_health(rows, store_stats, source_ages):
    """Health as 'when did this path last deliver', not 'is the process up'.

    A container cannot see its siblings' health status without the Docker
    socket, and mounting that would be a far larger privilege than anything
    else in this deployment. Delivery is observable from data we already hold,
    and it answers the more useful question anyway.
    """
    twinned = sum(1 for r in rows if r["twin"]["present"])
    return [
        {"name": "Twin layer and bridge",
         "detail": "last twin event",
         "age_seconds": source_ages.get("ditto")},
        {"name": "Audit proxy",
         "detail": "last operator action",
         "age_seconds": source_ages.get("horizon")},
        {"name": "Provisioner",
         "detail": "%d of %d boards have a twin" % (twinned, len(rows)),
         "age_seconds": None},
        {"name": "Audit store",
         "detail": "%s records, last sequence %s" % (
             store_stats.get("count"), store_stats.get("max_sequence")),
         "age_seconds": age_seconds(store_stats.get("last_timestamp"))},
    ]


# =========================================================================
# The three reads
# =========================================================================
def read_boards(cfg):
    """The registry. Same connection the provisioner already makes, read only."""
    import pymysql
    con = pymysql.connect(host=cfg.db_host, port=cfg.db_port, user=cfg.db_user,
                          password=cfg.db_pass, database=cfg.db_name,
                          connect_timeout=int(cfg.timeout),
                          cursorclass=pymysql.cursors.DictCursor)
    try:
        with con.cursor() as cur:
            cur.execute(BOARD_SQL)
            return list(cur.fetchall())
    finally:
        con.close()


def read_twins(cfg):
    """Every twin in the namespace, in one search call."""
    auth = base64.b64encode(
        ("%s:%s" % (cfg.ditto_user, cfg.ditto_pass)).encode()).decode()
    q = urllib.parse.urlencode({
        "namespaces": cfg.namespace,
        "option": "size(200)",
        "fields": "thingId,attributes,features,_revision,_modified",
    })
    req = urllib.request.Request(
        "%s/api/2/search/things?%s" % (cfg.ditto_url, q),
        headers={"Authorization": "Basic " + auth})
    with urllib.request.urlopen(req, timeout=cfg.timeout) as r:
        return json.loads(r.read().decode()).get("items", [])


def read_twin(cfg, uuid):
    """One twin, in full, for the detail view."""
    auth = base64.b64encode(
        ("%s:%s" % (cfg.ditto_user, cfg.ditto_pass)).encode()).decode()
    url = "%s/api/2/things/%s:%s" % (cfg.ditto_url, cfg.namespace, uuid)
    req = urllib.request.Request(url, headers={"Authorization": "Basic " + auth})
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def read_board_events(store, uuid, limit=50):
    """The most recent audit records for one board.

    Newest first, which is the opposite of the public API's fixed ascending
    order. That order exists so a ledger consumer can compare two calls; a
    person looking at a screen wants the last thing that happened at the top.
    The stored bytes are untouched either way.
    """
    rows = store.db.execute(
        "SELECT record FROM audit_events WHERE machine_id = ? "
        "ORDER BY sequence DESC LIMIT ?", (uuid, int(limit))).fetchall()
    out = []
    for (blob,) in rows:
        try:
            out.append(json.loads(blob))
        except (ValueError, TypeError):
            continue          # one unreadable row must not empty the panel
    return out


def read_audit(store):
    """Per board summary and per source freshness, straight from SQLite."""
    rows = store.db.execute(
        "SELECT machine_id, COUNT(*), MAX(timestamp),"
        "       COUNT(DISTINCT operator_name) "
        "FROM audit_events WHERE machine_id IS NOT NULL GROUP BY machine_id"
    ).fetchall()
    by_board = {}
    for mid, count, last_ts, operators in rows:
        last = store.db.execute(
            "SELECT machine_action, operator_name FROM audit_events "
            "WHERE machine_id = ? ORDER BY sequence DESC LIMIT 1", (mid,)
        ).fetchone()
        by_board[mid] = {
            "count": count,
            "last_event": last_ts,
            "last_action": last[0] if last else None,
            "last_operator": last[1] if last else None,
            "operator_count": operators,
        }
    ages = {}
    for source in ("ditto", "horizon"):
        r = store.db.execute(
            "SELECT MAX(timestamp) FROM audit_events WHERE source = ?",
            (source,)).fetchone()
        ages[source] = age_seconds(r[0] if r else None)
    return by_board, ages


# =========================================================================
# The application
# =========================================================================
def make_dashboard_app(store, cfg=None):
    from aiohttp import web
    import asyncio

    cfg = cfg or Config()

    async def data(request):
        loop = asyncio.get_running_loop()
        errors = {}

        # Each read is allowed to fail on its own. A dashboard that returns
        # nothing because one of three sources is slow is worse than one that
        # shows the boards and says the twin layer is unreachable.
        try:
            boards = await loop.run_in_executor(None, read_boards, cfg)
        except Exception as exc:
            boards, errors["boards"] = [], str(exc)
        try:
            things = await loop.run_in_executor(None, read_twins, cfg)
        except Exception as exc:
            things, errors["twins"] = [], str(exc)
        try:
            audit_by_board, source_ages = await loop.run_in_executor(
                None, read_audit, store)
        except Exception as exc:
            audit_by_board, source_ages = {}, {}
            errors["audit"] = str(exc)

        rows = join(boards, things, audit_by_board, cfg)
        stats = store.stats()
        return web.json_response({
            "generated_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "boards": rows,
            "health": summarise_health(rows, stats, source_ages),
            "totals": {
                "boards": len(rows),
                "with_twin": sum(1 for r in rows if r["twin"]["present"]),
                "online": sum(1 for r in rows if r["online"]),
                "agents_seen": sum(1 for r in rows if r["agent_seen"]),
                "audit_records": stats.get("count"),
            },
            "errors": errors,
        })

    async def board(request):
        """One board in full: the whole twin, and its audit history."""
        uuid = request.match_info["uuid"]
        limit = min(int(request.query.get("limit", 50)), 500)
        loop = asyncio.get_running_loop()
        out = {"uuid": uuid, "twin": None, "events": [], "errors": {}}
        try:
            out["twin"] = await loop.run_in_executor(None, read_twin, cfg, uuid)
        except Exception as exc:
            out["errors"]["twin"] = str(exc)
        try:
            out["events"] = await loop.run_in_executor(
                None, read_board_events, store, uuid, limit)
        except Exception as exc:
            out["errors"]["events"] = str(exc)
        return web.json_response(out)

    async def health(request):
        return web.json_response({"status": "ok"})

    app = web.Application()
    app.add_routes([
        web.get("/data", data),
        web.get("/board/{uuid}", board),
        web.get("/health", health),
    ])
    return app
