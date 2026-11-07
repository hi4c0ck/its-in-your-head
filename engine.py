"""Process-level engine wiring (parent, PG2 merge).

Bridges the committed lanes (S1 core pipeline, S2 encoder, S3 portrait,
S4 pulse) into live route data. Locked-rule compliance:

* Worker side: the metrics worker feeds :func:`on_events` after every
  batch persist — all embedding happens off the hot path.
* Hot path: :func:`on_tool_start` does registry-only work
  (``pulse.live_tick``: row create/increment, no backend, no encode()).
* Lazy: nothing here opens the DB or loads a model at import time;
  the backend is created on first use (``create_backend`` is
  register-time safe, no I/O).
* Routes: dashboard ``/pulse`` ``/portrait`` ``/drift`` read the
  zero-arg facades; ``POST /reset`` calls :func:`reset_engine` so the
  process view matches the wiped DB.

Import convention matches the rest of the plugin: namespaced in the
gateway, top-level under tests.
"""
from __future__ import annotations

import logging
import struct
import threading
import time
from collections import deque

try:  # hermes namespaced import (gateway) vs top-level import (tests)
    from . import pulse
    from .pulse import matrix as _pulse_matrix
    from .pulse import persistence as _pulse_persist
    from .perception import encoder as _encoder
    from .perception import anchors as _anchors
    from .perception import portrait as _portrait_mod
    from .core import settings as _settings
except ImportError:
    import pulse
    from pulse import matrix as _pulse_matrix
    from pulse import persistence as _pulse_persist
    from perception import encoder as _encoder
    from perception import anchors as _anchors
    from perception import portrait as _portrait_mod
    from core import settings as _settings

logger = logging.getLogger(__name__)

DRIFT_MAX = 512            # bounded in-memory portrait history rows
CAPSULES_PER_TOOL = 32    # recent encoded capsules kept for GOAL fallback


def _dims():
    return list(_anchors.DIMENSIONS)


