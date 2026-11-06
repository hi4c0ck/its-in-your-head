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
import threading
from collections import deque

try:  # hermes namespaced import (gateway) vs top-level import (tests)
    from . import pulse
    from .pulse import matrix as _pulse_matrix
    from .perception import encoder as _encoder
    from .perception import anchors as _anchors
    from .perception import portrait as _portrait_mod
    from .core import settings as _settings
except ImportError:
    import pulse
    from pulse import matrix as _pulse_matrix
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
        self.goal_text: str | None = None
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

    def _on_tool_end(self, ev: dict) -> None:
        try:
            capsule = pulse.handle_post_tool_call(
                ev, self.registry, backend=self._get_backend(), args=None)
            self._store_capsule(ev.get("tool_name"), capsule)
            if isinstance(capsule, dict):
                self.protos.record_tool_end(capsule)
        except Exception:
            logger.exception("its-in-your-head: engine tool_end feed failed")

    def _on_user_turn(self, ev: dict) -> None:
        text = ev.get("user_message") or ""
        if not isinstance(text, str) or not text:
            return
        try:
            self.goal_text = text
            self.portrait.update_observed_from_text(text, self._get_backend())
            norm = self.portrait.normalized()
            self._history.append(
                {"ts": ev.get("ts"), "values": dict(norm.get("values") or {})})
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
        if not self.portrait.evidence:
            # Empty-shape contract (S5): no evidence -> available False and
            # None values (structural defaults are NOT data).
            return {"available": False,
                    "dimensions": {d: None for d in dims},
                    "confidence": {d: None for d in dims},
                    "user": {d: None for d in dims},
                    "agent": {d: None for d in dims}}
        full = self.portrait.snapshot()
        interp = (full.get("interpreted") or {}).get("values") or {}
        values = {d: snap.get("values", {}).get(d) for d in dims}
        conf = {d: snap.get("confidences", {}).get(d) for d in dims}
        agent = {d: interp.get(d) for d in dims}
        return {"available": True,
                "dimensions": values, "confidence": conf,
                "user": dict(values), "agent": agent}

    def drift_view(self) -> dict:
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
            self.goal_text = None
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
