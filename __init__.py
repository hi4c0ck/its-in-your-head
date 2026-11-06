"""It's in Your Head — registration.

Infra skeleton: hooks are wired per the manifest; the hook BODIES are OWNER
WORK. Contract (official docs, /docs/developer-guide/plugins):
- register(ctx) runs once at startup; a crash disables the plugin, Hermes continues.
- Hook callbacks are fire-and-forget observers accepting **kwargs.
- Hot-path rule: capture + enqueue only; heavy work belongs to a background worker.

See ARCHITECTURE.md for locked decisions, slot defaults, and doc pointers.
"""
import logging

logger = logging.getLogger(__name__)


def _on_pre_llm_call(**kwargs):
    """OWNER SLOT: prompt capture. kwargs include session_id, user_message,
    conversation_history, is_first_turn, model, platform.
    Enqueue only — microseconds on the hot path. This is the only hook that can
    also inject {"context": ...} back into the prompt (the "proxy" power)."""
    pass


def _on_post_api_request(**kwargs):
    """OWNER SLOT: per-provider-call metrics. kwargs include usage,
    api_duration, finish_reason, and the turn_id/api_request_id/api_call_count
    correlation fields."""
    pass


def _on_api_request_error(**kwargs):
    """OWNER SLOT: error ticks. kwargs include status_code, retry_count,
    retryable, reason, error."""
    pass


def register(ctx):
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("api_request_error", _on_api_request_error)
    # OWNER: add metrics.start_worker() here when the worker is coded.
    # Lazy-DB rule: never open the plugin DB at register() time — doctor runs
    # register() under a temp HERMES_HOME and deletes it (WinError 145 cleanup).
    logger.info("its-in-your-head: observer hooks registered (skeleton)")
