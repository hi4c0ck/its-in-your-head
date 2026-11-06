"""Metrics pipeline: queue -> background worker -> WAL SQLite -> optional ticks.

Locked constraints (ARCHITECTURE.md #Metrics):
- Hot-path rule: hooks call enqueue() only — microseconds, no I/O, no
  embedding. ALL work happens in a daemon worker thread.
- Lazy DB: never open the plugin DB at import/register() time (doctor runs
  register() under a temp HERMES_HOME and deletes it — WinError 145 cleanup on
  Windows). The connection opens on the first event, inside the worker.
- Storage: plugins.plugin_storage.plugin_db("its-in-your-head") -> WAL SQLite
  under plugin-data/its-in-your-head/ — NEVER inside the plugin dir (install
  tree, wiped by update/remove).
- Push (optional live path):
      from hermes_cli.plugin_events import broadcast_plugin_event
      broadcast_plugin_event("its-in-your-head", "tick", {...})
  bare dotted event; fire-and-forget; no-op when no desktop client attached.
  The desktop polling fallback stays mandatory.

Event schema (dicts enqueued by hooks; the worker flattens them to rows):
  turn:  {type, ts, session_id, turn_id?, model, platform?, is_first_turn?,
          user_message, user_chars}
  api:   {type, ts, session_id, turn_id?, api_request_id?, api_call_count?,
          model, provider?, api_duration?, finish_reason?,
          usage: {prompt_tokens?, completion_tokens?, total_tokens?}}
  error: {type, ts, session_id, turn_id?, api_request_id?, status_code?,
          retry_count?, retryable?, reason?, error: {type?, message?}}
"""
import logging
import queue
import sqlite3
import threading
import time
from pathlib import Path

try:  # hermes namespaced import (gateway) vs top-level import (tests)
    from . import embedder
except ImportError:
    import embedder

try:
    from hermes_cli.plugin_events import broadcast_plugin_event
except Exception:  # plain test interpreters without the hermes CLI context
    broadcast_plugin_event = None

logger = logging.getLogger(__name__)

PLUGIN_ID = "its-in-your-head"

MAX_EVENTS = 50_000      # retention cap for the events table (owner default)
PRUNE_EVERY = 50         # prune check every N worker batches
BATCH_MAX = 64           # max events flushed per commit
QUEUE_GET_TIMEOUT = 0.5  # worker idle poll (s)

# SCHEMA — source of truth for the storage layout. Duplicated deliberately in
# dashboard/plugin_api.py: the dashboard mount must not import this worker
# module, and it can't rely on the worker's lazy connection having run.
# Keep the two copies in sync.
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id                INTEGER PRIMARY KEY,
    ts                REAL NOT NULL,
    type              TEXT NOT NULL,
    session_id        TEXT,
    turn_id           TEXT,
    api_request_id    TEXT,
    model             TEXT,
    provider          TEXT,
    api_duration      REAL,
    finish_reason     TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    status_code       INTEGER,
    retry_count       INTEGER,
    retryable         INTEGER,
    error_type        TEXT,
    error_message     TEXT,
    user_chars        INTEGER,
    user_message      TEXT,
    sketch            BLOB
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (type);
"""

_COLUMNS = [
    "ts", "type", "session_id", "turn_id", "api_request_id", "model",
    "provider", "api_duration", "finish_reason", "prompt_tokens",
    "completion_tokens", "total_tokens", "status_code", "retry_count",
    "retryable", "error_type", "error_message", "user_chars", "user_message",
    "sketch",
]
_INSERT = ("INSERT INTO events (" + ", ".join(_COLUMNS) + ") VALUES ("
           + ", ".join(":" + c for c in _COLUMNS) + ")")

_queue: "queue.Queue" = queue.Queue()
_db_path_override: "Path | str | None" = None  # test-only

_worker_lock = threading.Lock()
_worker_thread: "threading.Thread | None" = None
_shutdown = threading.Event()
_batches = 0


def enqueue(event: dict) -> None:
    """Called from hooks — microseconds only: build a dict, put it on the
    queue. No I/O, no embedding, no locking beyond the queue's own."""
    _queue.put(event)


