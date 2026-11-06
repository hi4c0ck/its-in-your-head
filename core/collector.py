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
    from .. import metrics
    from . import events, settings
    from .session_state import SessionState
except ImportError:
    import metrics
    from core import events, settings
    from core.session_state import SessionState

logger = logging.getLogger(__name__)

STATE = SessionState()
_settings_snapshot = dict(settings.DEFAULTS)


def set_ctx(ctx) -> None:
    """Snapshot C4 settings from the plugin ctx at register() time."""
    global _settings_snapshot
    try:
        _settings_snapshot = settings.load(ctx)
    except Exception:
        logger.exception("its-in-your-head: settings snapshot failed")


def _setting(key):
    return _settings_snapshot.get(key, settings.DEFAULTS.get(key))


def _corr(kwargs) -> dict:
    return STATE.note(kwargs)


def _flush_reasoning(turn_id, session_id=None):
    got = STATE.reasoning_flush(turn_id)
    if got is None:
        return
    text, chunks = got
    metrics.enqueue(events.reasoning_seg(
        turn_id=turn_id, session_id=session_id,
        reasoning_text=text, reasoning_deltas=chunks))


# --- pre-existing hooks (behavior unchanged) ----------------------------

def _on_pre_llm_call(**kwargs):
    """Prompt capture (turn event). Observer-only: returns None."""
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
    """Error ticks."""
    try:
        error = kwargs.get("error")
        if not isinstance(error, dict):
            error = {}
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
            "reason": kwargs.get("reason"),
            "error": error,
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
        metrics.enqueue(events.tool_start(
            tool_name=name,
            task_id=task_id or kwargs.get("task_id") or corr["task_id"],
            session_id=corr["session_id"], turn_id=corr["turn_id"],
            tool_call_id=kwargs.get("tool_call_id"),
            args_chars=len(str(args)) if args is not None else None))
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
            result_type = "dict:" + ",".join(sorted(str(k) for k in result)[:8])
            status = "error" if result.get("error") else "ok"
        elif isinstance(result, str):
            result_type, status = "str", "ok"
        elif result is None:
            result_type, status = "none", "ok"
        else:
            result_type, status = type(result).__name__, "ok"
        status = kwargs.get("status") or status
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
            if turn_id and STATE.reasoning_pending(turn_id):
                _flush_reasoning(turn_id, corr["session_id"])
            return
        if kind != "reasoning" or turn_id is None:
            return
        if not _setting("observation.reasoning"):
            return
        STATE.inc("stream_deltas")
        if STATE.reasoning_feed(turn_id, delta if delta is not None
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
        if turn_id and STATE.reasoning_pending(turn_id):
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
    """Flush the queue so pending events land before teardown."""
    try:
        STATE.session_ended(kwargs.get("session_id"))
        metrics.enqueue(events.session_end(
            session_id=kwargs.get("session_id") or STATE.session_id))
        metrics.flush(timeout=3.0)
    except Exception:
        logger.exception("its-in-your-head: session end flush failed")
