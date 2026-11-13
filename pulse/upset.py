"""User-upset friction accumulator (D1-D6, 2026-11-12 plan).

Two state variables, one input s_t (the per-turn UPSET score read from the
LLM's own reasoning, D1):

- **F' — "friction NOW"** (fast): a strict 6-turn window with 0.73
  recency decay (D2). Drives the friction-lane ``user`` pseudo-entry
  (D4: F' >= 0.30 the entry shows, >= 0.65 the portrait glitch stays
  active, intensity proportional to F'). Per-session, in-memory; reset on
  session end / backend restart (D3). A short good reaction (the
  feedback table's one retained job) hard-resets F' to 0 (D5) — it
  answers "no friction NOW" and never touches M.
- **M — slow mood** (the prognostic number, D6): ``M = (1-beta)*M +
  beta*(1 - s_t)``, beta = 0.1. Strain builds and releases at the SAME
  slow rate: a good moment never erases accumulated strain, it pulls M
  toward good slowly. M persists across sessions (the ``meta`` KV
  store: current value + a bounded session-level history the read-time
  prognosis trend is computed from — no new table, no write-path
  state).

Guardrails (module invariants):
- **No LLM invocation.** PULSE reads the LLM's already-streamed
  reasoning text (``reasoning_seg`` events) and encodes it locally with
  the ACTIVE encoder — one shared vector space, never a second model.
- **Pure embedding heuristic (owner-locked 2026-11-12): NO
  per-language word lists, NO keyword matching, NO Russian words
  anywhere in this pipeline. Text in ANY language is scored by cosine
  in one multilingual vector space against the English anchor family —
  the signal is language-agnostic by construction (a foreign-language
  outburst lands next to the English anger anchors in that space).
- **Deterministic + bounded.** Anchor families are a fixed phrase table
  (like FEEDBACK_RULES); scores are clamped; the window is strict; every
  public method is fault-tolerant (a fault degrades to "no evidence",
  never an exception to the caller, never a worker death).
- **No raw text is stored or logged.** Only the derived numbers
  (F', M, tier, upset-turn count) leave this module.
"""
from __future__ import annotations

import json
import logging
import math
import time as _time
from collections import deque

logger = logging.getLogger(__name__)

# --- D2/D4: the locked dynamics -------------------------------------------- #
UPSET_DECAY = 0.73            # per-turn recency decay (owner-locked)
UPSET_WINDOW = 6              # strict window in turns (owner-locked)
_UPSET_WINDOW_NORM = sum(UPSET_DECAY ** i for i in range(UPSET_WINDOW))
UPSET_LANE_TIER = 0.30        # D4: the friction-lane user entry appears
UPSET_GLITCH_TIER = 0.65     # D4: the sustained portrait glitch is active
UPSET_UPSET_SCORE_MIN = 0.30  # a turn whose s >= this counts as an
                              # "upset-narrating" turn (support fact)

# --- D6: the slow mood ------------------------------------------------------ #
MOOD_BETA = 0.1               # owner-locked: M = 0.9*M + 0.1*(1 - s_t)
MOOD_NEUTRAL = 0.5
MOOD_HISTORY_MAX = 64         # bounded session-level history (meta KV)
PROGNOSIS_HORIZON = 5         # the read-time trend reads this many values

# --- D1: the anchor families ------------------------------------------------ #
# Phrases the LLM's REASONING uses when it narrates that the user is
# upset (English regardless of the user's language — that is the point:
# the signal is language-agnostic by construction). A turn's score is the
# max cosine over the family, clamped to [0, 1]; an unrelated turn reads
# ~0.1-0.3 (the MiniLM floor), which stays under the lane tier.
UPSET_PHRASES: tuple[str, ...] = (
    # D1: the LLM's reasoning narrates the user's mood in calm
    # third-person English — the semantic half of the family.
    "the user is upset",
    "the user is annoyed",
    "the user is irritated",
    "the user is frustrated",
    "the user is angry",
    "the user is losing patience",
    "the user's tone is harsh",
    "the user is fed up",
    # Immediacy feed (2026-11-12): the user's OWN raw message (in any
    # language) is scored against the SAME family — the multilingual
    # encoder maps a foreign-language outburst into this space, so no
    # per-language word list is ever needed or allowed.
    "i am furious",
    "i'm very mad",
    "i am so angry",
    "i'm losing my patience",
    "you didn't do what i asked",
    "this is bullshit",
    "what the f*ck",
)
REASONING_CAP_CHARS = 2000    # D1: per-turn reasoning text cap
SESSION_TTL_S = 600.0         # stale per-session F state is evicted
_MAX_SESSIONS = 16

