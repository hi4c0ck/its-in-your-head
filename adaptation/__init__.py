"""Adaptation engine (S7): deterministic rules + pure injection builder.

No file writes, no network, no RNG, no memory-provider retrieval, no LLM
calls. Disabled by default (core/settings.py adaptation.enabled=False);
the gate lives in injection.build_injection.
"""
from . import rules, injection

__all__ = ["rules", "injection"]
