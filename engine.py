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
    from .pulse import velocity as _pulse_velocity
    from .perception import encoder as _encoder
    from .perception import anchors as _anchors
    from .perception import portrait as _portrait_mod
    from .perception import patterns as _patterns
    from .core import settings as _settings
except ImportError:
    import pulse
    from pulse import matrix as _pulse_matrix
    from pulse import persistence as _pulse_persist
    from pulse import velocity as _pulse_velocity
    from perception import encoder as _encoder
    from perception import anchors as _anchors
    from perception import portrait as _portrait_mod
    from perception import patterns as _patterns
    from core import settings as _settings

logger = logging.getLogger(__name__)

# Live-expression / morphology neutral literals (module constants so the
# fault paths never depend on a live import): identical shapes to
# _patterns.neutral_live_expression() / neutral_morphology().
NEUTRAL_LIVE_EXPRESSION: dict = {
    "openness": 0.0, "focus": 0.0, "resonance": 0.5, "tension": 0.5,
    "intensity": 0.0, "confidence": 0.0, "stage": "none",
    "updated_at": None,
}
NEUTRAL_MORPHOLOGY: dict = {
    "pair": None, "strength": 0.0, "confidence": 0.0, "evidence": 0,
    "available": False, "line_density": 0.0, "contour_asymmetry": 0.0,
    "echo_strength": 0.0, "gesture_bias": 0.0,
}

DRIFT_MAX = 512            # bounded in-memory portrait history rows
CAPSULES_PER_TOOL = 32    # recent encoded capsules kept for GOAL fallback


