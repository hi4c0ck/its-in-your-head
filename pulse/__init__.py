"""PULSE tool-activity matrix (S4): dynamic tool rows, activity capsules,
bounded GOOD/FRICTION prototype banks, and the LIVE/GOAL/GOOD/FRICTION view.

Hot-path rule: the pre_tool_call path (tool_registry.live_tick /
activity_encoder.handle_pre_tool_call) touches counters/registry only and
never calls EmbeddingBackend.encode(); encoding happens on the
post_tool_call / worker side (handle_post_tool_call, matrix building with
an explicit backend).
"""
from .tool_registry import ToolRegistry, live_tick
from .activity_encoder import (
    build_capsule, encode_capsule, sanitize_operation, duration_bucket,
    handle_pre_tool_call, handle_post_tool_call, CAPSULE_FIELDS,
)
from .prototypes import PrototypeStore, classify_feedback, cosine
from .matrix import build_matrix, neutral_cell

__all__ = [
    "ToolRegistry", "live_tick",
    "build_capsule", "encode_capsule", "sanitize_operation",
    "duration_bucket", "handle_pre_tool_call", "handle_post_tool_call",
    "CAPSULE_FIELDS",
    "PrototypeStore", "classify_feedback", "cosine",
    "build_matrix", "neutral_cell",
]
