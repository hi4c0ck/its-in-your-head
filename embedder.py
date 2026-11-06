"""Prompt-embedding interface — OWNER picks the engine.

Locked constraint (owner decision, see plan): LIGHT, NO USER HARM —
zero network per prompt, no heavy model download unless explicitly chosen.

Swap-in point: keep the `sketch(text) -> bytes` shape so the metrics pipeline
and the desktop half stay untouched when the engine changes.

Candidates:
- Tier 0: zero-dependency char-n-gram hash sketch (stdlib only, microseconds,
  ~1 KB packed per prompt).
- Tier 1: small local ONNX MiniLM-L6 (~85 MB one-time, CPU, ms-scale).
"""

DIM = 256  # placeholder until the owner fixes dimensionality


def sketch(text: str) -> bytes:
    """OWNER SLOT: deterministic text -> L2-normalized vector, packed bytes.

    Requirements: pure-local (no network), deterministic, microseconds-scale
    if ever called on a hot path. Return shape: struct-packed DIM float32s.
    """
    raise NotImplementedError("owner: implement embedder (ARCHITECTURE.md #Embedding)")
