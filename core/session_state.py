"""Session/turn/task/tool-call correlation ids, LIVE counters, and
per-turn reasoning-segment bookkeeping.

Pure in-memory, lock-guarded, microseconds on the hot path. The LIVE
counters are the synchronous half of pre_tool_call (C5): the hook
increments them immediately; the persisted row follows via the queue.
"""
import threading

REASONING_BUFFER_CAP = 8192  # chars; bounded per locked rule


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
            self._reasoning = {}  # turn_id -> {chunks, buffer, done}

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
            if kwargs.get("session_id"):
                self.session_id = kwargs["session_id"]
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
                self.session_id = session_id
            self.live["sessions_started"] += 1

    def session_ended(self, session_id=None):
        with self._lock:
            if session_id:
                self.session_id = session_id
            self.live["sessions_ended"] += 1

    # --- reasoning-segment bookkeeping ---------------------------------
    def reasoning_begin(self, turn_id):
        with self._lock:
            self._reasoning.setdefault(
                turn_id, {"chunks": 0, "buffer": "", "done": False})

    def reasoning_feed(self, turn_id, delta) -> bool:
        """Append a reasoning delta. Returns True while still capturing
        (first segment of this turn, buffer not yet flushed)."""
        if not isinstance(delta, str):
            delta = str(delta) if delta is not None else ""
        with self._lock:
            st = self._reasoning.setdefault(
                turn_id, {"chunks": 0, "buffer": "", "done": False})
            if st["done"]:
                return False
            st["chunks"] += 1
            room = REASONING_BUFFER_CAP - len(st["buffer"])
            if room > 0:
                st["buffer"] += delta[:room]
            return True

    def reasoning_flush(self, turn_id):
        """Pop the buffered first segment (once). Returns
        (text, chunk_count) or None if nothing to flush."""
        with self._lock:
            st = self._reasoning.get(turn_id)
            if not st or st["done"] or not st["buffer"]:
                if st:
                    st["done"] = True
                return None
            st["done"] = True
            self.live["reasoning_segments"] += 1
            return st["buffer"], st["chunks"]

    def reasoning_pending(self, turn_id) -> bool:
        with self._lock:
            st = self._reasoning.get(turn_id)
            return bool(st and not st["done"] and st["buffer"])
