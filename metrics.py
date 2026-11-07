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
import collections
import contextvars
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import NamedTuple

try:  # hermes namespaced import (gateway) vs top-level import (tests)
    from . import embedder
    from .storage import database as _database
    from .storage import migration as _migration
    from . import engine as _engine
except ImportError:
    import embedder
    from storage import database as _database
    from storage import migration as _migration
    try:
        import engine as _engine
    except ImportError:  # engine is optional plumbing; worker still persists
        _engine = None

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

# SCHEMA — alias of the single canonical DDL (storage.migration.EVENTS_DDL).
# Kept as a module-level name for existing callers/tests; do not copy the DDL.
SCHEMA = _migration.EVENTS_DDL

_COLUMNS = [
    "ts", "type", "session_id", "turn_id", "api_request_id", "model",
    "provider", "api_duration", "finish_reason", "prompt_tokens",
    "completion_tokens", "total_tokens", "status_code", "retry_count",
    "retryable", "error_type", "error_message", "user_chars", "user_message",
    "sketch", "tool_name", "task_id", "tool_call_id", "duration_ms",
    "result_type", "result_chars", "status", "finished", "reasoning_chars",
    "reasoning_deltas", "final_chars",
]
_INSERT = ("INSERT INTO events (" + ", ".join(_COLUMNS) + ") VALUES ("
           + ", ".join(":" + c for c in _COLUMNS) + ")")

# Shared event deque, guarded by _state_cv (the flush barrier's condition).
# A bare collections.deque instead of queue.Queue: NO blocking operation is
# ever performed while holding the barrier lock — the worker's idle wait is
# cv.wait() (which releases the lock while sleeping), so flush() and the
# producer can always acquire the condition. (v0.1.3 liveness fix: the
# previous queue.Queue.get(timeout=...) under the condition starved flush()
# lock acquisition on slow CI.)
_items: "collections.deque" = collections.deque()

_worker_lock = threading.Lock()
_worker_thread: "threading.Thread | None" = None
_shutdown = threading.Event()
_batches = 0

# Flush-barrier state (F4). _in_flight_events counts events the worker has
# dequeued but not yet committed (post-persist); _failed_events is the
# lifetime count of events dropped by a failed persist. _state_cv is
# notified on every increment/decrement/stop so flush() can wait on real
# state instead of polling the queue. The worker NEVER calls flush() while
# holding (or about to acquire) this condition, so there is no deadlock.
_in_flight_events = 0
_failed_events = 0
_state_cv = threading.Condition()


class FlushResult(NamedTuple):
    """Result of flush(): honest accounting, never a bare claim of drained.

    drained: True only when every event enqueued up to return was COMMITTED
        (queue empty, nothing in flight, no persist failure in the window).
        A failed batch is dropped via the error path (not retried), so
        drained is False even with pending == 0 — check `failed`.
    pending: events not yet committed at return (queued + in_flight).
    in_flight: dequeued-but-uncommitted events (mid-persist batches).
    queued: events still waiting on the queue.
    failed: events dropped by a failed persist during this flush window.

    Truthiness preserves the old `bool` polarity: bool(result) == drained.
    """

    drained: bool
    pending: int
    in_flight: int
    queued: int
    failed: int

    def __bool__(self) -> bool:
        return self.drained


def enqueue(event: dict) -> None:
    """Called from hooks — microseconds only: build a dict, append it to the
    shared deque. No I/O, no embedding; the only lock is the shared
    condition (a deque append + notify)."""
    with _state_cv:
        _items.append(event)
        _state_cv.notify_all()


def set_db_path(path: "Path | str") -> None:
    """TEST ONLY: point storage at a local file instead of plugin_db().
    Forwards to storage.database.set_override — the single override (P2)."""
    _database.set_override(path)


def _connect() -> sqlite3.Connection:
    return _database.connect()


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
        # v0.1.3 P0: raw prompts are NEVER serialized (the column stays in
        # the schema, written NULL; the worker purges the raw field).
        "user_message": None,
        "sketch": ev.get("sketch"),
        "tool_name": ev.get("tool_name"),
        "task_id": ev.get("task_id"),
        "tool_call_id": ev.get("tool_call_id"),
        "duration_ms": ev.get("duration_ms"),
        "result_type": ev.get("result_type"),
        "result_chars": _as_int(ev.get("result_chars")),
        "status": ev.get("status"),
        "finished": _as_int(ev.get("finished"))
        if ev.get("finished") is not None else None,
        "reasoning_chars": _as_int(ev.get("reasoning_chars")),
        "reasoning_deltas": _as_int(ev.get("reasoning_deltas")),
        "final_chars": _as_int(ev.get("final_chars")),
    }


