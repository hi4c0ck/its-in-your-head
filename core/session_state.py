"""Session/turn/task/tool-call correlation ids, LIVE counters, and
per-turn reasoning-segment bookkeeping.

Pure in-memory, lock-guarded, microseconds on the hot path. The LIVE
counters are the synchronous half of pre_tool_call (C5): the hook
increments them immediately; the persisted row follows via the queue.

Privacy hardening (v0.1.28, plan §2): the reasoning buffer is a transient
raw-text sink. It is keyed by (session_id, turn_id), bounded in BOTH the
number of active buffers and the number of completion markers, flushed
atomically (the text is retrieved and the entry removed at the same
instant), and cleared on session termination so one session's raw text
can never be read by a later turn. A completed-turn marker is metadata,
not a retained text buffer.
"""
import threading

REASONING_BUFFER_CAP = 8192      # chars; bounded per buffer (locked rule)
REASONING_MAX_ACTIVE = 64        # max concurrent unfinished raw buffers
REASONING_MAX_COMPLETED = 128    # bounded FIFO completion-marker collection
REASONING_MAX_ENDED = 128        # bounded FIFO ended-session tombstones


class SessionState:
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.session_id = None
            self.turn_id = None
            self.task_id = None
            self.live = {
                "tool_calls_started": 0,
                "tool_calls_completed": 0,
                "tool_errors": 0,
                "reasoning_segments": 0,
                "stream_deltas": 0,
                "sessions_started": 0,
                "sessions_ended": 0,
            }
            # (session_id, turn_id) -> {chunks, buffer, done}; the parallel
            # insertion-order lists give deterministic FIFO eviction.
            self._reasoning = {}
            self._reasoning_order = []
            # (session_id, turn_id) -> True: a bounded completion marker so a
            # late delta for an already-flushed turn cannot recreate its
            # buffer. Metadata only — no text is retained here.
            self._completed = {}
            self._completed_order = []
            # P1.3: bounded ended-session tombstones. A late reasoning delta
            # for an already-ended session is rejected (no buffer is ever
            # recreated for a finished session). Metadata only.
            self._ended = {}
            self._ended_order = []

    # --- correlation -------------------------------------------------
    def correlate(self, kwargs) -> dict:
        """Pull correlation ids out of hook kwargs (missing -> None)."""
        with self._lock:
            return {
                "session_id": kwargs.get("session_id") or self.session_id,
                "turn_id": kwargs.get("turn_id") or self.turn_id,
                "task_id": kwargs.get("task_id") or self.task_id,
            }

    def note(self, kwargs) -> dict:
        """Update last-seen correlation ids; return the correlated dict."""
        with self._lock:
            new_sid = kwargs.get("session_id")
            if new_sid:
                # P1.3: a session transition must not inherit the previous
                # session's turn/task correlation.
                if new_sid != self.session_id:
                    self.turn_id = None
                    self.task_id = None
                self.session_id = new_sid
            if kwargs.get("turn_id"):
                self.turn_id = kwargs["turn_id"]
            if kwargs.get("task_id"):
                self.task_id = kwargs["task_id"]
            return {"session_id": self.session_id, "turn_id": self.turn_id,
                    "task_id": self.task_id}

    # --- LIVE counters -----------------------------------------------
    def inc(self, name, n=1):
        with self._lock:
            self.live[name] = self.live.get(name, 0) + n

    def counter(self, name) -> int:
        with self._lock:
            return self.live.get(name, 0)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.live)

    # --- session lifecycle ---------------------------------------------
    def session_started(self, session_id=None):
        with self._lock:
            if session_id:
                # P1.3: a (re)started session is active again — lift its
                # ended tombstone + start fresh correlation (no stale turn).
                self._mark_ended(session_id, drop=True)
                self.session_id = session_id
                self.turn_id = None
                self.task_id = None
            self.live["sessions_started"] += 1

    def session_ended(self, session_id=None):
        with self._lock:
            if session_id:
                self.session_id = session_id
                # P1.3: tombstone the ended session so a late reasoning delta
                # for it cannot recreate a raw buffer, + drop its turn/task
                # correlation so the next session inherits nothing.
                self._mark_ended(session_id, drop=False)
                self.turn_id = None
                self.task_id = None
            self.live["sessions_ended"] += 1

    def _mark_ended(self, session_id, drop):
        """Manage the bounded ended-session tombstone (caller holds the lock).
        drop=True removes the tombstone (session restarted active); drop=False
        adds it (session ended)."""
        if drop:
            self._ended.pop(session_id, None)
            if session_id in self._ended_order:
                self._ended_order.remove(session_id)
        else:
            if session_id not in self._ended:
                self._ended[session_id] = True
                self._ended_order.append(session_id)
            while len(self._ended_order) > REASONING_MAX_ENDED:
                old = self._ended_order.pop(0)
                self._ended.pop(old, None)

    def session_ended_is_tombstoned(self, session_id) -> bool:
        with self._lock:
            return session_id in self._ended

    # --- reasoning-segment bookkeeping (bounded, session-scoped) ----------
    @staticmethod
    def _key(session_id, turn_id):
        return (session_id, turn_id)

    def _evict_reasoning(self):
        """FIFO-evict the oldest active buffer so _reasoning never exceeds
        REASONING_MAX_ACTIVE (deterministic; the caller holds the lock)."""
        while len(self._reasoning_order) > REASONING_MAX_ACTIVE:
            old = self._reasoning_order.pop(0)
            self._reasoning.pop(old, None)

    def _evict_completed(self):
        """FIFO-evict the oldest completion marker (the caller holds the lock)."""
        while len(self._completed_order) > REASONING_MAX_COMPLETED:
            old = self._completed_order.pop(0)
            self._completed.pop(old, None)

    def reasoning_begin(self, session_id, turn_id):
        with self._lock:
            # P1.3: a late delta for an already-ended session is rejected —
            # no buffer is ever (re)created for a finished session.
            if session_id in self._ended:
                return
            key = self._key(session_id, turn_id)
            if key not in self._reasoning:
                self._reasoning[key] = {"chunks": 0, "buffer": "", "done": False}
                self._reasoning_order.append(key)
                self._evict_reasoning()

    def reasoning_feed(self, session_id, turn_id, delta) -> bool:
        """Append a reasoning delta. Returns True while still capturing
        (first segment of this turn, buffer not yet flushed). A late delta
        for an already-flushed turn is a no-op (its completion marker wins).
        P1.3: a delta for an already-ENDED session is rejected outright so it
        can never resurrect a raw buffer."""
        if not isinstance(delta, str):
            delta = str(delta) if delta is not None else ""
        with self._lock:
            if session_id in self._ended:
                return False
            key = self._key(session_id, turn_id)
            if key in self._completed:
                return False
            st = self._reasoning.get(key)
            if st is None:
                st = self._reasoning[key] = {
                    "chunks": 0, "buffer": "", "done": False}
                self._reasoning_order.append(key)
                self._evict_reasoning()
            if st["done"]:
                return False
            st["chunks"] += 1
            room = REASONING_BUFFER_CAP - len(st["buffer"])
            if room > 0:
                st["buffer"] += delta[:room]
            return True

    def reasoning_flush(self, session_id, turn_id):
        """Atomically pop the buffered first segment (once). Returns
        (text, chunk_count) or None if nothing to flush. The entry is removed
        AND a bounded completion marker is set in the same instant, so a late
        delta cannot recreate the raw text."""
        with self._lock:
            key = self._key(session_id, turn_id)
            st = self._reasoning.pop(key, None)
            if key in self._reasoning_order:
                self._reasoning_order.remove(key)
            if st is None:
                return None
            self.live["reasoning_segments"] += 1
            if key not in self._completed:
                self._completed[key] = True
                self._completed_order.append(key)
                self._evict_completed()
            return st["buffer"], st["chunks"]

    def reasoning_pending(self, session_id, turn_id) -> bool:
        with self._lock:
            st = self._reasoning.get(self._key(session_id, turn_id))
            return bool(st and not st["done"] and st["buffer"])

    def clear_session(self, session_id=None):
        """Remove unfinished reasoning buffers + completion markers for the
        session so a session transition cannot leave the preceding session's
        raw text reachable. session_id=None clears everything (the host ended
        a session without supplying its id). P1.3: a cleared session is also
        tombstoned so a late reasoning delta for it is rejected (the cleared
        completion markers alone would no longer block resurrection)."""
        with self._lock:
            if session_id is None:
                self._reasoning.clear()
                self._completed.clear()
                self._reasoning_order.clear()
                self._completed_order.clear()
                # Reset the live correlation; keep the bounded tombstones so
                # late deltas for recently-ended sessions stay rejected.
                self.turn_id = None
                self.task_id = None
                return
            self._reasoning = {
                k: v for k, v in self._reasoning.items() if k[0] != session_id}
            self._completed = {
                k: v for k, v in self._completed.items() if k[0] != session_id}
            self._reasoning_order = [k for k in self._reasoning_order
                                      if k[0] != session_id]
            self._completed_order = [k for k in self._completed_order
                                      if k[0] != session_id]
            # P1.3: tombstone the cleared session (bounded) so a late delta
            # cannot recreate its buffer.
            self._mark_ended(session_id, drop=False)