# D3 cross-process handoff (2026-11-12): the F' mirror key in the ``meta``
# KV store. The isolated dashboard child (which serves the pane's /pulse)
# has NO live engine — it reconstructs the friction lane + glitch from this
# persisted mirror, not from in-memory F'. It carries ONLY derived numbers
# (the scored window, the upset-turn count, a timestamp) — never raw text.
FSTATE_KEY = "upset_fstate"


def prognosis_from_history(history) -> dict | None:
    """The D6 read-time forecast as a module function (shared by the
    instance view and the dashboard fallback's persisted-state read): the
    linear trend over the last PROGNOSIS_HORIZON session-level M values.
    None until at least 2 values exist (honest absence)."""
    values: list[float] = []
    for e in history or []:
        if not isinstance(e, dict):
            continue
        v = e.get("value")
        if isinstance(v, (int, float)):
            values.append(float(v))
    if len(values) < 2:
        return None
    tail = values[-PROGNOSIS_HORIZON:]
    n = len(tail)
    xs = [i for i in range(n)]
    x_mean = sum(xs) / n
    y_mean = sum(tail) / n
    denom = sum((x - x_mean) ** 2 for x in xs)
    slope = (sum((x - x_mean) * (y - y_mean)
                 for x, y in zip(xs, tail)) / denom) if denom else 0.0
    return {"values": tail, "slope": round(slope, 6), "horizon": n}