def _persist_batch(batch: list) -> None:
    conn = _connect()
    try:
        _migration.schema_bootstrap(conn, SCHEMA)
        for ev in batch:
            conn.execute(_INSERT, _row_from_event(ev))
        conn.commit()
    finally:
        conn.close()


def _prune() -> None:
    try:
        conn = _connect()
        try:
            _migration.schema_bootstrap(conn, SCHEMA)
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
    global _batches, _in_flight_events, _failed_events
    while not _shutdown.is_set():
        # Dequeue + in-flight accounting happen atomically under the flush
        # barrier's condition: an item can never be OUT of _items but NOT
        # YET counted in in_flight (that gap let flush() observers see
        # empty + in_flight==0 while a live batch was draining). The idle
        # wait is cv.wait(), which RELEASES the lock while sleeping — no
        # blocking queue operation ever runs under the barrier lock, so
        # flush() can always acquire it.
        batch: list = []
        with _state_cv:
            while not _items and not _shutdown.is_set():
                _state_cv.wait(timeout=QUEUE_GET_TIMEOUT)
            if _shutdown.is_set() and not _items:
                break
            while _items and len(batch) < BATCH_MAX:
                batch.append(_items.popleft())
                _in_flight_events += 1
            if batch:
                _state_cv.notify_all()
        if not batch:
            continue
        try:
            for ev in batch:
                if isinstance(ev, dict) and ev.get("type") == "turn":
                    ev["sketch"] = embedder.sketch(ev.get("user_message"))
                elif isinstance(ev, dict) and ev.get("type") == "reasoning_seg":
                    # Worker-side embedding (never on the delta path);
                    # the raw reasoning text is not persisted (C6).
                    ev["sketch"] = embedder.sketch(ev.get("reasoning_text"))
            # Persist BEFORE the engine feed: the row mapping no longer
            # serializes raw text (see _row_from_event), and the purge
            # below keeps the raw fields out of engine state too.
            _persist_batch(batch)
            _batches += 1
            if _batches % PRUNE_EVERY == 0:
                _prune()
            _flush_tick(len(batch))
            # Engine feed (pulse capsules, portrait update) — worker side
            # only, never on the hot path. A fault here drops engine
            # state, not the persisted events.
            if _engine is not None:
                try:
                    _engine.on_events(batch)
                except Exception:
                    logger.exception(
                        "its-in-your-head: engine feed failed (state dropped)")
            # Privacy scrub (v0.1.3 P0), AFTER the engine feed (feedback
            # detection legitimately reads the raw turn text in memory):
            # raw prompt/reasoning text lives in memory only — embed
            # FIRST, purge LAST so the raw fields can never outlive this
            # batch. Derived features (user_chars, reasoning_chars,
            # sketch) are kept. No migration scrub: no real users
            # pre-v0.1.3 (owner ruling).
            for ev in batch:
                if isinstance(ev, dict):
                    ev.pop("user_message", None)
                    ev.pop("reasoning_text", None)
        except Exception:
            # Failed-batch policy: report via the error path below and DROP
            # the batch — flush() does not retry. The dropped count is
            # accumulated so flush() reports it instead of silently
            # claiming the work drained.
            logger.exception("its-in-your-head: batch persist failed (events dropped)")
            with _state_cv:
                _failed_events += len(batch)
                _state_cv.notify_all()
        finally:
            # Post-persist commit accounting (runs on success AND failure):
            # the batch is no longer in flight. Never calls flush() here.
            with _state_cv:
                if _in_flight_events >= len(batch):
                    _in_flight_events -= len(batch)
                else:
                    # Invariant guard: the counter may never go negative —
                    # a deficit makes flush() under-report pending and break
                    # the committed+pending==total conservation tests. A
                    # deficit means a decrement outran its increments
                    # (observed when a prior worker thread survives a test
                    # reload and double-drains the shared state under load):
                    # log the evidence, clamp to the honest floor.
                    logger.warning(
                        "its-in-your-head: in-flight underflow "
                        "(%d < %d); clamping to 0",
                        _in_flight_events, len(batch))
                    _in_flight_events = 0
                _state_cv.notify_all()


