"""Hook bodies (C5 matrix): capture + enqueue only.

Hot-path rule (ARCHITECTURE #4): every body here is microseconds —
except pre_tool_call's synchronous LIVE counter increment, which is
also sub-0.5 ms (dict ops + queue put, no I/O, no inference).
Embedding + aggregation live in the metrics worker.

All callbacks take **kwargs (the dispatcher injects only the context
keys a signature names); returning None leaves behavior untouched.
A crash is never propagated from a hook body.
"""
import logging
import time

try:  # hermes namespaced import (gateway) vs top-level import (tests)
    from .. import metrics, engine
    from . import events, settings
    from .session_state import SessionState
    from ..adaptation.injection import build_injection
    from ..pulse.feedback import apply_feedback, detect_feedback
except ImportError:
    import metrics, engine
    from core import events, settings
    from core.session_state import SessionState
    from adaptation.injection import build_injection
    from pulse.feedback import apply_feedback, detect_feedback

logger = logging.getLogger(__name__)


def _adaptation_settings() -> dict:
    """Live C4 snapshot for the S7 seam (ctx config + ephemeral + DEFAULTS).

    The dashboard module owns the merged read (it received the plugin ctx
    at register() via set_ctx); when it is unavailable, DEFAULTS apply and
    the seam stays inert (adaptation disabled by default).
    """
    try:
        try:
            from ..dashboard import plugin_api as _papi
        except ImportError:
            from dashboard import plugin_api as _papi
        return _papi._settings_current()
    except Exception:
        return dict(settings.DEFAULTS)

STATE = SessionState()
_settings_snapshot = dict(settings.DEFAULTS)


def set_ctx(ctx) -> None:
    """Snapshot C4 settings from the plugin ctx at register() time."""
    global _settings_snapshot
    try:
        _settings_snapshot = settings.load(ctx)
    except Exception:
        logger.exception("its-in-your-head: settings snapshot failed")
    # §6: push the live retention value to the worker so its pruning path
    # actually consumes the configured observation.retain_days (best effort —
    # a failure here only means the worker keeps the default window).
    try:
        metrics.set_retain_days(_setting("observation.retain_days"))
    except Exception:
        logger.exception("its-in-your-head: retention-window push failed")


def _setting(key):
    return _settings_snapshot.get(key, settings.DEFAULTS.get(key))


