"""It's in Your Head — registration.

Observer hooks only: capture on the hot path is enqueue-only (microseconds);
embedding + aggregation happens in the metrics worker thread.
"""
import logging
import time

try:  # hermes namespaced plugin import (known parent package)
    from . import metrics
except ImportError:  # top-level import (tests / standalone dev)
    import metrics

logger = logging.getLogger(__name__)


def _on_pre_llm_call(**kwargs):
    try:
        metrics.enqueue({
            "type": "turn.start",
            "session_id": kwargs.get("session_id"),
            "model": kwargs.get("model"),
            "user_message": kwargs.get("user_message") or "",
            "ts": time.time(),
        })
    except Exception:
        logger.exception("iitoh: pre_llm_call capture failed")


def _on_post_api_request(**kwargs):
    try:
        usage = kwargs.get("usage") or {}
        metrics.enqueue({
            "type": "api.call",
            "session_id": kwargs.get("session_id"),
            "model": kwargs.get("model"),
            "duration_s": kwargs.get("api_duration"),
            "finish_reason": kwargs.get("finish_reason"),
            "prompt_tokens": usage.get("prompt_tokens") if isinstance(usage, dict) else None,
            "completion_tokens": usage.get("completion_tokens") if isinstance(usage, dict) else None,
            "total_tokens": usage.get("total_tokens") if isinstance(usage, dict) else None,
            "ts": time.time(),
        })
    except Exception:
        logger.exception("iitoh: post_api_request capture failed")


def _on_api_request_error(**kwargs):
    try:
        err = kwargs.get("error") or {}
        metrics.enqueue({
            "type": "api.error",
            "session_id": kwargs.get("session_id"),
            "model": kwargs.get("model"),
            "status_code": kwargs.get("status_code"),
            "reason": kwargs.get("reason"),
            "ts": time.time(),
        })
    except Exception:
        logger.exception("iitoh: api_request_error capture failed")


def register(ctx):
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("api_request_error", _on_api_request_error)
    metrics.start_worker()
    logger.info("its-in-your-head: observer hooks + metrics worker registered")