def set_db_path(path: "Path | str") -> None:
    """TEST ONLY: point storage at a local file instead of plugin_db()."""
    global _db_path_override
    _db_path_override = path


def _connect() -> sqlite3.Connection:
    if _db_path_override is not None:
        conn = sqlite3.connect(str(_db_path_override))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    from plugins.plugin_storage import plugin_db
    return plugin_db(PLUGIN_ID)


def _as_int(v) -> "int | None":
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _row_from_event(ev: dict) -> dict:
    usage = ev.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    error = ev.get("error")
    if not isinstance(error, dict):
        error = {}
    retryable = ev.get("retryable")
    return {
        "ts": ev.get("ts") or time.time(),
        "type": ev.get("type"),
        "session_id": ev.get("session_id"),
        "turn_id": ev.get("turn_id"),
        "api_request_id": ev.get("api_request_id"),
        "model": ev.get("model"),
        "provider": ev.get("provider"),
        "api_duration": ev.get("api_duration"),
        "finish_reason": ev.get("finish_reason"),
        "prompt_tokens": _as_int(usage.get("prompt_tokens", usage.get("input_tokens"))),
        "completion_tokens": _as_int(usage.get("completion_tokens", usage.get("output_tokens"))),
        "total_tokens": _as_int(usage.get("total_tokens")),
        "status_code": _as_int(ev.get("status_code")),
        "retry_count": _as_int(ev.get("retry_count")),
        "retryable": _as_int(retryable) if retryable is not None else None,
        "error_type": error.get("type"),
        "error_message": error.get("message"),
        "user_chars": _as_int(ev.get("user_chars")),
        "user_message": ev.get("user_message"),
        "sketch": ev.get("sketch"),
    }


def _persist_batch(batch: list) -> None:
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        for ev in batch:
            conn.execute(_INSERT, _row_from_event(ev))
        conn.commit()
    finally:
        conn.close()


def _prune() -> None:
    try:
        conn = _connect()
        try:
            conn.executescript(SCHEMA)
            conn.execute(
                "DELETE FROM events WHERE id <= "
                "(SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (MAX_EVENTS,),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception("its-in-your-head: prune failed")


def _flush_tick(n: int) -> None:
    """Optional live path: desktop subscribes to
    host.onEvent('plugin.its-in-your-head.tick', fn); polling is the
    mandatory fallback, so this is fire-and-forget."""
    if broadcast_plugin_event is None:
        return
    try:
        broadcast_plugin_event(PLUGIN_ID, "tick", {"events": n, "ts": time.time()})
    except Exception:
        logger.exception("its-in-your-head: tick broadcast failed")


def _worker_main() -> None:
    global _batches
    while not _shutdown.is_set():
        try:
            first = _queue.get(timeout=QUEUE_GET_TIMEOUT)
        except queue.Empty:
            continue
        batch = [first]
        while len(batch) < BATCH_MAX:
            try:
                batch.append(_queue.get_nowait())
            except queue.Empty:
                break
        try:
            for ev in batch:
                if isinstance(ev, dict) and ev.get("type") == "turn":
                    ev["sketch"] = embedder.sketch(ev.get("user_message"))
            _persist_batch(batch)
            _batches += 1
            if _batches % PRUNE_EVERY == 0:
                _prune()
            _flush_tick(len(batch))
        except Exception:
            logger.exception("its-in-your-head: batch persist failed (events dropped)")


def start_worker() -> None:
    """Daemon thread draining _queue: embed turn prompts, aggregate, persist
    through the lazy-opened plugin DB, optionally broadcast ticks.
    Idempotent — a second call while the worker is alive is a no-op.
    Call from register(ctx); the DB opens on the first event, never here."""
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _shutdown.clear()
        _worker_thread = threading.Thread(
            target=_worker_main, name="its-in-your-head-metrics", daemon=True)
        _worker_thread.start()


def stop_worker(timeout: float = 3.0) -> None:
    """Stop the worker (clean teardown / tests). Unflushed queue items are
    lost; persistence is best-effort telemetry."""
    global _worker_thread
    with _worker_lock:
        thread = _worker_thread
        _worker_thread = None
        _shutdown.set()
    if thread is not None:
        thread.join(timeout=timeout)