def _safe_category(value, cap: int = 32):
    """P2.3: coerce a categorical field to a bounded, safe string. A missing
    value -> None; a non-string -> its type name (bounded); a string ->
    stripped + truncated to `cap`. No newlines/control chars survive."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = type(value).__name__
    cleaned = "".join(ch for ch in value if ch.isprintable())
    return cleaned[:cap] if cleaned else None


def _corr(kwargs) -> dict:
    return STATE.note(kwargs)


def _flush_reasoning(turn_id, session_id=None):
    got = STATE.reasoning_flush(session_id, turn_id)
    if got is None:
        return
    text, chunks = got
    metrics.enqueue(events.reasoning_seg(
        turn_id=turn_id, session_id=session_id,
        reasoning_text=text, reasoning_deltas=chunks))


# --- pre-existing hooks (behavior unchanged) ----------------------------

def _apply_explicit_feedback(message) -> None:
    """Deterministic explicit-feedback -> prototypes association.

    Runs after the turn is captured. Detection/association is fully
    fault-isolated: any exception (including a detector fault) yields
    no label, the observer continues, and raw text is never logged.
    Only the in-memory engine store is touched (no file I/O).
    """
    try:
        if detect_feedback(message) is None:
            return
        eng = engine.get_engine()
        # Hydrate BEFORE feedback association (feedback may precede the
        # first dashboard read). Worker/association path only — never
        # the sync tool-start hot path.
        ensure = getattr(eng, "ensure_pulse_hydrated", None)
        if callable(ensure):
            ensure()
        apply_feedback(eng.protos, eng.capsule_state(), message)
    except Exception:
        logger.exception(
            "its-in-your-head: explicit feedback association failed")


def _on_pre_llm_call(**kwargs):
    """Prompt capture (turn event) + S7 adaptation seam.

    Observer-only by default. When the user has explicitly enabled prompt
    adaptation, a single bounded fixed-template instruction
    ({"context", "meta"}) is returned; otherwise None. Any fault in the
    seam degrades to observer-only — the LLM call path is never broken."""
    message = ""
    try:
        message = kwargs.get("user_message") or ""
        if not isinstance(message, str):
            message = str(message)
        metrics.enqueue({
            "type": "turn",
            "ts": time.time(),
            "session_id": kwargs.get("session_id"),
            "turn_id": kwargs.get("turn_id"),
            "model": kwargs.get("model"),
            "platform": kwargs.get("platform"),
            "is_first_turn": kwargs.get("is_first_turn"),
            "user_message": message,
            "user_chars": len(message),
        })
    except Exception:
        logger.exception("its-in-your-head: turn capture failed")
    _apply_explicit_feedback(message)
    try:
        snap = _adaptation_settings()
        if snap.get("adaptation.enabled"):
            eng = engine.get_engine()
            pulse_state = {
                "prototypes": eng.protos,
                "capsules": eng.capsule_state(),
            }
            prompt_features = {"text": message,
                               "turn_id": kwargs.get("turn_id")}
            # §3: encode the prompt with the ACTIVE encoder (not
            # unconditional Tier-0) so the similarity is in the same
            # vector space as the PULSE prototypes; without a
            # compatible vector the channels that need one stay quiet.
            try:
                _backend = eng._get_backend()
                pvecs = _backend.encode([message]) if message else []
                if pvecs:
                    prompt_features["vector"] = list(pvecs[0])
            except Exception:
                pass
            try:
                prompt_features["vector_space"] = eng._pulse_vector_space()
            except Exception:
                pass
            res = build_injection(
                snap,
                prompt_features,
                pulse_state,
                turn_id=kwargs.get("turn_id"))
            if res is not None:
                return res
    except Exception:
        logger.exception("its-in-your-head: adaptation injection failed")
    return None


def _on_post_api_request(**kwargs):
    """Per-provider-call metrics."""
    try:
        usage = kwargs.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        metrics.enqueue({
            "type": "api",
            "ts": time.time(),
            "session_id": kwargs.get("session_id"),
            "turn_id": kwargs.get("turn_id"),
            "api_request_id": kwargs.get("api_request_id"),
            "api_call_count": kwargs.get("api_call_count"),
            "model": kwargs.get("model"),
            "provider": kwargs.get("provider"),
            "api_duration": kwargs.get("api_duration"),
            "finish_reason": kwargs.get("finish_reason"),
            "approx_input_tokens": kwargs.get("approx_input_tokens"),
            "usage": usage,
        })
    except Exception:
        logger.exception("its-in-your-head: api metrics capture failed")


def _on_api_request_error(**kwargs):
    """Error ticks. §4.1: only a bounded error-class + the numeric
    status/retry fields are carried; arbitrary provider/exception text
    (the raw `reason` + `error` body) is NOT retained in the event or
    persisted — the DB stores the category + a NULL message only."""
    try:
        error = kwargs.get("error")
        metrics.enqueue({
            "type": "error",
            "ts": time.time(),
            "session_id": kwargs.get("session_id"),
            "turn_id": kwargs.get("turn_id"),
            "api_request_id": kwargs.get("api_request_id"),
            "model": kwargs.get("model"),
            "status_code": kwargs.get("status_code"),
            "retry_count": kwargs.get("retry_count"),
            "retryable": kwargs.get("retryable"),
            "error_type": events.error_type(error),
        })
    except Exception:
        logger.exception("its-in-your-head: error capture failed")


# --- S1 hooks (C5 matrix) -----------------------------------------------

def _on_pre_tool_call(tool_name=None, args=None, task_id=None, **kwargs):
    """LIVE row create/increment: synchronous counter bump + enqueue.
    Sub-0.5 ms, zero inference, no I/O. Returns None (observer)."""
    try:
        corr = _corr(kwargs)
        name = tool_name or kwargs.get("tool_name") or "unknown"
        STATE.inc("tool_calls_started")
        ev = events.tool_start(
            tool_name=name,
            task_id=task_id or kwargs.get("task_id") or corr["task_id"],
            session_id=corr["session_id"], turn_id=corr["turn_id"],
            tool_call_id=kwargs.get("tool_call_id"),
            args_chars=len(str(args)) if args is not None else None)
        # PULSE registry bump happens worker-side (engine.on_events) — the
        # hot path stays STATE counter + enqueue only, sub-0.5 ms, no I/O.
        metrics.enqueue(ev)
    except Exception:
        logger.exception("its-in-your-head: tool_start capture failed")
    return None


def _on_post_tool_call(tool_name=None, args=None, result=None, task_id=None,
                       duration_ms=None, **kwargs):
    """Status/duration/result-type -> activity capsule -> queue.
    Raw tool output is never persisted (C6)."""
    try:
        corr = _corr(kwargs)
        name = tool_name or kwargs.get("tool_name") or "unknown"
        if isinstance(result, dict):
            # P2.3: store only the shape ("dict"), never the dynamic key
            # names — they can be long + user-controlled, an unnecessary
            # risk surface. The values are never persisted (C6).
            result_type = "dict"
            status = "error" if result.get("error") else "ok"
        elif isinstance(result, str):
            result_type, status = "str", "ok"
        elif result is None:
            result_type, status = "none", "ok"
        else:
            result_type, status = type(result).__name__, "ok"
        # P2.3: a host-supplied status is a short categorical, not free
        # text — cap it so an arbitrary string can't become a persistent
        # unbounded field.
        status = _safe_category(kwargs.get("status") or status) or "unknown"
        STATE.inc("tool_calls_completed")
        if status == "error":
            STATE.inc("tool_errors")
        metrics.enqueue(events.tool_end(
            tool_name=name, status=status,
            duration_ms=duration_ms if duration_ms is not None
            else kwargs.get("duration_ms"),
            result_type=result_type,
            result_chars=len(str(result)) if result is not None else None,
            task_id=task_id or kwargs.get("task_id") or corr["task_id"],
            session_id=corr["session_id"], turn_id=corr["turn_id"],
            tool_call_id=kwargs.get("tool_call_id")))
    except Exception:
        logger.exception("its-in-your-head: tool_end capture failed")


def _on_stream_delta(delta=None, kind=None, **kwargs):
    """First reasoning segment per turn ONLY (bounded buffer). Text
    deltas are ignored after triggering the reasoning flush; the
    buffer is discarded after enqueue. Reasoning capture is gated by
    the plugin-side observation.reasoning switch (C4) on top of the
    host's plugins.stream_reasoning_deltas opt-in."""
    try:
        corr = _corr(kwargs)
        kind = kind or kwargs.get("kind")
        turn_id = corr["turn_id"]
        if kind == "text":
            if turn_id and STATE.reasoning_pending(corr["session_id"], turn_id):
                _flush_reasoning(turn_id, corr["session_id"])
            return
        if kind != "reasoning" or turn_id is None:
            return
        if not _setting("observation.reasoning"):
            return
        STATE.inc("stream_deltas")
        if STATE.reasoning_feed(corr["session_id"], turn_id,
                                delta if delta is not None
                                else kwargs.get("delta")):
            pass  # still buffering the first segment
    except Exception:
        logger.exception("its-in-your-head: stream delta capture failed")


