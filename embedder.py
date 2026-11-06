"""Tier-0 zero-dependency prompt sketch.

char 3-grams hashed (FNV-1a) into a fixed 256-d vector, L2-normalized.
Cosine similarity ≈ n-gram overlap. Pure stdlib, microseconds, zero network.
Swap-in point for Tier-1 (local ONNX MiniLM) behind the same `sketch()` shape.
"""
import struct

DIM = 256
_FNV = 0x811C9DC5
_PRIME = 0x01000193


def _fnv(text: str) -> int:
    h = _FNV
    for ch in text:
        h ^= ord(ch)
        h = (h * _PRIME) & 0xFFFFFFFF
    return h


def sketch(text: str) -> bytes:
    """Deterministic 256-d L2-normalized sketch as packed float32 (1 KB)."""
    v = [0.0] * DIM
    padded = f" {(text or '').lower()} "
    for i in range(len(padded) - 2):
        v[_fnv(padded[i:i + 3]) % DIM] += 1.0
    norm = sum(x * x for x in v) ** 0.5
    if norm > 0:
        v = [x / norm for x in v]
    return struct.pack(f"{DIM}f", *v)


def load(blob: bytes):
    """Inverse of sketch(): bytes -> list[float]."""
    return list(struct.unpack(f"{DIM}f", blob))


def cosine(a: bytes, b: bytes) -> float:
    va, vb = load(a), load(b)
    return sum(x * y for x, y in zip(va, vb))  # both L2-normalized
