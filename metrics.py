"""Metrics pipeline skeleton — OWNER codes queue, worker, storage.

Locked constraints (doc + owner decisions; see ARCHITECTURE.md #Metrics):
- Hot-path rule: hooks call enqueue() only; ALL work happens in a daemon worker.
- Lazy DB: never open plugin_db at register() time (doctor's temp-home
  cleanup; WinError 145 on Windows). Open on first event, not at import.
- Storage: plugin_db("its-in-your-head") — WAL SQLite under
  plugin-data/its-in-your-head/, NEVER inside the plugin dir (install tree).
- Push to desktop (optional live path):
      from hermes_cli.plugin_events import broadcast_plugin_event
      broadcast_plugin_event("its-in-your-head", "tick", {...})
  bare dotted event; fire-and-forget; no-op when no desktop client attached.
"""
import queue

_queue: "queue.Queue" = queue.Queue()


def enqueue(event: dict) -> None:
    """Called from hooks — microseconds only. Event dict schema is an OWNER
    decision; document it here once defined. Suggested keys: type, ts,
    session_id, model, plus per-event payloads (user_message / usage / error)."""
    _queue.put(event)


def start_worker() -> None:
    """OWNER SLOT: daemon thread draining _queue — embed (embedder.sketch),
    aggregate, persist through the lazy-opened plugin_db connection, and
    optionally broadcast ticks. Idempotent (second call is a no-op).
    Call from register(ctx)."""
    raise NotImplementedError("owner: implement worker (ARCHITECTURE.md #Metrics)")