class Engine:
    """Wires pulse registry/prototypes + portrait into one process view."""

    def __init__(self, settings: dict | None = None):
        s = dict(_settings.DEFAULTS)
        if isinstance(settings, dict):
            s.update({k: v for k, v in settings.items()
                      if k in _settings.DEFAULTS})
        self._lock = threading.Lock()
        self.registry = pulse.ToolRegistry()
        self.protos = pulse.PrototypeStore(
            cap=int(s.get("pulse.prototype_cap", 8)),
            min_labeled=int(s.get("pulse.min_labeled", 3)))
        self.portrait = _portrait_mod.Portrait(
            retain_days=int(s.get("observation.retain_days", 90)))
        self._capsules: dict[str, deque] = {}
        self._history: deque = deque(maxlen=DRIFT_MAX)
        # F2: persisted portrait_history is rehydrated lazily on first
        # portrait-history access (drift view / first user turn after
        # restart) — never at Engine construction/register time.
        self._history_loaded = False
        self.goal_text: str | None = None
        # Interpreted-track (agent/reasoning) evidence bookkeeping:
        # counts reasoning_seg updates that actually landed, so
        # /portrait can evidence-gate the agent track separately from
        # the observed (user) track. No raw text is stored here.
        self._interpreted_evidence_count = 0
        self._interpreted_last_ts = None
        self._interpreted_last_confidence = None
        self._anchor_vectors = None
        self._anchor_backend = None
        self._thresholds = {
            k: s.get(f"pulse.thresholds.{k}")
            for k in ("goal", "good", "friction")}
        self._backend = None
        self._backend_name = str(
            s.get("perception.encoder") or _encoder.DEFAULT_BACKEND)

    # -- backend (lazy) --------------------------------------------------- #
    def _get_backend(self):
        with self._lock:
            if self._backend is None:
                self._backend = _encoder.create_backend(
                    None, self._backend_name)
            return self._backend

    # -- hot path (pre_tool_call, sync, no inference) --------------------- #
    def on_tool_start(self, event: dict) -> dict:
        """Registry-only LIVE bump (``pulse.live_tick``). Never encodes."""
        return pulse.live_tick(self.registry, event)

    # -- worker side (off the hot path) ----------------------------------- #
    def on_events(self, batch) -> None:
        for ev in batch:
            if not isinstance(ev, dict):
                continue
            etype = ev.get("type")
            if etype == "tool_start":
                # PULSE LIVE registry bump (row create/increment, counters
                # only — no encoding; the C5 hot path enqueues these, and
                # the registry I/O stays off the token path).
                try:
                    pulse.handle_pre_tool_call(ev, self.registry)
                except Exception:
                    logger.exception(
                        "its-in-your-head: engine tool_start feed failed")
            elif etype == "tool_end":
                self._on_tool_end(ev)
            elif etype in ("user_turn", "turn"):
                self._on_user_turn(ev)
            elif etype == "reasoning_seg":
                self._on_reasoning_seg(ev)
        # Hot-path feedback labels (collector) mark the in-memory banks
        # dirty without any I/O; this worker pass persists them — the
        # isolated dashboard half sees the labels on its next /pulse read.
        self._persist_dirty_prototypes()

    def _persist_dirty_prototypes(self) -> list:
        """Persist every dirty tool's banks, then clear its dirty mark.

        Persist functions are fault-isolated (never raise), so marking
        clean is unconditional: a failed persist retries on the next
        change to that tool, never wedging the dirty set.
        """
        dirty = self.protos.dirty_tools()
        for tool in dirty:
            _pulse_persist.persist_tool_banks(self.protos, tool)
            self.protos.mark_clean(tool)
        return sorted(dirty)

    @staticmethod
    def _vector_from_event(ev: dict):
        """Extract a stored embedding/sketch vector from an event.

        Accepts ``vector``/``embedding`` (list of floats) or ``sketch``
        (Tier-0 packed float32 bytes, or a list of floats). Raw text is
        never re-embedded: no vector on the event means no evidence.
        """
        for key in ("vector", "embedding", "sketch", "sketch_vector"):
            raw = ev.get(key)
            if raw is None:
                continue
            if isinstance(raw, (bytes, bytearray)):
                if len(raw) % 4 or not len(raw):
                    return None
                return list(struct.unpack(
                    f"<{len(raw) // 4}f", bytes(raw)))
            if isinstance(raw, (list, tuple)) and raw:
                try:
                    return [float(x) for x in raw]
                except (TypeError, ValueError):
                    return None
            return None
        return None

    def _anchor_vectors_for(self, backend):
        """Cached anchor vectors for ``backend`` (built once per backend).

        Uses the same ``get_anchor_vectors`` accessor as the observed
        (user) track, so both tracks live in the same resemblance space.
        """
        if self._anchor_vectors is not None and \
                self._anchor_backend is backend:
            return self._anchor_vectors
        try:
            from .perception.anchors import get_anchor_vectors
        except ImportError:
            from perception.anchors import get_anchor_vectors
        vectors = get_anchor_vectors(backend)
        self._anchor_vectors = vectors
        self._anchor_backend = backend
        return vectors

    def _on_reasoning_seg(self, ev: dict) -> None:
        """Fold one reasoning segment into the interpreted portrait track.

        Projection (deterministic, no raw text, no re-embedding):
        stored sketch/vector -> ``resemblance_all`` vs the anchor bank
        (same six dimensions as the observed track) -> per-dimension
        ``R_d`` -> ``Calibration.confidence(R_d)`` (logistic, slope 6,
        midpoint 0, i.e. ``confidence_profile`` with the portrait's
        calibration) -> [0, 1] interpreted value per dimension ->
        ``Portrait.update_interpreted`` (its own EMA / MAX_STEP cap
        applies). Evidence bookkeeping is updated only when the update
        actually lands. Any fault degrades to no-op: the worker and the
        observed track are never affected.
        """
        try:
            vec = self._vector_from_event(ev)
            if not vec:
                return
            try:
                from .perception.resemblance import (
                    confidence_profile, resemblance_all)
            except ImportError:
                from perception.resemblance import (
                    confidence_profile, resemblance_all)
            backend = self._get_backend()
            anchor_vectors = self._anchor_vectors_for(backend)
            probe = next(iter(anchor_vectors.values()), None)
            probe_vec = (probe.get("positive") or [None])[0] \
                if isinstance(probe, dict) else None
            if probe_vec is None or len(probe_vec) != len(vec):
                return  # vector space mismatch: no evidence, not an error
            scores = resemblance_all(vec, anchor_vectors)
            conf = confidence_profile(scores, self.portrait.calibration)
            targets = {d: conf[d] for d in _dims() if d in conf}
            if not targets:
                return
            self.portrait.update_interpreted(targets)
            self._interpreted_evidence_count += 1
            self._interpreted_last_ts = ev.get("ts")
            try:
                self._interpreted_last_confidence = (
                    sum(targets.values()) / len(targets))
            except (TypeError, ZeroDivisionError):
                pass
        except Exception:
            logger.exception(
                "its-in-your-head: engine reasoning_seg feed failed")

    def _on_tool_end(self, ev: dict) -> None:
        try:
            capsule = pulse.handle_post_tool_call(
                ev, self.registry, backend=self._get_backend(), args=None)
            tool = str(ev.get("tool_name") or "unknown")
            self._store_capsule(tool, capsule)
            label = None
            if isinstance(capsule, dict):
                label = self.protos.record_tool_end(capsule)
            # Cross-process bridge (worker side, fault-isolated): persist
            # the bounded derived-vector state the isolated dashboard
            # half reads. Derived vectors only — the capsule text payload
            # and any raw prompt never reach the DB.
            if isinstance(capsule, dict) and capsule.get("vector") is not None:
                _pulse_persist.persist_capsule(capsule, CAPSULES_PER_TOOL)
            if label:
                _pulse_persist.persist_tool_banks(self.protos, tool)
        except Exception:
            logger.exception("its-in-your-head: engine tool_end feed failed")

    def _on_user_turn(self, ev: dict) -> None:
        text = ev.get("user_message") or ""
        if not isinstance(text, str) or not text:
            return
        try:
            self.goal_text = text
            # PULSE bridge: persist the DERIVED goal vector (the embedding
            # of this prompt). The raw text stays in memory only (C6 /
            # v0.1.3): the goal row stores the vector, never the prompt.
            try:
                goal_vector = pulse.activity_encoder.encode_goal_text(
                    text, self._get_backend())
                _pulse_persist.persist_goal_vector(
                    goal_vector, ts=ev.get("ts"),
                    backend=self._backend_name)
            except Exception:
                logger.exception(
                    "its-in-your-head: goal vector persist failed")
            self.portrait.update_observed_from_text(text, self._get_backend())
            norm = self.portrait.normalized()
            ts = ev.get("ts")
            entry = {
                "ts": float(ts) if ts is not None else time.time(),
                "values": {
                    d: float(v)
                    for d, v in dict(norm.get("values") or {}).items()
                    if v is not None
                },
            }
            # F2 writer+rehydrate in one connection (single bootstrap):
            # on a post-restart first turn the persisted history loads
            # first, then this entry appends after it in both views.
            # Fault-isolated: a persist failure must not break perception;
            # the engine degrades to in-memory-only history.
            try:
                self._append_history(entry)
            except Exception:
                logger.exception(
                    "its-in-your-head: portrait_history persist failed")
        except Exception:
            logger.exception("its-in-your-head: engine user_turn feed failed")

    def _store_capsule(self, tool, capsule) -> None:
        if not isinstance(capsule, dict) or capsule.get("vector") is None:
            return
        key = str(tool or "unknown")
        with self._lock:
            dq = self._capsules.setdefault(
                key, deque(maxlen=CAPSULES_PER_TOOL))
            dq.append(capsule)

    # -- F2 portrait_history durability ------------------------------------ #
    @staticmethod
    def _storage():
        """(database, migration) modules, imported lazily.

        No DB handle is opened at import/register time; the caller opens
        one per call (same pattern as metrics._persist_batch).
        """
        try:  # namespaced gateway vs top-level (tests)
            from .storage import database as _db
            from .storage import migration as _mig
        except ImportError:
            from storage import database as _db
            from storage import migration as _mig
        return _db, _mig

    @staticmethod
    def _load_rows(conn) -> list:
        """All portrait_history entries as {ts, values}, probe order."""
        dims = _dims()
        cols = ", ".join(dims)
        rows = conn.execute(
            f"SELECT ts, {cols} FROM portrait_history "
            f"ORDER BY ts, id").fetchall()
        return [{"ts": row[0],
                 "values": {d: float(v) for d, v in zip(dims, row[1:])
                            if v is not None}} for row in rows]

    def _append_history(self, entry: dict) -> None:
        """Rehydrate (if first access), append locally, then persist.

        The local deque append is unconditional (perception must never
        depend on storage); the persist is one write (insert + retention
        prune). A persist failure raises — the caller (_on_user_turn)
        catches and degrades to in-memory-only history.
        """
        self._ensure_history()  # never raises (degrades to empty/current)
        self._history.append(entry)
        self._persist_history_entry(entry)

    def _ensure_history(self) -> None:
        """Lazy rehydrate of ``_history`` from ``portrait_history``.

        Runs once per process (flag-guarded), on first portrait-history
        access — never at register/Engine construction. Rows load in
        ``ts, id`` order (the probe's ordering); the deque's ``maxlen``
        keeps only the most recent ``DRIFT_MAX`` entries. Any failure
        degrades to the current (possibly empty) in-memory history.
        """
        if self._history_loaded:
            return
        self._history_loaded = True  # guard first: no retry loops
        try:
            _db, _mig = self._storage()
            conn = _db.connect()
            try:
                _mig.schema_bootstrap(conn)
                entries = self._load_rows(conn)
            finally:
                conn.close()
            # Deque bound: keep only the most recent DRIFT_MAX, in order.
            self._history.extend(entries[-DRIFT_MAX:])
        except Exception:
            logger.exception(
                "its-in-your-head: portrait_history rehydrate failed")

    def _persist_history_entry(self, entry: dict) -> None:
        """Persist one portrait-history entry (one write per entry).

        Retention: after the insert, rows beyond
        ``migration.PORTRAIT_HISTORY_MAX`` (512, mirroring ``DRIFT_MAX``)
        are pruned, keeping the most recent by ``id``. Caller wraps this
        in try/except — a persist failure must not break perception.
        """
        _db, _mig = self._storage()
        dims = _dims()
        vals = entry.get("values") or {}
        row = [float(entry["ts"])] + [
            float(vals[d]) if vals.get(d) is not None else None
            for d in dims
        ]
        conn = _db.connect()
        try:
            _mig.schema_bootstrap(conn)
            conn.execute(
                "INSERT INTO portrait_history (ts, "
                + ", ".join(dims) + ") VALUES ("
                + ", ".join(["?"] * (len(dims) + 1)) + ")",
                row)
            conn.execute(
                "DELETE FROM portrait_history WHERE id NOT IN "
                "(SELECT id FROM portrait_history ORDER BY id DESC LIMIT ?)",
                (_mig.PORTRAIT_HISTORY_MAX,))
            conn.commit()
        finally:
            conn.close()

    # -- zero-arg route facades -------------------------------------------- #
    def _session_state(self):
        try:
            try:
                from .core.collector import STATE
            except ImportError:
                from core.collector import STATE
            return STATE
        except Exception:
            return None

    def capsule_state(self) -> dict:
        """Per-tool activity-capsule vectors (S7 seam goal evidence)."""
        with self._lock:
            return {t: list(dq) for t, dq in self._capsules.items()}

    def pulse_view(self) -> dict:
        thr = {k: v for k, v in self._thresholds.items() if v is not None}
        capsules = None
        with self._lock:
            if self._capsules:
                capsules = {t: list(dq) for t, dq in self._capsules.items()}
        rows = _pulse_matrix.build_matrix(
            self.registry, self.protos,
            session_state=self._session_state(),
            goal_text=self.goal_text,
            backend=self._get_backend(),
            capsules_by_tool=capsules,
            thresholds=thr or None)
        return {"rows": list(rows),
                "columns": ["goal", "good", "friction", "unlabeled"],
                "available": bool(rows)}

    def portrait_view(self) -> dict:
        dims = _dims()
        snap = self.portrait.normalized()
        full = self.portrait.snapshot()
        interp_track = full.get("interpreted") or {}
        has_observed = bool(self.portrait.evidence)
        has_interpreted = bool(
            self._interpreted_evidence_count
            or (interp_track.get("evidence_count") or 0))
        if not has_observed and not has_interpreted:
            # Empty-shape contract (S5): no evidence -> available False and
            # None values (structural defaults are NOT data).
            return {"available": False,
                    "dimensions": {d: None for d in dims},
                    "confidence": {d: None for d in dims},
                    "user": {d: None for d in dims},
                    "agent": {d: None for d in dims}}
        interp = interp_track.get("values") or {}
        values = ({d: snap.get("values", {}).get(d) for d in dims}
                  if has_observed else {d: None for d in dims})
        conf = ({d: snap.get("confidences", {}).get(d) for d in dims}
                if has_observed else {d: None for d in dims})
        # Agent (interpreted) track: values only when reasoning
        # evidence exists; otherwise None, never the neutral defaults.
        agent = ({d: interp.get(d) for d in dims} if has_interpreted
                 else {d: None for d in dims})
        return {"available": True,
                "dimensions": values, "confidence": conf,
                "user": dict(values), "agent": agent}

    def drift_view(self) -> dict:
        # Lazy rehydrate: first drift access after a plugin-process
        # restart loads the persisted portrait_history into the deque
        # (in ts/id order, capped by the deque bound) before serving.
        self._ensure_history()
        dims = _dims()
        series: dict[str, list] = {d: [] for d in dims}
        for entry in self._history:
            vals = entry.get("values") or {}
            for d in dims:
                if d in vals and vals[d] is not None:
                    series[d].append(float(vals[d]))
        return {"series": series, "available": bool(self._history)}

    # -- explicit wipe (POST /reset) ---------------------------------------- #
    def reset(self, settings: dict | None = None) -> None:
        merged = dict(_settings.DEFAULTS)
        if isinstance(settings, dict):
            merged.update({k: v for k, v in settings.items()
                           if k in _settings.DEFAULTS})
        with self._lock:
            self._capsules.clear()
            self._history.clear()
            # Explicit wipe: never resurrect pre-wipe rows from the DB.
            # (The route's _reset_dataset() deletes portrait_history;
            # marking loaded keeps this process view equal to that wipe.)
            self._history_loaded = True
            self.goal_text = None
            self._interpreted_evidence_count = 0
            self._interpreted_last_ts = None
            self._interpreted_last_confidence = None
            self.protos = pulse.PrototypeStore(
                cap=int(merged.get("pulse.prototype_cap", 8)),
                min_labeled=int(merged.get("pulse.min_labeled", 3)))
            self.portrait = _portrait_mod.Portrait(
                retain_days=int(merged.get("observation.retain_days", 90)))
        # Registry rows live in the DB; the route's _reset_dataset() wipes
        # those — the registry re-reads lazily on the next observe.


# -- process singleton ------------------------------------------------------
_engine: Engine | None = None
_engine_lock = threading.Lock()


def get_engine() -> Engine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = Engine()
        return _engine


def on_events(batch) -> None:
    get_engine().on_events(batch)


def on_tool_start(event: dict) -> dict:
    return get_engine().on_tool_start(event)


def reset_engine(settings: dict | None = None) -> None:
    get_engine().reset(settings)