def _on_stream_end(final_text=None, finished=None, error=None, **kwargs):
    """Stream completion + error signals -> queue. Flushes any pending
    reasoning segment for the turn first."""
    try:
        corr = _corr(kwargs)
        turn_id = corr["turn_id"]
        if turn_id and STATE.reasoning_pending(corr["session_id"], turn_id):
            _flush_reasoning(turn_id, corr["session_id"])
        text = final_text if final_text is not None else kwargs.get("final_text")
        metrics.enqueue(events.stream_end(
            finished=finished if finished is not None else kwargs.get("finished"),
            error=error if error is not None else kwargs.get("error"),
            final_chars=len(text) if isinstance(text, str) else None,
            session_id=corr["session_id"], turn_id=turn_id))
    except Exception:
        logger.exception("its-in-your-head: stream end capture failed")


def _on_session_start(**kwargs):
    """Restore analytical state (per-session correlation + counters)."""
    try:
        STATE.session_started(kwargs.get("session_id"))
        metrics.enqueue(events.session_start(
            session_id=kwargs.get("session_id") or STATE.session_id))
    except Exception:
        logger.exception("its-in-your-head: session start capture failed")


def _on_session_end(**kwargs):
    """Flush the queue so pending events land before teardown, then clear
    this session's transient reasoning buffers + markers (§2.2): a session
    transition must not leave the preceding session's raw text reachable.
    A missing session_id clears everything (the host ended a session
    without identifying it)."""
    try:
        session_id = kwargs.get("session_id")
        STATE.session_ended(session_id)
        STATE.clear_session(session_id)
        metrics.enqueue(events.session_end(
            session_id=session_id or STATE.session_id))
        metrics.flush(timeout=3.0)
    except Exception:
        logger.exception("its-in-your-head: session end flush failed")
