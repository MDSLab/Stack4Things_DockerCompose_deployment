# conf_wagent/wagent_outbox.py
# Store-and-forward outbox for iotronic-wagent.
# Placed in conf_wagent/ so it is available inside the container
# at /etc/iotronic/wagent_outbox.py without rebuilding the image.

import sqlite3
import asyncio
import json
import time
import logging

log = logging.getLogger(__name__)
DB_PATH = "/var/lib/wagent/outbox.db"


def init_db():
    """Create the outbox table if it does not exist. Called once at startup."""
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS outbox (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            uri        TEXT    NOT NULL,
            args       TEXT    NOT NULL DEFAULT '[]',
            status     TEXT    NOT NULL DEFAULT 'pending',
            created_at REAL    NOT NULL,
            sent_at    REAL,
            attempts   INTEGER NOT NULL DEFAULT 0
        )
    """)
    con.commit()
    con.close()
    log.info("[outbox] Database initialised at %s", DB_PATH)


def enqueue(uri, args):
    """Persist a WAMP call so it survives a wagent restart."""
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO outbox (uri, args, created_at) VALUES (?, ?, ?)",
        (uri, json.dumps(args), time.time())
    )
    con.commit()
    con.close()
    log.info("[outbox] Enqueued uri=%s", uri)


async def drain_outbox(wamp_session, poll_interval=2.0):
    """
    Background coroutine: retry all 'pending' rows using the live WAMP session.
    Runs forever — start it with asyncio.ensure_future() after onJoin.
    """
    log.info("[outbox] Drain loop started")
    while True:
        try:
            con = sqlite3.connect(DB_PATH)
            rows = con.execute(
                "SELECT id, uri, args FROM outbox WHERE status = 'pending' LIMIT 10"
            ).fetchall()
            for row_id, uri, args_json in rows:
                try:
                    await wamp_session.call(uri, *json.loads(args_json), timeout=5)
                    con.execute(
                        "UPDATE outbox SET status='sent', sent_at=? WHERE id=?",
                        (time.time(), row_id)
                    )
                    log.info("[outbox] Delivered id=%d uri=%s", row_id, uri)
                except Exception as exc:
                    con.execute(
                        "UPDATE outbox SET attempts=attempts+1 WHERE id=?",
                        (row_id,)
                    )
                    log.warning("[outbox] Failed id=%d: %s", row_id, exc)
            con.commit()
            con.close()
        except Exception as exc:
            log.error("[outbox] Drain cycle error: %s", exc)
        await asyncio.sleep(poll_interval)
