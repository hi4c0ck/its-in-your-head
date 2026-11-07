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
        # Pattern-perception state (third portrait view):
        # prompt vectors cached per turn for (prompt, reaction) pairing,
        # top-pattern tallies + last-evidence timestamps for confidence.
        # Vectors/numbers only — never raw text is stored here.
        self._prompt_vectors: dict = {}
        self._pattern_turns = 0
        self._pattern_top_counts: dict = {}
        self._pattern_last_evidence: dict = {}
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

    # -- pattern perception (third view, worker side only) ------------------ #
    def _cache_prompt_vector(self, turn_id, vector) -> None:
        """Cache one prompt vector keyed by turn (bounded; newest wins)."""
        if vector is None:
            return
        key = str(turn_id) if turn_id is not None else "__last__"
        self._prompt_vectors[key] = list(vector)
        self._prompt_vectors["__last__"] = list(vector)
        while len(self._prompt_vectors) > 34:  # 32 turns + __last__ slot
            oldest = next(iter(self._prompt_vectors))
            if oldest == "__last__":
                break
            self._prompt_vectors.pop(oldest, None)

    def _lookup_prompt_vector(self, turn_id):
        key = str(turn_id) if turn_id is not None else "__last__"
        found = self._prompt_vectors.get(key)
        if found is not None:
            return found
        return self._prompt_vectors.get("__last__")

    def _pattern_confidence(self, name, evidence_n, now) -> float:
        """confidence_from_evidence with plain age in seconds.

        n = the pattern's accumulated evidence count (portrait third view);
        agreement = fraction of scored turns in which this pattern was the
        top pattern (first-principles: a pattern that keeps winning the
        turn is more reliable than one that wins once); age = now minus
        the pattern's last-evidence timestamp, in plain seconds (never an
        epoch value, never the now_s argument). ``confidence_from_evidence``
        treats sub-1e8 values as a plain age, which is what we pass.
        """
        try:
            try:
                from .perception.patterns import confidence_from_evidence
            except ImportError:
                from perception.patterns import confidence_from_evidence
        except Exception:
            return 0.0
        turns = self._pattern_turns
        agreement = (self._pattern_top_counts.get(name, 0) / turns
                     if turns else 0.0)
        last = self._pattern_last_evidence.get(name)
        age = 0.0
        if last is not None:
            try:
                age = max(0.0, float(now) - float(last))
            except (TypeError, ValueError):
                age = 0.0
        return float(confidence_from_evidence(
            int(evidence_n or 0), agreement, age))

    def _update_patterns_and_persist(self, prompt_vector, reaction_vector,
                                     ts) -> None:
        """Score one turn against the pattern bank and persist derived rows.

        Fault-isolated: any failure degrades to no pattern evidence and
        never breaks the observed/interpreted tracks or the worker. Runs
        worker-side only (engine on_events), after the raw reasoning text
        has been encoded — the raw text itself is never stored here.
        """
        if prompt_vector is None:
            return
        try:
            try:
                from .perception import patterns as _patterns
            except ImportError:
                from perception import patterns as _patterns
        except Exception:
            return
        try:
            backend = self._get_backend()
            bank = _patterns.build_pattern_bank(
                backend, getattr(backend, "model_version", "unknown"))
            if not bank:
                return
            scored = _patterns.score_turn(
                prompt_vector, reaction_vector, bank)
            patterns = scored.get("patterns") or []
            if not patterns:
                return
            self.portrait.update_patterns(scored)
            now = float(ts) if ts is not None else time.time()
            self._pattern_turns += 1
            top_name = patterns[0].get("name")
            if top_name is not None:
                self._pattern_top_counts[top_name] = (
                    self._pattern_top_counts.get(top_name, 0) + 1)
            for entry in patterns:
                name = entry.get("name")
                if name is not None:
                    self._pattern_last_evidence[name] = now
            top = patterns[0]
            snap = self.portrait.snapshot()
            evidence_n = 0
            repeated = False
            for entry in snap.get("patterns") or []:
                if entry.get("name") == top_name:
                    evidence_n = int(entry.get("evidence") or 0)
                    repeated = bool(entry.get("repeated"))
                    break
            confidence = self._pattern_confidence(top_name, evidence_n, now)
            # Persist only paired rows (reaction present): prompt-only
            # scoring still updates the in-memory third view, but a
            # prompt-only row must never displace the durable paired
            # history read by the dashboard fallback (bridge contract:
            # an empty table means "no paired evidence yet").
            if reaction_vector is not None:
                self._persist_pattern_row(
                    top, now, evidence_n, confidence, repeated, set(bank),
                    _patterns)
        except Exception:
            logger.exception(
                "its-in-your-head: engine pattern scoring failed")

    def _persist_pattern_row(self, top_entry, ts, evidence_n, confidence,
                             repeated, bank_names, _patterns=None) -> None:
        """Persist ONE derived pattern_affinity_history row (D4 Option B).

        ``top_pattern`` is a preset-bank name taken from the sorted
        patterns list — never free text, never a prompt fragment. All
        other columns are derived floats/ints. ``None`` columns when the
        agent side is absent (prompt-only turn). Fault-isolated,
        log-and-never-raise, mirroring ``_persist_history_entry``.
        """
        try:
            name = top_entry.get("name")
            if name is None or name not in bank_names:
                return  # top_pattern accepts preset-bank names only

            def _num(v):
                return None if v is None else float(v)

            _db, _mig = self._storage()
            conn = _db.connect()
            try:
                _mig.schema_bootstrap(conn)
                conn.execute(
                    "INSERT INTO pattern_affinity_history "
                    "(ts, top_pattern, top_prompt_affinity, "
                    "top_agent_affinity, top_margin, top_amplification, "
                    "evidence, confidence, repeated) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (float(ts), name,
                     _num(top_entry.get("prompt_affinity")),
                     _num(top_entry.get("agent_affinity")),
                     _num(top_entry.get("margin")),
                     _num(top_entry.get("amplification")),
                     int(evidence_n or 0),
                     None if confidence is None else float(confidence),
                     1 if repeated else 0))
                _mig.prune_pattern_affinity_history(conn)
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logger.exception(
                "its-in-your-head: pattern_affinity_history persist failed")

    def _on_reasoning_seg(self, ev: dict) -> None:
        """Fold one reasoning segment into the interpreted portrait track.

        The raw reasoning text (available in memory at engine-feed time,
        before the metrics worker purges it) is encoded through the SAME
        configured backend used for prompts, so prompt and reaction share
        one vector space (tier is a quality switch, not a fork). The
        dim-mismatch guard below is only model-bump protection: same
        backend, so it should not trigger in normal operation. Scoring
        then feeds ``resemblance_all`` -> ``confidence_profile`` and
        ``Portrait.update_interpreted`` with per-dim (value, confidence)
        tuples, plus pattern scoring against the cached prompt vector.
        Any fault degrades to no-op: the worker and the observed track
        are never affected.
        """
        try:
            text = ev.get("reasoning_text")
            if not isinstance(text, str) or not text:
                return  # no raw reasoning text: no evidence, not an error
            try:
                from .perception.resemblance import (
                    confidence_profile, resemblance_all)
            except ImportError:
                from perception.resemblance import (
                    confidence_profile, resemblance_all)
            backend = self._get_backend()
            (vec,) = backend.encode([text])
            if not vec:
                return
            anchor_vectors = self._anchor_vectors_for(backend)
            probe = next(iter(anchor_vectors.values()), None)
            probe_vec = (probe.get("positive") or [None])[0] if isinstance(
                probe, dict) else None
            if probe_vec is None or len(probe_vec) != len(vec):
                return  # vector space mismatch: no evidence, not an error
            scores = resemblance_all(vec, anchor_vectors)
            conf = confidence_profile(scores, self.portrait.calibration)
            targets = {d: (conf[d], conf[d]) for d in _dims() if d in conf}
            if not targets:
                return
            self.portrait.update_interpreted(targets)
            self._interpreted_evidence_count += 1
            self._interpreted_last_ts = ev.get("ts")
            try:
                plain = [t[0] for t in targets.values()]
                self._interpreted_last_confidence = (
                    sum(plain) / len(plain))
            except (TypeError, ZeroDivisionError):
                pass
            prompt_vector = self._lookup_prompt_vector(ev.get("turn_id"))
            self._update_patterns_and_persist(
                prompt_vector, vec, ev.get("ts"))
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
            backend = self._get_backend()
            prompt_vector = None
            try:
                (prompt_vector,) = backend.encode([text])
            except Exception:
                logger.exception(
                    "its-in-your-head: prompt encode failed")
            if prompt_vector:
                self._cache_prompt_vector(ev.get("turn_id"), prompt_vector)
            if prompt_vector:
                try:
                    try:
                        from .perception.resemblance import resemblance_all
                    except ImportError:
                        from perception.resemblance import resemblance_all
                    # Single encode, shared with patterns below (no
                    # double-encoding of the same prompt).
                    anchor_vectors = self._anchor_vectors_for(backend)
                    self.portrait.update_observed(
                        resemblance_all(prompt_vector, anchor_vectors))
                except Exception:
                    self.portrait.update_observed_from_text(text, backend)
            else:
                self.portrait.update_observed_from_text(text, backend)
            self._update_patterns_and_persist(
                prompt_vector, None, ev.get("ts"))
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

    def _perception_view(self, dims, full) -> dict:
        """Additive ``perception`` key (C1 adjudication).

        Exact shape: ``patterns`` (list of objects with name,
        prompt_affinity, agent_affinity, margin, amplification, evidence,
        confidence, repeated), ``dim_signals`` (dict keyed by dimension
        name, each an object with prompt and agent), and
        ``perception_available`` (bool). Filled from the portrait third
        view snapshot and accumulated evidence/confidence. No evidence:
        patterns empty, dim_signals keeps every dimension key with None
        values, perception_available False. Never neutral defaults,
        never invented.
        """
        third = full.get("patterns")
        if third is None or not full.get("perception_available"):
            return {"patterns": [],
                    "dim_signals": {d: {"prompt": None, "agent": None}
                                    for d in dims},
                    "perception_available": False}
        now = time.time()
        patterns = []
        for entry in third:
            name = entry.get("name")
            patterns.append({
                "name": name,
                "prompt_affinity": entry.get("prompt_affinity"),
                "agent_affinity": entry.get("agent_affinity"),
                "margin": entry.get("margin"),
                "amplification": entry.get("amplification"),
                "evidence": int(entry.get("evidence") or 0),
                "confidence": self._pattern_confidence(
                    name, entry.get("evidence") or 0, now),
                "repeated": bool(entry.get("repeated")),
            })
        raw_signals = full.get("dim_signals") or {}
        dim_signals = {}
        for d in dims:
            sig = raw_signals.get(d) or {}
            dim_signals[d] = {"prompt": sig.get("prompt"),
                              "agent": sig.get("agent")}
        return {"patterns": patterns,
                "dim_signals": dim_signals,
                "perception_available": True}

    def portrait_view(self) -> dict:
        dims = _dims()
        snap = self.portrait.normalized()
        full = self.portrait.snapshot()
        interp_track = full.get("interpreted") or {}
        has_observed = bool(self.portrait.evidence)
        has_interpreted = bool(
            self._interpreted_evidence_count
            or (interp_track.get("evidence_count") or 0))
        perception = self._perception_view(dims, full)
        if not has_observed and not has_interpreted:
            # Empty-shape contract (S5): no evidence -> available False and
            # None values (structural defaults are NOT data).
            return {"available": False,
                    "dimensions": {d: None for d in dims},
                    "confidence": {d: None for d in dims},
                    "user": {d: None for d in dims},
                    "agent": {d: None for d in dims},
                    "perception": perception}
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
                "user": dict(values), "agent": agent,
                "perception": perception}

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
            self._prompt_vectors.clear()
            self._pattern_turns = 0
            self._pattern_top_counts.clear()
            self._pattern_last_evidence.clear()
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
