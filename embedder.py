"""Prompt-embedding interface — Tier-0 stdlib sketch (LOCKED decision #2).

Locked constraint: LIGHT, NO USER HARM — zero network per prompt, no model
download. Swap-in point stays `sketch(text) -> bytes`; Tier-1 (local ONNX
MiniLM-L6) plugs in behind this same interface — the metrics pipeline and the
desktop half are engine-agnostic.

Tier-0 algorithm (deterministic, stdlib-only, microsecond-to-millisecond):
  1. casefold the text; pad short texts so 1-2 char prompts still yield grams
  2. hash every char 3-gram with blake2b (64-bit digest, PYTHONHASHSEED-
     independent) into DIM buckets
  3. L2-normalize the count vector (empty text -> zero vector)
  4. pack DIM x float32, little-endian -> DIM * 4 bytes

Cosine similarity between two sketches ~ weighted n-gram overlap: good
enough for repeat-prompt / topic clustering; ms-scale at most, and it only
ever runs in the background worker, never on a hook's hot path.
"""
import hashlib
import math
import struct

DIM = 256   # fixed 256-d sketch (plan default; the Tier-1 swap may keep it)
GRAM = 3    # char n-gram size (plan default)
DIGEST = 8  # blake2b digest bytes -> 64-bit hash, mod DIM


def _bucket(gram: str, cache: dict) -> int:
    b = cache.get(gram)
    if b is None:
        digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=DIGEST).digest()
        b = int.from_bytes(digest, "big") % DIM
        cache[gram] = b
    return b


def sketch(text: str | None) -> bytes:
    """Deterministic text -> L2-normalized vector, packed float32 bytes.

    Pure-local, no network. Shape: struct-packed DIM float32s (DIM * 4 bytes).
    Empty/None text yields the zero vector.
    """
    vec = [0.0] * DIM
    t = (text or "").casefold()
    if t:
        grams = t if len(t) >= GRAM else (" " + t + " ")
        cache: dict = {}
        for i in range(len(grams) - GRAM + 1):
            vec[_bucket(grams[i:i + GRAM], cache)] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
    return struct.pack(f"<{DIM}f", *vec)