def _cosine(a, b) -> float:
    """Plain cosine in [0, 1] (clamped): the anchor families are short,
    the vectors small — no numpy, no I/O."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _tier(value: float) -> int:
    """0 = off, 1 = lane entry, 2 = glitch active (D4)."""
    if value >= UPSET_GLITCH_TIER:
        return 2
    if value >= UPSET_LANE_TIER:
        return 1
    return 0


class UpsetAccumulator:
    """Per-session F' windows + the global slow mood M (D1-D6).

    The engine feeds it three event types (all fault-isolated at the
    call site): ``on_reasoning_seg`` (D1 signal), ``on_user_turn`` (D5
    good-reset), ``close_session`` (D3 close + D6 persist). Pure math —
    storage is injected per call (``persist_mood`` / ``hydrate_mood``
    take a connection) so the unit tests never touch a real DB.
    """

    def __init__(self) -> None:
        # session_id -> {"window": deque(maxlen=6) of s (oldest first),
        # "reasoning": {turn_key: capped text}, "last_turn": key,
        # "last_ts": float, "upset_turns": int}
        self._sessions: dict[str, dict] = {}
        self._active: str | None = None
        # D6: the global slow mood (persists across sessions).
        self.M: float = MOOD_NEUTRAL
        self._mood_hydrated = False
        # True once any _step has integrated M in this process — a
        # stepped (or hydrated) M is the process's own truth; only a
        # never-stepped M may be seeded from the persisted hand-off.
        self._mood_stepped = False
        self._mood_history: list = []
        # Anchor vectors per encoder id (lazily embedded, D1).
        self._anchors: dict[str, list] = {}
        # The backend the last successful encode used (kept so the
        # session-close finalize can re-score without the engine passing
        # it again; None until the first successful score_reasoning).
        self._last_backend = None

    @property
    def mood_hydrated(self) -> bool:
        """True once the one-time meta-KV restore has been ATTEMPTED
        this process (success or not — no retry hammering of a broken
        DB)."""
        return self._mood_hydrated

    # -- D1: the per-turn upset score --------------------------------------- #
    def _ensure_anchors(self, backend, backend_name: str | None) -> list | None:
        """Embed the anchor family with the ACTIVE encoder (cached per
        encoder id — an encoder switch re-embeds). None when the backend
        cannot encode (no evidence, not an error)."""
        if backend is None:
            return None
        key = str(backend_name or getattr(backend, "name", "default"))
        cached = self._anchors.get(key)
        if cached is not None:
            return cached
        try:
            vecs = backend.encode(list(UPSET_PHRASES))
            vectors = [list(v) for v in vecs if v]
        except Exception:
            logger.exception(
                "its-in-your-head: upset anchor embed failed")
            return None
        if not vectors:
            return None
        self._anchors[key] = vectors
        return vectors

    def score_reasoning(self, text: str, backend,
                        backend_name: str | None = None) -> float:
        """s_t in [0, 1] for one turn's aggregated reasoning text.

        0.0 for empty/undecodable input (no evidence, not an error);
        the max cosine over the anchor family otherwise (the D1
        signal). A successful encode records the backend so the
        session-close finalize step can re-score (see close_session)."""
        if not isinstance(text, str) or not text.strip():
            return 0.0
        anchors = self._ensure_anchors(backend, backend_name)
        if not anchors:
            return 0.0
        try:
            (vec,) = backend.encode([text[:REASONING_CAP_CHARS]])
        except Exception:
            logger.exception("its-in-your-head: upset reasoning encode failed")
            return 0.0
        if not vec or any(len(v) != len(vec) for v in anchors):
            return 0.0  # vector-space mismatch: no evidence, not an error
        self._last_backend = backend
        sem = max(_cosine(vec, a) for a in anchors)
        return _clip01(sem)

    # -- session bookkeeping ------------------------------------------------- #
    def _evict_stale(self, now: float) -> None:
        for sid in [s for s, st in self._sessions.items()
                    if now - st["last_ts"] > SESSION_TTL_S]:
            del self._sessions[sid]
        overflow = len(self._sessions) - _MAX_SESSIONS
        if overflow > 0:
            oldest = sorted(self._sessions.items(),
                            key=lambda kv: kv[1]["last_ts"])[:overflow]
            for sid, _ in oldest:
                del self._sessions[sid]

    def _session(self, session_id: str | None, now: float) -> dict:
        sid = str(session_id or "local")
        st = self._sessions.get(sid)
        if st is None:
            st = {"window": deque(maxlen=UPSET_WINDOW),
                  "reasoning": {},
                  "last_turn": None,
                  "last_ts": now,
                  "upset_turns": 0}
            self._sessions[sid] = st
        # D3 close: a NEW session arriving finalizes the previous one
        # (its last turn is scored into M; the F state is dropped — it
        # is a "now" signal, D3). The M hand-off ROW is written by the
        # session_end close (close_session), not here: M is global and
        # continuous in memory across the switch.
        if self._active is not None and self._active != sid:
            self._close_session_impl(self._active, persist=False)
        self._active = sid
        st["last_ts"] = now
        return st

    def _step(self, st: dict, s: float) -> None:
        """One finalized turn: advance the strict decay window + M (D6)."""
        s = _clip01(s)
        st["window"].append(s)
        if s >= UPSET_UPSET_SCORE_MIN:
            st["upset_turns"] += 1
        # D6: slow mood — strain (s=1) and ease (s=0) move M at the same
        # rate; a good moment never erases accumulated strain.
        self.M = _clip01((1.0 - MOOD_BETA) * self.M + MOOD_BETA * (1.0 - s))
        self._mood_stepped = True

    @staticmethod
    def f_prime(window: deque) -> float:
        """F' in [0, 1]: the strict 6-turn decay window, normalized by
        the window ACTUALLY PRESENT, so one strong turn reads at full
        strength (the owner's immediacy rule — a single harsh message
        can clear the tiers on its own; the D2 0.73-decay still
        governs how a sustained window accumulates/decays). A full
        window of s=1.0 reads exactly 1.0 (D2 math, owner-pinned)."""
        items = list(window)
        if not items:
            return 0.0
        norm = sum(UPSET_DECAY ** i for i in range(len(items)))
        raw = sum(UPSET_DECAY ** i * s
                  for i, s in enumerate(reversed(items)))
        return _clip01(raw / norm)

    # -- event feeds (engine call sites; each fault-isolated upstream) ------ #
    def on_reasoning_seg(self, ev: dict, backend,
                         backend_name: str | None = None,
                         now: float | None = None) -> bool:
        """Buffer one reasoning segment for its turn; finalize the
        PREVIOUS turn when the turn key advances (D1: the accumulator
        ticks once per LLM response, not per stream chunk).

        Returns True ONLY when a turn was finalized (the F' window
        actually stepped) — the caller uses that to persist the cross-
        process mirror once per LLM response, not per stream chunk.
        False on a plain buffer append within the same turn."""
        ts = float(ev.get("ts") or now or _time.time())
        st = self._session(ev.get("session_id"), ts)
        self._evict_stale(ts)
        key = ev.get("turn_id")
        key = key if isinstance(key, str) and key else "local"
        text = ev.get("reasoning_text")
        last = st["last_turn"]
        stepped = False
        if last is not None and key != last:
            # Turn boundary: score the previous response now (its text
            # is complete), then start the new turn's buffer.
            self._step(st, self.score_reasoning(
                st["reasoning"].get(last) or "", backend, backend_name))
            st["reasoning"] = {}
            stepped = True
        if isinstance(text, str) and text:
            st["reasoning"][key] = (
                st["reasoning"].get(key, "") + " " + text)[:REASONING_CAP_CHARS]
        st["reasoning"].setdefault(key, "")
        st["last_turn"] = key
        return stepped

    def on_user_turn(self, ev: dict, good_reset: bool = False,
                     now: float | None = None) -> bool:
        """D5: a short GOOD reaction (the retained table job, decided by
        the ENGINE which owns the detector + the raw message this
        module never sees) hard-resets F' to 0 for the session — "no
        friction NOW". M is untouched (D6).

        Returns True when the session state CHANGED (a good-reset
        cleared the window) — the caller persists the cross-process
        mirror on that release; False otherwise (a plain turn marker)."""
        ts = float(ev.get("ts") or now or _time.time())
        st = self._session(ev.get("session_id"), ts)
        self._evict_stale(ts)
        if good_reset:
            st["window"].clear()
            st["upset_turns"] = 0
            return True
        return False

    def on_user_message(self, ev: dict, text: str, backend,
                       backend_name: str | None = None,
                       now: float | None = None) -> bool:
        """Immediacy feed (owner rule 2026-11-12): the user's OWN
        message is the fastest upset signal — score it NOW and step the
        window, instead of waiting for the next turn's reasoning
        narration to finalize the score. One harsh message can clear
        the tiers on its own (f_prime normalizes by the window present);
        a neutral message steps s~0 and simply decays the window.
        M (D6) integrates as on any step. The engine skips this feed
        on D5 good-resets (the window was just cleared).

        Returns True when the window stepped (text present) — the caller
        persists the cross-process mirror so the pane sees the friction
        the moment the user's harsh message lands; False on empty text."""
        if not isinstance(text, str) or not text.strip():
            return False
        ts = float(ev.get("ts") or now or _time.time())
        st = self._session(ev.get("session_id"), ts)
        self._evict_stale(ts)
        self._step(st, self.score_reasoning(text, backend, backend_name))
        return True

    def close_session(self, session_id: str | None,
                      conn=None) -> None:
        """D3/D6 close: finalize the session's last buffered turn (it is
        scored into F'/M), persist M (the cross-session hand-off row in
        the meta KV store when ``conn`` is given), and drop the
        session's F state. Fault-tolerant: a storage failure degrades
        to the in-memory-only close (D3 restart semantics)."""
        ts = _time.time()
        sid = str(session_id or "local")
        st = self._sessions.get(sid)
        if st is not None:
            text = st["reasoning"].get(st["last_turn"]) if st["last_turn"] else None
            if text and self._last_backend is not None:
                self._step(st, self.score_reasoning(
                    text, self._last_backend,
                    getattr(self._last_backend, "name", None)))
            self._sessions.pop(sid, None)
        if self._active == sid:
            self._active = None
        # D3 cross-process handoff: the session's F state is dropped here,
        # so the mirror is written CLEARED (empty window) — the pane's
        # friction settles while the global M hand-off persists.
        self.persist_state(conn, ts=ts, session_id=sid)

    def _close_session_impl(self, sid: str, persist: bool) -> None:
        """The lazy close on session switch (no storage here — the
        session_end event owns the persist, which carries the conn)."""
        st = self._sessions.pop(sid, None)
        if st is None:
            return
        text = st["reasoning"].get(st["last_turn"]) if st["last_turn"] else None
        if text and self._last_backend is not None:
            self._step(st, self.score_reasoning(
                text, self._last_backend,
                getattr(self._last_backend, "name", None)))
        if persist:
            self.persist_mood(None, ts=st.get("last_ts"), session_id=sid)

    # -- D6 persistence (injected conn: the meta KV store) ------------------ #
    @staticmethod
    def _bootstrap(conn) -> bool:
        """Make sure the meta KV table exists on ``conn`` (idempotent).
        False when the storage modules are unavailable (in-memory-only
        degradation, never an exception)."""
        try:
            try:
                from ..storage import migration as _mig
            except ImportError:
                from storage import migration as _mig
            _mig.schema_bootstrap(conn)
            return True
        except Exception:
            logger.exception("its-in-your-head: mood storage bootstrap failed")
            return False

    def hydrate_mood(self, conn) -> None:
        """One-time restore of M + the session history from the ``meta``
        KV store (fault-tolerant: any failure keeps the neutral prior
        and the in-memory history)."""
        if self._mood_hydrated or conn is None:
            return
        self._mood_hydrated = True
        if not self._bootstrap(conn):
            return
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                ("upset_mood",)).fetchone()
            if row and row[0]:
                self.M = _clip01(
                    float(json.loads(row[0]).get("value", MOOD_NEUTRAL)))
            hist = conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                ("upset_mood_history",)).fetchone()
            if hist and hist[0]:
                self._mood_history = list(json.loads(hist[0]) or [])[
                    -MOOD_HISTORY_MAX:]
        except Exception:
            logger.exception("its-in-your-head: mood hydrate read failed")

    def persist_mood(self, conn, ts: float | None = None,
                    session_id: str | None = None) -> None:
        """Write the session-level M hand-off (current value + bounded
        history) to the ``meta`` KV store. No-op when ``conn`` is None
        (in-memory-only degradation — the D3 restart path)."""
        if conn is None:
            return
        if not self._bootstrap(conn):
            return
        # Cross-process continuity: an unhydrated process must not
        # TRUNCATE the persisted history (or lose the persisted M) —
        # seed both from the stored rows first (single cheap reads; a
        # fault degrades to the in-memory state only).
        if not self._mood_hydrated:
            self._mood_hydrated = True
            try:
                hist = conn.execute(
                    "SELECT value FROM meta WHERE key = ?",
                    ("upset_mood_history",)).fetchone()
                if hist and hist[0]:
                    self._mood_history = list(json.loads(hist[0]) or [])[
                        -MOOD_HISTORY_MAX:]
                # M itself: only a never-stepped process (a fresh
                # restart) may take the persisted hand-off value — a
                # stepped M is this process's own integration, newer
                # than the stored row, and must not be clobbered.
                if not self._mood_stepped:
                    cur = conn.execute(
                        "SELECT value FROM meta WHERE key = ?",
                        ("upset_mood",)).fetchone()
                    if cur and cur[0]:
                        self.M = _clip01(
                            float(json.loads(cur[0])
                                  .get("value", self.M)))
            except Exception:
                logger.exception(
                    "its-in-your-head: mood persist seed read failed")
        entry = {"ts": ts if ts is not None else _time.time(),
                 "session_id": session_id,
                 "value": round(float(self.M), 6)}
        self._mood_history.append(entry)
        self._mood_history = self._mood_history[-MOOD_HISTORY_MAX:]
        try:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("upset_mood", json.dumps(
                    {"value": entry["value"], "ts": entry["ts"]}))
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("upset_mood_history", json.dumps(self._mood_history))
            )
            conn.commit()
        except Exception:
            logger.exception("its-in-your-head: mood persist write failed")

    # -- D3 cross-process handoff (the F' mirror) ---------------------------- #
    def persist_state(self, conn, ts: float | None = None,
                     session_id: str | None = None) -> None:
        """Mirror M + history + the active session's F' window to the
        ``meta`` KV store so the ISOLATED dashboard (which serves the
        pane's /pulse with no live engine) can surface the friction lane +
        glitch (the D3 cross-process handoff).

        Writes the same M/hand-off rows as :meth:`persist_mood`, PLUS the
        ``upset_fstate`` mirror: the session's scored window + upset-turn
        count + a timestamp. Only derived numbers leave this method —
        never raw text (C6). When ``session_id`` names a session already
        dropped (a close), the mirror is written CLEARED (empty window) so
        the pane's friction settles while M is retained. No-op when
        ``conn`` is None (in-memory-only degradation, the D3 restart
        path); any storage fault degrades to the mood-only write."""
        self.persist_mood(conn, ts=ts, session_id=session_id)
        if conn is None:
            return
        try:
            sid = str(session_id or self._active or "local")
            st = self._sessions.get(sid)
            window = [round(float(s), 6) for s in (st["window"] if st else [])]
            turns = int(st["upset_turns"]) if st else 0
            fstate = {
                "window": window,
                "upset_turns": turns,
                "ts": ts if ts is not None else _time.time(),
                "session_id": sid,
            }
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (FSTATE_KEY, json.dumps(fstate)))
            conn.commit()
        except Exception:
            logger.exception("its-in-your-head: fstate mirror write failed")

    @staticmethod
    def read_state(conn, now: float | None = None) -> dict | None:
        """The DASHBOARD-FALLBACK read path: reconstruct the ``upset``
        block from the persisted mirror + M + history (the isolated
        dashboard has no live engine, so this is how it sees F').

        TTL-gated: a fstate older than SESSION_TTL_S reads as SETTLED
        (value 0.0 / tier 0) while M + the prognosis are still surfaced —
        a stale mirror never shows stuck friction. A corrupt/missing
        mirror degrades to the mood-only block; any storage fault returns
        None (the caller serves the documented neutral shape)."""
        if conn is None:
            return None
        try:
            frow = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (FSTATE_KEY,)
            ).fetchone()
            mrow = conn.execute(
                "SELECT value FROM meta WHERE key = ?", ("upset_mood",)
            ).fetchone()
            hrow = conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                ("upset_mood_history",)).fetchone()
        except Exception:
            return None
        now_ts = float(now) if now is not None else _time.time()
        window: list[float] = []
        turns = 0
        if frow and frow[0]:
            try:
                fs = json.loads(frow[0])
                if now_ts - float(fs.get("ts") or 0.0) <= SESSION_TTL_S:
                    window = [float(s) for s in (fs.get("window") or [])]
                    turns = int(fs.get("upset_turns") or 0)
            except Exception:
                window, turns = [], 0
        mood = MOOD_NEUTRAL
        if mrow and mrow[0]:
            try:
                mood = _clip01(
                    float(json.loads(mrow[0]).get("value", MOOD_NEUTRAL)))
            except Exception:
                mood = MOOD_NEUTRAL
        history: list = []
        if hrow and hrow[0]:
            try:
                history = list(json.loads(hrow[0]) or [])[-MOOD_HISTORY_MAX:]
            except Exception:
                history = []
        value = (UpsetAccumulator.f_prime(deque(window))
                 if window else 0.0)
        return {
            "value": round(value, 4),
            "tier": _tier(value),
            "upset_turns": turns,
            "mood": round(mood, 4),
            "prognosis": prognosis_from_history(history),
        }

    # -- read-time prognosis (D6: the forecast, computed on demand) --------- #
    def prognosis(self) -> dict | None:
        """Linear trend over the last PROGNOSIS_HORIZON session-level M
        values (the read-time forecast — nothing is computed at write
        time). None until at least 2 values exist (honest absence)."""
        return prognosis_from_history(self._mood_history)

    # -- views ---------------------------------------------------------------- #
    def payload(self, session_id: str | None = None,
                now: float | None = None) -> dict:
        """The /pulse top-level ``upset`` shape: F'/tier for the session
        (the friction-lane user entry is built from this by
        pulse/matrix.py) + M + the read-time prognosis."""
        sid = str(session_id or "local")
        st = self._sessions.get(sid)
        if st is None and self._active in self._sessions:
            st = self._sessions[self._active]
        value = self.f_prime(st["window"]) if st is not None else 0.0
        return {
            "value": round(value, 4),
            "tier": _tier(value),
            "upset_turns": st["upset_turns"] if st is not None else 0,
            "mood": round(self.M, 4),
            "prognosis": self.prognosis(),
        }

    def view(self, session_id: str | None = None,
             now: float | None = None) -> dict:
        """Alias kept for the engine call site (pulse_view common dict)."""
        return self.payload(session_id, now)