def _reinforce_allowed(settings, amplification) -> bool:
    """Reinforcement gate (D3 route A): enabled AND amplification is a
    number at least ``perception.reinforce_min_amplification``.
    Pure helper so the threshold semantics are pinned in tests.
    """
    if not (settings or {}).get("perception.reinforce", True):
        return False
    if amplification is None:
        return False
    try:
        threshold = float(
            (settings or {}).get(
                "perception.reinforce_min_amplification", 0.15))
        return float(amplification) >= threshold
    except (TypeError, ValueError):
        return False


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
            min_labeled=int(s.get("pulse.min_labeled", 3)),
            goal_min_references=s.get("pulse.goal_min_references"),
            good_min_samples=s.get("pulse.good_min_samples"),
            user_friction_min_samples=s.get(
                "pulse.user_friction_min_samples"),
            objective_friction_min_samples=s.get(
                "pulse.objective_friction_min_samples"))
        self.portrait = _portrait_mod.Portrait(
            retain_days=int(s.get("observation.retain_days", 90)))
        self._capsules: dict[str, deque] = {}
        self._history: deque = deque(maxlen=DRIFT_MAX)
        # F2: persisted portrait_history is rehydrated lazily on first
        # portrait-history access (drift view / first user turn after
        # restart) — never at Engine construction/register time.
        self._history_loaded = False
        self.goal_text: str | None = None
        # Hydrated goal vector + metadata (derived vector only, never raw
        # text). Set from _on_user_turn or from hydration; pulse_view
        # prefers it over re-encoding goal_text so the restored view
        # matches the DB-fallback view.
        self._goal_vector = None
        self._goal_ts = None
        self._goal_backend = None
        # PULSE hydration (Stage 1): lazy one-time restore from the
        # active profile's DB. _pulse_hydrated guards duplicate loads;
        # _pulse_hydration_lock serializes concurrent first reads.
        self._pulse_hydrated = False
        self._pulse_hydration_lock = threading.Lock()
        # v0.1.10 (review fix 1): monotonic deadline of the next
        # hydration retry after a FAILED load (bounded backoff — a
        # broken DB read must not be hammered on every /pulse poll).
        self._pulse_retry_at = 0.0
        # Interpreted-track (agent/reasoning) evidence bookkeeping:
        # counts reasoning_seg updates that actually landed, so
        # /portrait can evidence-gate the agent track separately from
        # the observed (user) track. No raw text is stored here.
        self._interpreted_evidence_count = 0
        self._interpreted_last_ts = None
        self._interpreted_last_confidence = None
        self._anchor_vectors = None
        self._anchor_backend = None
        # Rolling reference (dynamic reassembly): lazily created + seeded from
        # the bank on the first backend-available turn, then reassembled by
        # live observed turns. Never created at register time. ood_drift is
        # the last turn's drift from the reference (0 = in-territory).
        self.reference = None
        self._ood_drift = 0.0
        self._reference_loaded = False
        # Pattern-perception state (third portrait view):
        # prompt vectors cached per turn for (prompt, reaction) pairing,
        # top-pattern tallies + last-evidence timestamps for confidence.
        # Vectors/numbers only — never raw text is stored here.
        self._prompt_vectors: dict = {}
        self._pattern_turns = 0
        self._pattern_top_counts: dict = {}
        self._pattern_last_evidence: dict = {}
        # Transient live-expression state (process-only, never persisted):
        # Live expression (transient portrait gesture controls): target +
        # display base + timestamps ONLY. Zero persisted state, no timers,
        # no animation worker — a process restart returns to the neutral
        # resting state; the settle/hold/release arithmetic runs on READ
        # (portrait_view), driven by target_ts/now timestamps.
        self._live_expr_target: dict | None = None
        self._live_expr_ts: float | None = None
        # The display snapshot captured when the target was set: the next
        # settle starts from the current display value (continuity rule).
        self._live_expr_base: dict | None = None
        # Morphology: lazily derived from the persisted
        # pattern_affinity_history (reconstructs after restart) and cached
        # by the DB row signature so polls don't re-scan 512 rows.
        self._morph_cache: dict | None = None
        self._morph_cache_key: tuple | None = None
        # Reinforced-bank dirty set (L10, L5 open item (a)): in-memory mirror
        # of the persisted pattern_bank rows, loaded once at bootstrap.
        # _reinforced_cache: {name: {prompt_anchor, agent_anchor, evidence,
        # updated_at}} (never raw text). _reinforced_dirty: names this
        # process reinforced since bootstrap; membership never triggers a
        # DB re-read within this process. _reinforced_loaded: bootstrap done.
        self._reinforced_cache: dict = {}
        self._reinforced_dirty: set = set()
        self._reinforced_loaded = False
        self._thresholds = {
            k: s.get(f"pulse.thresholds.{k}")
            for k in ("goal", "good", "friction")}
        self._backend = None
        self._backend_name = str(
            s.get("perception.encoder") or _encoder.DEFAULT_BACKEND)
        self._settings = dict(s)
        # Live-settings provider (wired by the register hook): a callable
        # -> {key: value} that re-reads the PERSISTED settings, so a live
        # encoder switch (perception.encoder) re-resolves the backend with
        # no restart and no state wipe.
        self._settings_provider = None

    # -- backend (lazy) --------------------------------------------------- #
    def set_settings_provider(self, provider) -> None:
        """Store a live-settings callable (``-> {key: value}``). When set,
        ``_get_backend`` re-reads ``perception.encoder`` on each call and
        re-resolves the backend on a live switch — no state wipe, no
        restart. ``None`` / a failing provider degrades to the startup
        backend name (never crashes the encode path)."""
        self._settings_provider = provider

    def _get_backend(self):
        with self._lock:
            name = self._backend_name
            if self._settings_provider is not None:
                try:
                    live = self._settings_provider() or {}
                    name = str(live.get("perception.encoder") or name)
                except Exception:
                    name = self._backend_name
            if name != self._backend_name or self._backend is None:
                self._backend_name = name
                self._backend = _encoder.create_backend(None, name)
            return self._backend

    # -- PULSE lazy hydration (Stage 1) ------------------------------------ #
    PULSE_HYDRATION_RETRY_SECONDS = 30.0

    def ensure_pulse_hydrated(self) -> None:
        """Lazy ONE-TIME hydration of PULSE learning state from the
        active profile's DB: prototype banks (good / friction /
        objective_friction), recent capsules, latest goal vector +
        metadata. Public ensure/restore entry point invoked by both
        pulse_view and the collector feedback-association path before
        their work (feedback may arrive before the first dashboard
        read). Concurrency-guarded: concurrent first calls load once.
        Explicit reset sets _pulse_hydrated=True so post-reset state
        stays empty. Never raises; never called from on_tool_start
        (sync tool-start hot path stays I/O-free).

        v0.1.10 (review fix 1): the hydrated flag is set ONLY after a
        successful load AND merge; a failed database read leaves the
        engine un-hydrated and arms a bounded retry backoff instead of
        permanently marking an empty engine as hydrated. Load success
        is reported by persistence (a failed read no longer masquerades
        as an empty database).
        """
        if self._pulse_hydrated:
            return
        with self._pulse_hydration_lock:
            if self._pulse_hydrated:
                return
            now = time.monotonic()
            if now < self._pulse_retry_at:
                return  # inside the backoff window after a failed load
            state, ok = _pulse_persist.load_pulse_state_checked()
            if not ok:
                # The database read itself failed: stay UN-hydrated so a
                # later call retries, with a bounded backoff.
                self._pulse_retry_at = (
                    now + self.PULSE_HYDRATION_RETRY_SECONDS)
                return
            try:
                # Banks: merge, skipping vectors already present (a
                # tool_end persisted before hydration must not
                # duplicate). v0.1.10: preserve the persisted
                # vector-space provenance (bank_meta) so the
                # compatibility gate can tell encoder spaces apart.
                for tool, banks in (state.get("banks") or {}).items():
                    meta = (state.get("banks_meta") or {}).get(tool) or {}
                    existing = self.protos.bank(tool)
                    for label, vecs in banks.items():
                        label_meta = meta.get(label) or {}
                        provenance = None
                        if (label_meta.get("backend") is not None
                                or label_meta.get("model_version")
                                is not None):
                            provenance = (label_meta.get("backend"),
                                          label_meta.get("model_version"))
                        for vec in vecs or []:
                            if vec not in existing.get(label, []):
                                self.protos.add(tool, vec, label,
                                                provenance=provenance)
                    self.protos.mark_clean(tool)
                # Capsules: merge, skipping (tool, ts, vector) duplicates.
                # v0.1.10: keep the persisted provenance tags.
                with self._lock:
                    for tool, caps in (state.get("capsules") or {}).items():
                        dq = self._capsules.setdefault(
                            str(tool), deque(maxlen=CAPSULES_PER_TOOL))
                        seen = {(c.get("ts"), tuple(c.get("vector") or ()))
                                for c in dq if isinstance(c, dict)}
                        for cap in caps or []:
                            vec = cap.get("vector")
                            if vec is None:
                                continue
                            key = (cap.get("ts"), tuple(vec))
                            if key in seen:
                                continue
                            seen.add(key)
                            dq.append({"vector": list(vec),
                                       "ts": cap.get("ts"),
                                       "tool_name": str(tool),
                                       "backend": cap.get("backend"),
                                       "model_version":
                                           cap.get("model_version")})
                if self._goal_vector is None and \
                        state.get("goal_vector") is not None:
                    self._goal_vector = list(state["goal_vector"])
                    self._goal_ts = state.get("goal_ts")
                    self._goal_backend = state.get("goal_backend")
            except Exception:
                logger.exception(
                    "its-in-your-head: pulse hydration merge failed")
                # Merge fault: keep the engine un-hydrated so the next
                # poll retries (bounded backoff), same as a load fault.
                self._pulse_retry_at = (
                    now + self.PULSE_HYDRATION_RETRY_SECONDS)
                return
            self._pulse_hydrated = True

    def ensure_restored(self) -> None:
        """Alias of ensure_pulse_hydrated (public restore entry)."""
        self.ensure_pulse_hydrated()

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

        v0.1.10 (review fix 3): persist the active encoder's
        vector-space identity with the bank rows, so a later encoder
        switch cannot score new vectors against an old space.
        """
        dirty = self.protos.dirty_tools()
        space = self._pulse_vector_space()
        for tool in dirty:
            _pulse_persist.persist_tool_banks(
                self.protos, tool,
                backend=space["id"],
                model_version=space["model_version"])
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

    def _ensure_reference(self, backend, anchor_vectors):
        """Lazily materialize the rolling reference.

        L-MIGRATE: on the first call of a process, restore a PERSISTED rolling
        reference from the DB (so the reassembled window survives a restart)
        before falling back to the seed bank (R4 cold start). Seeding/restoring
        needs a backend + the anchor vectors, so it happens on the first
        available turn — never at register time. ``backend`` is retained for a
        future encoder switch."""
        if self.reference is not None:
            return self.reference
        try:
            from .perception.reference import RollingReference
        except ImportError:
            from perception.reference import RollingReference
        if not self._reference_loaded:
            self._reference_loaded = True
            persisted = self._load_reference_state()
            if persisted:
                self.reference = RollingReference(state=persisted)
                return self.reference
        # Cold start: seed the window from the bank's per-dim positives.
        seed = {dim: list(
            (anchor_vectors.get(dim) or {}).get("positive") or [])
            for dim in _dims()}
        self.reference = RollingReference(seed_bank=seed)
        return self.reference

    def _load_reference_state(self) -> dict | None:
        """Restore the persisted rolling-reference state from the DB
        (L-MIGRATE, fault-isolated: None on any failure -> seed-bank cold
        start). Worker-side; the meta row is tiny."""
        try:
            _db, _mig = self._storage()
            conn = _db.connect()
            try:
                _mig.schema_bootstrap(conn)
                try:
                    from .perception import reference as _ref
                except ImportError:
                    from perception import reference as _ref
                return _ref.load_state(conn)
            finally:
                conn.close()
        except Exception:
            logger.exception(
                "its-in-your-head: reference state load failed")
            return None

    def _persist_reference_state(self) -> None:
        """Persist the rolling-reference state to the DB so the reassembled
        window survives a restart (L-MIGRATE). Fault-isolated: a failure must
        never break the live turn (the reference stays in-memory)."""
        if self.reference is None:
            return
        try:
            _db, _mig = self._storage()
            conn = _db.connect()
            try:
                _mig.schema_bootstrap(conn)
                try:
                    from .perception import reference as _ref
                except ImportError:
                    from perception import reference as _ref
                _ref.save_state(conn, self.reference.to_state())
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logger.exception(
                "its-in-your-head: reference state persist failed")

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

    def _pattern_confidence(self, name, evidence_n, now, margin=None) -> float:
        """confidence_from_evidence with plain age in seconds.

        n = the pattern's accumulated evidence count (portrait third view);
        agreement = fraction of scored turns in which this pattern was the
        top pattern (first-principles: a pattern that keeps winning the
        turn is more reliable than one that wins once); age = now minus
        the pattern's last-evidence timestamp, in plain seconds (never an
        epoch value, never the now_s argument). ``confidence_from_evidence``
        treats sub-1e8 values as a plain age, which is what we pass.

        ``margin`` (L11): the entry's top-margin. When given, the
        calibrated (margin-gated) branch of ``confidence_from_evidence``
        runs; when None (legacy call sites / no margin available) the
        legacy output is reproduced exactly.
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
            int(evidence_n or 0), agreement, age, margin=margin))

    def _load_reinforced_bank(self, _patterns) -> dict:
        """Reinforced anchors from the DB (D3 route A). Fault-isolated:
        any failure degrades to {} (preset-only bank), never breaks
        scoring. Worker-side only; the table is tiny.

        Called once at bootstrap (:meth:`_reinforced_bank`); scored turns
        use the in-memory cache, never this method.
        """
        try:
            _db, _mig = self._storage()
            conn = _db.connect()
            try:
                _mig.schema_bootstrap(conn)
                return _patterns.load_reinforced(conn)
            finally:
                conn.close()
        except Exception:
            logger.exception(
                "its-in-your-head: reinforced bank load failed")
            return {}

    def _reinforced_bank(self, _patterns) -> dict:
        """In-memory reinforced bank with dirty-set discipline (L10).

        Bootstrap (first scored turn of the process): load once from the
        DB via :meth:`_load_reinforced_bank` (fault-isolated, {} on
        failure). Every scored turn afterwards returns the in-memory
        cache — no per-turn DB read, even for names this process
        reinforced (``_reinforced_dirty`` never triggers a re-read).

        Cross-process semantics (accepted staleness): a reinforcement
        persisted by ANOTHER process is picked up only at the next
        bootstrap — i.e. a fresh Engine (next process start) or an
        explicit :meth:`reset` — never mid-process. This process's own
        reinforcements are applied to the cache directly at persist
        time (see :meth:`_maybe_reinforce_and_persist`), so the next
        turn scores against them without any DB round-trip.
        """
        if self._reinforced_loaded:
            return self._reinforced_cache
        self._reinforced_loaded = True  # guard first: no retry loops
        self._reinforced_cache = self._load_reinforced_bank(_patterns)
        return self._reinforced_cache

    def _maybe_reinforce_and_persist(self, bank, top_entry,
                                     reaction_vector, _patterns) -> None:
        """Reinforce the top pattern with the reaction vector and persist
        the reinforced entry (D3 route A). Worker-side only, after a
        PAIRED scoring turn; fault-isolated like the
        pattern_affinity_history writer — a failure must not touch the
        live pipeline. Gate: settings ``perception.reinforce`` and the
        top pattern's amplification >= the configured minimum.

        In-memory mirror (L10): after the persist, the fresh entry is
        written straight into ``_reinforced_cache`` and the name is
        marked in ``_reinforced_dirty`` — the next scored turn sees the
        fresh vectors without any DB read. The dirty mark never causes
        a DB re-read in this process (see :meth:`_reinforced_bank`).
        ``updated_at`` mirrors the persist's monotonic rule (wall clock,
        bumped past the prior value when needed) so the cache matches
        what a per-turn DB read would have supplied.
        """
        try:
            if reaction_vector is None:
                return
            amplification = (top_entry or {}).get("amplification")
            if not _reinforce_allowed(self._settings, amplification):
                return
            name = (top_entry or {}).get("name")
            if not name or name not in bank:
                return
            reinforced_entry = _patterns.reinforce(
                bank[name], reaction_vector)
            _db, _mig = self._storage()
            conn = _db.connect()
            try:
                _mig.schema_bootstrap(conn)
                _patterns.persist_reinforced(
                    conn, {name: reinforced_entry})
            finally:
                conn.close()
            now = time.time()
            prior = self._reinforced_cache.get(name) or {}
            prior_updated = prior.get("updated_at")
            if prior_updated is not None and now <= prior_updated:
                now = prior_updated + 0.001
            self._reinforced_cache[name] = {
                "prompt_anchor": list(reinforced_entry["prompt_anchor"]),
                "agent_anchor": list(reinforced_entry["agent_anchor"]),
                "evidence": int(reinforced_entry.get("evidence") or 0),
                "updated_at": now,
            }
            self._reinforced_dirty.add(name)
        except Exception:
            logger.exception(
                "its-in-your-head: pattern reinforce persist failed")

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
            reinforced = self._reinforced_bank(_patterns)
            bank = _patterns.build_pattern_bank(
                backend, getattr(backend, "model_version", "unknown"),
                custom_patterns=self._settings.get(
                    "perception.custom_patterns"),
                reinforced=reinforced)
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
            # Live expression (transient): prompt arrives -> stage 1
            # (prompt-only affinities); reasoning folded -> stage 2
            # (paired affinities refine resonance/tension). Timestamps
            # only — nothing here persists.
            self._set_live_expression(
                patterns, paired=reaction_vector is not None, ts=now)
            top = patterns[0]
            snap = self.portrait.snapshot()
            evidence_by_name = {}
            for entry in snap.get("patterns") or []:
                nm = entry.get("name")
                if nm is not None:
                    evidence_by_name[nm] = int(entry.get("evidence") or 0)
            repeated_top = any(
                e.get("name") == top_name and e.get("repeated")
                for e in snap.get("patterns") or [])
            # Persist only paired rows (reaction present): prompt-only
            # scoring still updates the in-memory third view, but a
            # prompt-only row must never displace the durable paired
            # history read by the dashboard fallback (bridge contract:
            # an empty table means "no paired evidence yet").
            if reaction_vector is not None:
                self._persist_pattern_rows(
                    patterns, now, evidence_by_name, repeated_top,
                    set(bank), _patterns)
                self._maybe_reinforce_and_persist(
                    bank, top, reaction_vector, _patterns)
        except Exception:
            logger.exception(
                "its-in-your-head: engine pattern scoring failed")

    def _persist_pattern_rows(self, entries, ts, evidence_by_name,
                              repeated_top, bank_names,
                              _patterns=None) -> None:
        """Persist up to THREE derived pattern_affinity_history rows for
        one turn (E3, schema v7; D4 Option B, top-3 extension).

        Rank 1 is the turn's winner — byte-identical values to the
        pre-v7 single row (same name/prompt/agent/margin/amp, the
        winner's evidence, the L12-wired confidence, the repeated
        flag). Ranks 2/3 (when the scored list is longer) carry that
        pattern's OWN derived values: its name/affinities/margin/
        amplification, its own accumulated evidence, its own
        confidence, repeated=0 (the flag is winner-defined — a
        runner-up never WON). ``None`` fields write NULL columns.
        ``turn_key`` is the turn's ts as a deterministic string,
        shared by all of a turn's rows and distinct across turns.
        ``top_pattern`` accepts preset-bank names only — never free
        text, never a prompt fragment; an invalid winner row persists
        nothing (pre-v7 behavior, unchanged). Fault-isolated,
        log-and-never-raise, mirroring ``_persist_history_entry``.
        Reinforce (route A) remains top-only — see
        :meth:`_maybe_reinforce_and_persist`.
        """
        try:
            if not entries or entries[0] is None:
                return
            top_name = entries[0].get("name")
            if top_name is None or top_name not in bank_names:
                return  # top_pattern accepts preset-bank names only

            def _num(v):
                return None if v is None else float(v)

            turn_key = repr(float(ts))
            rows = []
            for rank, entry in enumerate((entries or [])[:3], start=1):
                name = entry.get("name") if isinstance(entry, dict) else None
                if name is None or name not in bank_names:
                    continue
                evidence_n = int(evidence_by_name.get(name, 0) or 0)
                confidence = self._pattern_confidence(
                    name, evidence_n, float(ts),
                    margin=(entry or {}).get("margin"))
                rows.append((
                    float(ts), name,
                    _num(entry.get("prompt_affinity")),
                    _num(entry.get("agent_affinity")),
                    _num(entry.get("margin")),
                    _num(entry.get("amplification")),
                    evidence_n,
                    None if confidence is None else float(confidence),
                    1 if (rank == 1 and repeated_top) else 0,
                    rank,
                    turn_key))
            if not rows:
                return

            _db, _mig = self._storage()
            conn = _db.connect()
            try:
                _mig.schema_bootstrap(conn)
                for row in rows:
                    conn.execute(
                        "INSERT INTO pattern_affinity_history "
                        "(ts, top_pattern, top_prompt_affinity, "
                        "top_agent_affinity, top_margin, "
                        "top_amplification, evidence, confidence, "
                        "repeated, rank, turn_key) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        row)
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
            # P0: value = the measured trait O_d = (S_d + 1)/2; confidence is
            # the separate calibrated reliability. (Previously both slots
            # carried conf[d], so an ambiguous resemblance read ~0.5 and the
            # interpreted track was pulled toward a false neutral.)
            targets = {
                d: ((float(scores[d]["S"]) + 1.0) / 2.0, conf[d])
                for d in _dims() if d in scores and d in conf
            }
            if not targets:
                return
            self.portrait.update_interpreted(targets)
            self._interpreted_evidence_count += 1
            self._interpreted_last_ts = ev.get("ts")
            try:
                plain = [t[1] for t in targets.values()]
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
            # v0.1.10 (review fix 3): tag the capsule's vector-space
            # identity (the encoder name; model_version was attached by
            # encode_capsule when a backend was used) BEFORE the bank
            # label and the in-memory store see the capsule, so
            # objective-friction prototypes, the capsule store and the
            # persisted row all carry the same provenance.
            if isinstance(capsule, dict):
                capsule["backend"] = self._backend_name
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
                _pulse_persist.persist_tool_banks(
                    self.protos, tool,
                    backend=self._backend_name,
                    model_version=self._pulse_vector_space()
                    ["model_version"])
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
                self._goal_vector = list(goal_vector) if goal_vector else None
                self._goal_ts = ev.get("ts")
                self._goal_backend = self._backend_name
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
                    scores = resemblance_all(prompt_vector, anchor_vectors)
                    self.portrait.update_observed(scores)
                    # Dynamic reassembly: measure this turn's OOD drift from
                    # the rolling reference, then fold its prompt vector into
                    # the dims it aligns with (S_d >= 0.3 = a positive
                    # instance) so the reference reassembles with usage.
                    ref = self._ensure_reference(backend, anchor_vectors)
                    self._ood_drift = ref.ood_drift_for(prompt_vector)
                    ts_re = float(ev.get("ts") or time.time())
                    for dim, s in scores.items():
                        if (s.get("S") or 0.0) >= 0.3:
                            ref.reassemble(dim, prompt_vector, ts_re)
                    # L-MIGRATE: persist the reassembled window (fault-
                    # isolated) so it survives a process restart.
                    self._persist_reference_state()
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

    # -- live expression + morphology (transient / lazy) ------------------- #
    def _set_live_expression(self, patterns, paired: bool, ts) -> None:
        """Set the transient live-expression target for one scored turn.

        Stage 1 (``paired=False``) fires when the prompt lands — the
        portrait must react WITHOUT waiting for the agent's reasoning.
        Stage 2 (``paired=True``) fires when a reasoning segment folds in:
        the paired affinities refine the expression (resonance/tension
        become available; the prompt-only channels are re-derived from
        BOTH sides). Missing reasoning never fabricates values — the
        paired derivation keeps the unavailable channels None and lowers
        confidence (perception/patterns.derive_live_expression).

        Continuity: the display snapshot at the moment of setting becomes
        the next settle's base, so a new target settles from where the
        portrait currently sits (no snap between unrelated expressions).
        Timestamps only — no timers, no animation worker, no persisted
        live state (process restart -> neutral resting state).
        """
        try:
            target = _patterns.derive_live_expression(
                patterns, paired=paired, ts=ts)
            now = float(ts) if ts is not None else time.time()
            self._live_expr_base = self._live_expression_display(now)
            self._live_expr_target = target
            self._live_expr_ts = now
            if paired:
                # A new paired row is about to land in the DB: the lazy
                # morphology summary is now potentially stale — invalidate
                # (recompute on the next read; the engine is the only
                # in-process writer, so this can't thrash).
                self._morph_cache_key = None
        except Exception:
            logger.exception(
                "its-in-your-head: live expression target set failed")

    def _live_expression_display(self, now: float | None = None) -> dict:
        """The live-expression DISPLAY value at read time (pure).

        Settle/hold/release arithmetic over the stored target + base
        timestamps (perception/patterns.live_expression_display): smooth
        arrival well under one pane poll, a several-second hold, then an
        exponential release back to the neutral resting state. Identical
        inputs always give the identical display (deterministic, no
        uncontrolled jitter). No evidence yet -> the neutral resting
        state, never an invented value.
        """
        if self._live_expr_target is None:
            return dict(NEUTRAL_LIVE_EXPRESSION)
        try:
            return _patterns.live_expression_display(
                self._live_expr_base, self._live_expr_target,
                self._live_expr_ts,
                now if now is not None else time.time())
        except Exception:
            logger.exception(
                "its-in-your-head: live expression display failed")
            return dict(NEUTRAL_LIVE_EXPRESSION)

    def morphology_view(self, now: float | None = None) -> dict:
        """Lazy, cached morphology from the persisted pattern history.

        Recurring two-pattern pairs are discovered from the existing
        pattern_affinity_history top-three rows (shared turn_key, bounded
        table — no new table, no migration). The summary is recomputed
        only when the underlying row set changed, then cached in this
        process; because the history is persisted, the morphology
        RECONSTRUCTS after a normal process restart. Derived numbers
        only — never raw prompts or reasoning text. Fault-isolated: any
        DB/module fault degrades to the neutral morphology (no mutation),
        never raises.
        """
        ts = float(now) if now is not None else time.time()
        try:
            rows = self._pattern_history_rows()
            sig = (len(rows), max((r[0] for r in rows), default=None))
            if self._morph_cache is not None and sig == self._morph_cache_key:
                return self._morph_cache
            self._morph_cache = _patterns.morphology_from_rows(
                rows, now_s=ts)
            self._morph_cache_key = sig
            return self._morph_cache
        except Exception:
            logger.exception(
                "its-in-your-head: morphology view failed")
            return dict(NEUTRAL_MORPHOLOGY)

    def _pattern_history_rows(self) -> list:
        """(ts, turn_key, name) rows of the persisted PAIRED pattern
        history, for pair-morphology discovery. Read-only; the table is
        bounded (512 rows), so this is a cheap, capped read. Legacy rows
        without a turn_key are treated as single-name turns by the
        grouping helper — they can never fabricate pair evidence."""
        _db, _mig = self._storage()
        conn = _db.connect()
        try:
            _mig.schema_bootstrap(conn)
            rows = conn.execute(
                "SELECT ts, turn_key, top_pattern FROM "
                "pattern_affinity_history "
                "WHERE top_pattern IS NOT NULL "
                "ORDER BY ts ASC, id ASC").fetchall()
            return [(float(r[0]), r[1], r[2]) for r in rows if r[2]]
        finally:
            conn.close()

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

    # -- PULSE live view (S4) --------------------------------------------- #
    def _pulse_vector_space(self) -> dict:
        """v0.1.10 (review fix 3): the ACTIVE embedding-space identity
        for the matrix's compatibility gate — {"id": encoder name,
        "model_version": encoder model tag (or None), "dim": vector
        length (from the live goal vector when present, else None —
        the gate then applies the provenance rules without a
        dimension gate)}."""
        space = {"id": self._backend_name, "model_version": None,
                 "dim": None}
        try:
            backend = self._get_backend()
            space["model_version"] = getattr(backend, "model_version",
                                             None)
        except Exception:
            backend = None
        if self._goal_vector:
            space["dim"] = len(self._goal_vector)
        return space

    def pulse_view(self) -> dict:
        self.ensure_pulse_hydrated()
        thr = {k: v for k, v in self._thresholds.items() if v is not None}
        capsules = None
        with self._lock:
            if self._capsules:
                capsules = {t: list(dq) for t, dq in self._capsules.items()}
        space = self._pulse_vector_space()
        # C.1: per-tool event-rate velocity (persisted tool_start
        # throughput in a rolling window) — the "fast-decay" LIVE signal.
        # The agent half owns the same profile DB (it writes events);
        # open a per-call handle (the _storage pattern) and fault-
        # tolerate -> {} (the lane then uses the legacy sort).
        event_rates: dict = {}
        try:
            _db, _mig = self._storage()
            vconn = _db.connect()
            try:
                _mig.schema_bootstrap(vconn)
                event_rates = _pulse_velocity.event_velocity(
                    vconn, window_s=_pulse_velocity.PULSE_VELOCITY_WINDOW_S)
            finally:
                vconn.close()
        except Exception:
            event_rates = {}
        common = dict(
            registry=self.registry, prototypes=self.protos,
            session_state=self._session_state(),
            goal_vector=self._goal_vector,
            goal_text=self.goal_text,
            backend=self._get_backend(),
            capsules_by_tool=capsules,
            thresholds=thr or None,
            # v0.1.10: the compatibility gate (unknown-provenance
            # vectors score but never satisfy readiness; a goal whose
            # known encoder space differs from the active one is
            # gated out).
            vector_space=space,
            goal_backend=self._goal_backend,
            event_rates=event_rates)
        rows = _pulse_matrix.build_matrix(**common)
        # v0.1.10 (review fix 2): the authoritative lane calculation
        # comes from the SAME inputs (one scoring path — the
        # dashboard's rows->lanes mirror stays a compatibility
        # fallback for pre-v0.1.10 engine responses only).
        lanes = _pulse_matrix.build_lanes(**common)
        return {"rows": list(rows),
                "columns": ["goal", "good", "friction", "unlabeled"],
                "available": bool(rows),
                "schema_version": 2,
                "lane_order": ["live", "goal", "good", "friction"],
                "lanes": lanes}

    def _perception_view(self, dims, full, values, agent) -> dict:
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
        now = time.time()
        ood = self._ood_drift
        ref_fresh = self.reference.freshness(now) if self.reference else None
        if third is None or not full.get("perception_available"):
            return {"patterns": [],
                    "dim_signals": {d: {"prompt": None, "agent": None}
                                    for d in dims},
                    "ood_drift": ood, "reference_freshness": ref_fresh,
                    "perception_available": False}
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
                    name, entry.get("evidence") or 0, now,
                    margin=entry.get("margin")),
                "repeated": bool(entry.get("repeated")),
            })
        # Per-dimension prompt/agent signals, keyed by dimension (the shape
        # the pane and the tests expect). Sourced from the GATED observed
        # (prompt/user) and interpreted (agent) values passed in from
        # portrait_view — None when a track has no evidence, never
        # fabricated. The third view's pattern-KEYED dim_signals must NOT
        # be re-keyed here: that mismatch is what used to yield all-None
        # even when the tracks had data.
        dim_signals = {
            d: {"prompt": values.get(d),
                "agent": agent.get(d)}
            for d in dims
        }
        return {"patterns": patterns,
                "dim_signals": dim_signals,
                "ood_drift": ood, "reference_freshness": ref_fresh,
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
        interp = interp_track.get("values") or {}
        values = ({d: snap.get("values", {}).get(d) for d in dims}
                  if has_observed else {d: None for d in dims})
        conf = ({d: snap.get("confidences", {}).get(d) for d in dims}
                if has_observed else {d: None for d in dims})
        # Agent (interpreted) track: values only when reasoning
        # evidence exists; otherwise None, never the neutral defaults.
        agent = ({d: interp.get(d) for d in dims} if has_interpreted
                 else {d: None for d in dims})
        perception = self._perception_view(dims, full, values, agent)
        if not has_observed and not has_interpreted:
            # Empty-shape contract (S5): no evidence -> available False and
            # None values (structural defaults are NOT data).
            return {"available": False,
                    "dimensions": {d: None for d in dims},
                    "confidence": {d: None for d in dims},
                    "user": {d: None for d in dims},
                    "agent": {d: None for d in dims},
                    "perception": perception,
                    "live_expression": self._live_expression_display(),
                    "morphology": self.morphology_view()}
        return {"available": True,
                "dimensions": values, "confidence": conf,
                "user": dict(values), "agent": agent,
                "perception": perception,
                "live_expression": self._live_expression_display(),
                "morphology": self.morphology_view()}

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
        # v0.1.10 (review fix 1): reset synchronizes with the HYDRATION
        # lock (outermost — the same order ensure_pulse_hydrated uses:
        # hydration lock, then data lock), so an in-flight hydration
        # merge cannot repopulate the state being wiped below.
        with self._pulse_hydration_lock:
            self._reset_body(merged)
        # Re-bootstrap the reinforced cache from the DB (fault-isolated:
        # failure degrades to {} via _load_reinforced_bank's discipline).
        try:
            try:
                from .perception import patterns as _patterns
            except ImportError:
                from perception import patterns as _patterns
            self._reinforced_bank(_patterns)
        except Exception:
            logger.exception(
                "its-in-your-head: reinforced cache re-bootstrap failed")
        # Registry rows live in the DB; the route's _reset_dataset()
        # wipes those — the registry re-reads lazily on the next observe.
        # v0.1.10 (review fix 1): with no live provider, reset() itself
        # is the explicit wipe, so mark the PULSE state empty and load
        # nothing; an in-flight restore cannot repopulate state after a
        # reset. With a live provider, the next natural read re-hydrates
        # from the DB; clear any pending retry so it is not blocked by
        # an earlier failure backoff.
        if self._settings_provider is None:
            try:
                self.ensure_pulse_hydrated()
            except Exception:
                logger.exception(
                    "its-in-your-head: pulse hydrate after reset failed")
        else:
            self._pulse_retry_at = 0.0

    def _reset_body(self, merged) -> None:
        """The explicit-wipe body under the data lock, called from
        reset() while it holds the hydration lock (no deadlock: the
        hydration path takes the locks in the same order)."""
        with self._lock:
            self._settings = dict(merged)
            self._capsules.clear()
            self._history.clear()
            # Explicit wipe: never resurrect pre-wipe rows from the DB.
            self._history_loaded = True
            # PULSE reset semantics: post-reset state stays empty — mark
            # hydrated so ensure_pulse_hydrated() never re-loads the
            # just-deleted state. Clear the goal vector too.
            self._pulse_hydrated = True
            self._goal_vector = None
            self._goal_ts = None
            self._goal_backend = None
            self.goal_text = None
            self._interpreted_evidence_count = 0
            self._interpreted_last_ts = None
            self._interpreted_last_confidence = None
            self._prompt_vectors.clear()
            self._pattern_turns = 0
            self._pattern_top_counts.clear()
            self._pattern_last_evidence.clear()
            # Explicit wipe also clears the transient live expression and
            # the lazy morphology cache (a post-wipe read must not
            # resurrect pre-wipe pair evidence: the route's
            # _reset_dataset() deletes the pattern history, so the next
            # read recomputes from the wiped table -> neutral).
            self._live_expr_target = None
            self._live_expr_ts = None
            self._live_expr_base = None
            self._morph_cache = None
            self._morph_cache_key = None
            # Reinforced dirty set (L10): reset is a re-bootstrap path —
            # clear the dirty marks and re-read the cache from the DB
            # below, so cross-process persists become visible again.
            self._reinforced_cache = {}
            self._reinforced_dirty = set()
            self._reinforced_loaded = False
            self.protos = pulse.PrototypeStore(
                cap=int(merged.get("pulse.prototype_cap", 8)),
                min_labeled=int(merged.get("pulse.min_labeled", 3)),
                goal_min_references=merged.get(
                    "pulse.goal_min_references"),
                good_min_samples=merged.get("pulse.good_min_samples"),
                user_friction_min_samples=merged.get(
                    "pulse.user_friction_min_samples"),
                objective_friction_min_samples=merged.get(
                    "pulse.objective_friction_min_samples"))
            self.portrait = _portrait_mod.Portrait(
                retain_days=int(merged.get("observation.retain_days", 90)))


# -- process singleton ------------------------------------------------------
_engine: Engine | None = None
_engine_lock = threading.Lock()
# Live-settings provider, registered by the register hook (set_live_settings_
# provider). When set, get_engine() applies it to the Engine so a persisted
# perception.encoder flip re-resolves the backend with no restart and no state
# wipe. Kept at module level (never built into Engine.__init__) so register()
# stays I/O-free: applying it here is deferred to first use.
_live_settings_provider = None


def set_live_settings_provider(provider) -> None:
    """Register a live-settings callable (``-> {key: value}``) applied to every
    Engine by ``get_engine``. No Engine is built here (lazy-DB rule); the hook
    only stores the callable. ``None`` clears it (Engine degrades to its
    startup backend name)."""
    global _live_settings_provider
    _live_settings_provider = provider


def get_engine() -> Engine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = Engine()
        if _live_settings_provider is not None:
            _engine.set_settings_provider(_live_settings_provider)
        return _engine


def on_events(batch) -> None:
    get_engine().on_events(batch)


def on_tool_start(event: dict) -> dict:
    return get_engine().on_tool_start(event)


def reset_engine(settings: dict | None = None) -> None:
    get_engine().reset(settings)
