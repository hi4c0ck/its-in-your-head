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
    from .core import events as _events
    from . import engine as _engine
except ImportError:
    import embedder
    from storage import database as _database
    from storage import migration as _migration
    from core import events as _events
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
PRUNE_EVERY = 50         # prune check every N worker batches (active floor)
# P2.2: time-based prune ceiling so an IDLE worker still ages out expired
# events. The batch floor alone would never fire with no activity.
PRUNE_INTERVAL_S = 300   # at most 5 min between prune passes when idle
_last_prune_ts = 0.0     # start_worker() reinitializes this to time.time();
                        # 0.0 here is only the pre-start placeholder.
BATCH_MAX = 64           # max events flushed per commit
QUEUE_GET_TIMEOUT = 0.5  # worker idle poll (s)
# v0.1.28 P0 (§2.4): the raw-text event queue is BOUNDED. A stopped or slow
# worker can no longer accumulate an unbounded backlog of raw prompts/reasoning
# in memory. The overflow + text-truncation counters are safe (no raw text).
MAX_QUEUE_EVENTS = 4096  # hard cap on the in-memory event backlog
MAX_PROMPT_CHARS = 16384  # incoming prompt analysis-text cap (bounded)

# §6: time-based retention window (days) for the events table, driven by the
# C4 `observation.retain_days` setting. The row-count cap (MAX_EVENTS) remains
# a hard upper bound on top of the age policy. Defaults to the C4 default;
# set_retain_days() pushes the live configured value into the worker.
RETAIN_DAYS_DEFAULT = 90
_retain_days = RETAIN_DAYS_DEFAULT


def set_retain_days(days) -> None:
    """§6: point the events-pruning age window at the live retention value.
    Clamped to a positive integer (0 disables the age leg; the row cap still
    applies). Called from the collector at register() time + on settings
    change so the pruning path actually consumes the configured value."""
    global _retain_days
    try:
        d = int(days)
    except (TypeError, ValueError):
        d = RETAIN_DAYS_DEFAULT
    _retain_days = d if d >= 0 else 0

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
# P1.1: a /reset in flight. While True the worker discards picked-up batches
# (no persist, no engine-side write) so a pre-wipe record can never be
# committed after the delete. Set/cleared by _set_reset_in_progress().
_reset_in_progress = False
_batches = 0


def _set_reset_in_progress(v: bool) -> None:
    """Toggle the /reset in-flight flag the worker checks before committing."""
    global _reset_in_progress
    _reset_in_progress = bool(v)


def reset_in_progress() -> bool:
    return _reset_in_progress

# Flush-barrier state (F4). _in_flight_events counts events the worker has
# dequeued but not yet committed (post-persist); _failed_events is the
# lifetime count of events dropped by a failed persist. _state_cv is
# notified on every increment/decrement/stop so flush() can wait on real
# state instead of polling the queue. The worker NEVER calls flush() while
# holding (or about to acquire) this condition, so there is no deadlock.
_in_flight_events = 0
_failed_events = 0
# v0.1.28 P0 (§2.4): per-event-type overflow accounting — safe counters only,
# no raw text is ever retained here. `dropped` = events discarded because the
# bounded queue was full; `rejected` = events whose raw prompt was truncated
# at the analysis-text cap before entering the queue.
_dropped = {}   # event-type -> count discarded on queue overflow
_rejected = {}  # event-type -> count truncated at the analysis-text cap
_state_cv = threading.Condition()


class FlushResult(NamedTuple):
    """Result of flush(): honest accounting, never a bare claim of drained.

    drained: True only when every event enqueued up to return was COMMITTED
        (queue empty, nothing in flight, and no persist failure, queue
        overflow, or text-truncation in the window). A failed/dropped batch
        makes drained False even with pending == 0 — check the counters.
    pending: events not yet committed at return (queued + in_flight).
    in_flight: dequeued-but-uncommitted events (mid-persist batches).
    queued: events still waiting on the queue.
    failed: events dropped by a failed persist during this flush window.
    dropped: events discarded because the bounded queue was full (§2.4).
    rejected: events whose raw prompt was truncated at the text cap (§2.4).

    Truthiness preserves the old `bool` polarity: bool(result) == drained.
    """

    drained: bool
    pending: int
    in_flight: int
    queued: int
    failed: int
    dropped: int = 0
    rejected: int = 0

    def __bool__(self) -> bool:
        return self.drained