def flush(timeout: float = 3.0) -> FlushResult:
    """Flush barrier: wait until the queue is empty AND no batch is in
    flight (dequeued but not yet committed).

    Returns a FlushResult (bool(result) == result.drained). Contract:
    - drained is True only when everything enqueued up to return was
      committed: queue empty, in_flight == 0, and no persist failure
      occurred during this flush window. Never True while work is
      uncommitted.
    - Timeout (default 3.0s): bounded wait; on timeout returns
      drained=False with the actual pending/in_flight/queued counts —
      never claims drained when work is uncommitted.
    - Failure policy: a failed batch is reported via the error path and
      dropped, NOT retried; its events are counted in `failed` and make
      drained False for that window even though pending == 0.
    - stop_worker() releases the barrier: when shutdown is set AND no work
      is in flight, flush returns promptly with drained=False (unless
      everything is committed). While a live worker still has a dequeued
      batch to commit, flush keeps waiting (bounded by timeout) instead of
      early-returning work that was about to commit. The worker never
      calls flush(), so there is no self-deadlock.
    """
    deadline = time.monotonic() + timeout
    with _state_cv:
        failed_at_entry = _failed_events
        while True:
            queued = len(_items)
            in_flight = _in_flight_events
            failed = _failed_events - failed_at_entry
            if queued == 0 and in_flight == 0:
                return FlushResult(
                    drained=(failed == 0), pending=0, in_flight=0,
                    queued=0, failed=failed)
            if _shutdown.is_set() and in_flight == 0:
                # Worker stopping releases the barrier — but ONLY when no
                # work is in flight: a live worker that has dequeued a batch
                # will commit it and run its post-persist decrement (the
                # finally), so returning early would report work that was
                # about to commit as 'pending'. In-flight + stopping worker
                # falls through to the bounded wait below; once the commit
                # lands (in_flight -> 0) the drain check above succeeds.
                # A DEAD worker can never have in_flight > 0 (the decrement
                # precedes thread exit), so this branch is reachable only
                # with a live worker or a crashed one.
                return FlushResult(
                    drained=False, pending=queued + in_flight,
                    in_flight=in_flight, queued=queued, failed=failed)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return FlushResult(
                    drained=False, pending=queued + in_flight,
                    in_flight=in_flight, queued=queued, failed=failed)
            # Short wait slice (enqueue/worker notify on state changes, but
            # re-poll on every wakeup to stay correct regardless).
            _state_cv.wait(timeout=min(remaining, 0.02))


def start_worker() -> None:
    """Daemon thread draining _queue: embed turn prompts, aggregate, persist
    through the lazy-opened plugin DB, optionally broadcast ticks.
    Idempotent — a second call while the worker is alive is a no-op.
    Call from register(ctx); the DB opens on the first event, never here.

    Hermes loads each profile's plugins under that profile's context-local
    HERMES_HOME override. Preserve that context in the worker: otherwise
    plugin_data_dir()/plugin_db() can fall back to the gateway's launch
    (default) home and split the writer from the profile-scoped dashboard.
    """
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _shutdown.clear()
        try:
            # Hermes' helper copies the current ContextVars into the new
            # thread. PluginManager loads a profile's plugin under that
            # profile's HERMES_HOME scope, so this worker remains bound to
            # the same profile as register(ctx) and its dashboard child.
            from agent.memory_provider import spawn_context_thread
        except ModuleNotFoundError as exc:
            # The standalone plugin test suite intentionally runs without
            # the Hermes runtime installed. Keep the exact context-copy
            # semantics there, using only the Python standard library.
            if exc.name not in {"agent", "agent.memory_provider"}:
                raise
            context = contextvars.copy_context()
            _worker_thread = threading.Thread(
                target=context.run, args=(_worker_main,),
                name="its-in-your-head-metrics", daemon=True)
        else:
            _worker_thread = spawn_context_thread(
                _worker_main, name="its-in-your-head-metrics", daemon=True)
        _worker_thread.start()


def stop_worker(timeout: float = 3.0) -> None:
    """Stop the worker (clean teardown / tests). Unflushed queue items are
    lost; persistence is best-effort telemetry."""
    global _worker_thread
    with _worker_lock:
        thread = _worker_thread
        _worker_thread = None
        _shutdown.set()
    # Stopping releases the flush barrier: wake any flush() waiters so
    # they return promptly with honest pending accounting.
    with _state_cv:
        _state_cv.notify_all()
    if thread is not None:
        thread.join(timeout=timeout)
