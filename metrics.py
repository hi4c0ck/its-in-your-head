"""SQLite metrics store + background worker (off the LLM hot path).

Hooks enqueue events; a daemon thread drains them, sketches prompts,
writes WAL SQLite under the plugin data root, and pushes a live tick to
the desktop half (no-op when no desktop client is attached).
"""
import logging
import queue
import threading
import time

try:  # hermes namespaced plugin import (known parent package)
    from . import embedder
except ImportError:  # top-level import (tests / standalone dev)
    import embedder

logger = logging.getLogger(__name__)

PLUGIN_ID = "its-in-your-head"

_queue: "queue.Queue" = queue.Queue()
_worker = None


def _db():
    from plugins.plugin_storage import plugin_db
    return plugin_db(PLUGIN_ID)


def ensure_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY,
          ts REAL NOT NULL,
          type TEXT NOT NULL,
          session_id TEXT,
          model TEXT,
          user_message TEXT,
          embedding BLOB,
          duration_s REAL,
          finish_reason TEXT,
          prompt_tokens INTEGER,
          completion_tokens INTEGER,
          total_tokens INTEGER,
          status_code INTEGER,
          reason TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
        CREATE INDEX IF NOT EXISTS idx_events_type ON events (type);
    """)


def enqueue(event: dict):
    _queue.put(event)


def start_worker():
    global _worker
    if _worker is not None:
        return
    _worker = threading.Thread(target=_drain, name="iitoh-metrics", daemon=True)
    _worker.start()


_conn = None


def _get_conn():
    """Lazily open the WAL DB on first event (never at register time —
    doctor's temp HERMES_HOME must stay clean for its cleanup step)."""
    global _conn
    if _conn is None:
        conn = _db()
        ensure_schema(conn)
        _conn = conn
    return _conn


def _drain():
    while True:
        ev = _queue.get()
        if ev is None:
            break
        try:
            if ev.get("type") == "turn.start":
                ev["embedding"] = embedder.sketch(ev.get("user_message") or "")
            conn = _get_conn()
            conn.execute(
                """INSERT INTO events (ts, type, session_id, model, user_message,
                                       embedding, duration_s, finish_reason,
                                       prompt_tokens, completion_tokens, total_tokens,
                                       status_code, reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ev.get("ts") or time.time(),
                    ev.get("type"),
                    ev.get("session_id"),
                    ev.get("model"),
                    ev.get("user_message"),
                    ev.get("embedding"),
                    ev.get("duration_s"),
                    ev.get("finish_reason"),
                    ev.get("prompt_tokens"),
                    ev.get("completion_tokens"),
                    ev.get("total_tokens"),
                    ev.get("status_code"),
                    ev.get("reason"),
                ),
            )
            conn.commit()
            _broadcast(ev)
        except Exception:
            logger.exception("iitoh: failed to store event %s", ev.get("type"))


def _broadcast(ev):
    """Fire-and-forget push to the desktop half; safe no-op when absent."""
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event(
            PLUGIN_ID, "tick",
            {"type": ev.get("type"), "total_tokens": ev.get("total_tokens")},
        )
    except Exception:
        pass  # no desktop client / module unavailable — polling fallback covers it