def enqueue(event: dict) -> None:
    """Called from hooks — microseconds only, and NEVER blocking (§2.4). The
    raw-text queue is bounded: an over-cap prompt is truncated to the
    analysis-text cap before entering, and when the backlog is full the event
    is dropped (counted by type; the raw text is never logged or retained).
    A dropped observation means less analytical evidence — never a fallback
    to storing raw input elsewhere. No I/O, no embedding; the only lock is
    the shared condition (a deque append + notify)."""
    global _dropped, _rejected
    etype = event.get("type") if isinstance(event, dict) else "unknown"
    truncated = False
    if isinstance(event, dict):
        msg = event.get("user_message")
        if isinstance(msg, str) and len(msg) > MAX_PROMPT_CHARS:
            event["user_message"] = msg[:MAX_PROMPT_CHARS]
            event["user_chars"] = MAX_PROMPT_CHARS
            truncated = True
    with _state_cv:
        if truncated:
            _rejected[etype] = _rejected.get(etype, 0) + 1
        if len(_items) >= MAX_QUEUE_EVENTS:
            _dropped[etype] = _dropped.get(etype, 0) + 1
            _state_cv.notify_all()
            return
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
    # §4.1/P1.2: a STRICT error-category allowlist. Both the legacy error dict
    # + a producer-supplied top-level error_type are coerced through the same
    # allowlist, so a future producer cannot bypass the policy by passing a
    # dict directly to metrics.enqueue(). Arbitrary free-text (e.g. "Bearer
    # <token>") collapses to "unknown"; the raw message is never persisted.
    if ev.get("error_type") is not None:
        _error_type = _events._error_type(ev.get("error_type"))
    else:
        _error_type = _events._error_type(error) if error else None
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
        "error_type": _error_type,
        # §4.1/§4.2: arbitrary provider/exception messages are NEVER
        # persisted. error_type + the numeric status/retry counters are the
        # approved classification; the raw untrusted message stays out of the
        # DB (the column remains in the schema, written NULL). A future event
        # producer cannot bypass the policy by supplying a dict here.
        "error_message": None,
        "user_chars": _as_int(ev.get("user_chars")),
        # v0.1.3 P0: raw prompts are NEVER serialized (the column stays in
        # the schema, written NULL; the worker purges the raw field).
        "user_message": None,
        # D4 discard-after-scoring: the derived reasoning sketch BLOB no
        # longer persists — reasoning rows carry no sketch (the column
        # stays for turn rows only). Reasoning is encoded engine-side
        # from the in-memory raw text before the C6 purge.
        "sketch": None if ev.get("type") == "reasoning_seg" else ev.get("sketch"),
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
    """§6/P2.2: two complementary limits on the events table — (a) time-based
    retention from the live observation.retain_days window (older rows drop),
    and (b) the hard row-count cap (MAX_EVENTS) as a final upper bound. A
    retention of 0 disables the age leg; the row cap always applies. A
    successful prune stamps the P2.2 time-based trigger."""
    global _last_prune_ts
    try:
        conn = _connect()
        try:
            _migration.schema_bootstrap(conn, SCHEMA)
            if _retain_days > 0:
                cutoff = time.time() - _retain_days * 86400.0
                conn.execute(
                    "DELETE FROM events WHERE ts IS NOT NULL AND ts < ?",
                    (cutoff,))
            conn.execute(
                "DELETE FROM events WHERE id <= "
                "(SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (MAX_EVENTS,))
            conn.commit()
            _last_prune_ts = time.time()
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
            # P1.1: a batch picked up during a stop/reset must not commit — a
            # /reset wipe races the engine-side writes that would repopulate
            # just-deleted rows. Discard it (scrub + tick in the finally),
            # never persist + never feed the engine.
            if _shutdown.is_set() or _reset_in_progress:
                _flush_tick(len(batch))
            else:
                for ev in batch:
                    if isinstance(ev, dict) and ev.get("type") == "turn":
                        ev["sketch"] = embedder.sketch(ev.get("user_message"))
                # D4 discard-after-scoring: no Tier-0 sketch is attached
                # to reasoning_seg events anymore. The raw reasoning text
                # stays in memory only; the engine encodes it through the
                # configured backend during the feed below (one shared
                # vector space for prompt + reaction), and the C6 purge
                # after the feed discards it. Nothing derived from the
                # raw text persists as a BLOB.
                # Persist BEFORE the engine feed: the row mapping above no
                # longer serializes raw text (reasoning rows write a NULL
                # sketch), and the purge below keeps the raw fields out of
                # engine state too. The engine feed runs BEFORE the purge, so
                # the raw reasoning text is still in memory for engine-side
                # embedding (embed first — engine-side — purge last).
                _persist_batch(batch)
                _batches += 1
                # Activity-gated prune: the N-batch floor OR a time ceiling.
                # The ceiling lets an otherwise-idle worker age out expired
                # events without waiting for N more batches; both fire ONLY
                # after a real persist (the DB is open + in use) — never on
                # the idle wait, so an idle worker still never opens the DB
                # (the lazy invariant the lazy-DB test guards).
                if _batches % PRUNE_EVERY == 0 or \
                        time.time() - _last_prune_ts >= PRUNE_INTERVAL_S:
                    _prune()
                # Engine feed BEFORE the tick notification (v0.1.10 spec
                # §3.2): the desktop's next /pulse refresh is triggered by
                # the tick, so the pulse_tools registry must already be
                # updated — a tick that fires first makes the client read a
                # stale snapshot. The scrub below stays LAST (embed first,
                # purge last — the invariant from v0.1.3).
                if _engine is not None:
                    try:
                        _engine.on_events(batch)
                    except Exception:
                        logger.exception(
                            "its-in-your-head: engine feed failed (state dropped)")
                _flush_tick(len(batch))
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
            # §2.3 unconditional scrub: raw prompt + reasoning text are purged
            # on EVERY path — success, failed persist, engine failure, or a
            # dropped batch. The derived features (user_chars, reasoning_chars,
            # the turn sketch) were already persisted + fed above; the raw
            # fields never outlive this batch. The known raw-content keys are
            # scrubbed defensively so a future raw field cannot ride along.
            for ev in batch:
                if isinstance(ev, dict):
                    ev.pop("user_message", None)
                    ev.pop("reasoning_text", None)
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


def reset_transient_state() -> int:
    """§7.2/§7.4: discard the transient raw-text backlog — the pending event
    queue + the collector's SessionState reasoning buffers + completion
    markers — so no pre-wipe record can be committed after a /reset. Returns
    the number of queued events discarded. The caller stops the worker
    first (so an in-flight batch can't resurrect the backlog); fresh events
    collected after the wipe start a clean generation."""
    with _state_cv:
        n = len(_items)
        _items.clear()
    try:
        from core import collector as _collector
        _collector.STATE.clear_session(None)
    except Exception:
        pass
    return n


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
        dropped_at_entry = sum(_dropped.values())
        rejected_at_entry = sum(_rejected.values())
        while True:
            queued = len(_items)
            in_flight = _in_flight_events
            failed = _failed_events - failed_at_entry
            dropped = sum(_dropped.values()) - dropped_at_entry
            rejected = sum(_rejected.values()) - rejected_at_entry
            if queued == 0 and in_flight == 0:
                return FlushResult(
                    drained=(failed == 0 and dropped == 0 and rejected == 0),
                    pending=0, in_flight=0, queued=0, failed=failed,
                    dropped=dropped, rejected=rejected)
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
                    in_flight=in_flight, queued=queued, failed=failed,
                    dropped=dropped, rejected=rejected)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return FlushResult(
                    drained=False, pending=queued + in_flight,
                    in_flight=in_flight, queued=queued, failed=failed,
                    dropped=dropped, rejected=rejected)
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
    global _worker_thread, _last_prune_ts
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _shutdown.clear()
        # P2.2: the time-based prune ceiling counts from worker start, not
        # from the epoch (the module-level 0.0 would make the very first
        # persist look "stale" and trigger a start-prune that deletes
        # just-written rows). Idle workers still age out old rows on the
        # next activity after PRUNE_INTERVAL_S without breaking the lazy
        # invariant (no DB open on a pure idle wait).
        _last_prune_ts = time.time()
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


def stop_worker(timeout: float = 3.0) -> bool:
    """Stop the worker (clean teardown / tests). Returns True ONLY if the
    worker thread is confirmed stopped within `timeout`. A live worker
    (mid batch) makes this False; the thread reference is then KEPT so
    start_worker() will not spawn a second thread on top of it. Callers that
    need a hard stop (e.g. /reset) must abort or retry on False — they must
    not proceed with a destructive wipe while the worker can still write.
    Unflushed queue items are lost; persistence is best-effort telemetry."""
    global _worker_thread
    with _worker_lock:
        thread = _worker_thread
    _shutdown.set()
    # Stopping releases the flush barrier: wake any flush() waiters so
    # they return promptly with honest pending accounting.
    with _state_cv:
        _state_cv.notify_all()
    stopped = True
    if thread is not None:
        thread.join(timeout=timeout)
        stopped = not thread.is_alive()
    if stopped:
        with _worker_lock:
            # Clear the ref only if it still points at the thread we stopped
            # (a concurrent start_worker may have already replaced it).
            if _worker_thread is thread:
                _worker_thread = None
    return stopped
