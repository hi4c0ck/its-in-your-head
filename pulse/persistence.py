"""Profile-DB persistence of the bounded PULSE vector state (S4).

Cross-process bridge: the agent half (metrics worker + engine) and the
isolated dashboard half (plugin-host child, which cannot import the
agent's in-memory ``engine`` module) run in separate processes against
the same profile DB. The live /pulse route reads the in-process engine;
when that engine is unavailable the dashboard serves /pulse from the
tables persisted here (see storage.migration v4).

Locked rules honoured:
- derived vectors ONLY: the goal row stores the embedding of the last
  user prompt, never the prompt text (v0.1.3 privacy ruling); capsule
  rows store the capsule vector, never the text payload.
- bounded: prototype banks follow the store's cap (FIFO), capsule rows
  follow the engine's per-tool bound; pruning happens on write.
- worker-side only: every public function here is called from the
  metrics worker path (engine.on_events / _on_tool_end / _on_user_turn),
  never from the collector's hot path (hot-path feedback labels only
  mark the in-memory store dirty; the next worker pass persists them).
- fault isolated: public functions log and never raise — a persistence
  fault must not break perception or the worker.
"""
from __future__ import annotations

import logging
import struct
import time

try:  # hermes namespaced import (gateway) vs top-level import (tests)
    from ..storage import database as _database
    from ..storage import migration as _migration
except ImportError:  # pragma: no cover
    from storage import database as _database  # type: ignore[no-redef]
    from storage import migration as _migration  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

_BANKS = ("good", "friction", "objective_friction")


# Vectors are stored at their NATIVE encoder dimension (256-d sketch or
# 384-d Tier-1): nothing is downconverted on write, so no resolution is
# ever lost. Cross-dim reachability is handled read-side
# (perception.canonical.cross_dim_cosine via pulse.prototypes.cosine).


def pack_vector(vector) -> bytes | None:
    """float vector -> little-endian float32 BLOB (the ``events.sketch``
    convention). None/empty -> None."""
    if not vector:
        return None
    try:
        return struct.pack(f"<{len(vector)}f",
                           *(float(x) for x in vector))
    except (TypeError, ValueError):
        return None


def unpack_vector(blob) -> list[float] | None:
    """Inverse of :func:`pack_vector`; malformed -> None."""
    if not blob:
        return None
    if len(blob) % 4:
        return None
    return list(struct.unpack(f"<{len(blob) // 4}f", bytes(blob)))


def _ensure_schema(conn) -> None:
    """Lazy bootstrap of the derived-vector tables (idempotent; the
    pulse_tools table is bootstrapped by the registry/migration)."""
    conn.executescript(_migration.PULSE_STATE_DDL)
    conn.commit()


def persist_tool_banks(store, tool: str, backend: str | None = None,
                       model_version: str | None = None) -> None:
    """Re-sync one tool's persisted banks to the in-memory store state
    (delete + insert the current capped banks; converged because the
    engine worker is the single writer per profile DB).

    ``backend``/``model_version`` tag the vector-space identity of the
    encoder that produced the bank's vectors (v8); omitted -> NULL
    (unknown provenance). The engine passes its active encoder.

    Best effort: logs and never raises.
    """
    try:
        banks = store.bank(str(tool))
        vectors = []
        for label in _BANKS:
            for vec in banks.get(label, ()) or []:
                packed = pack_vector(vec)
                if packed is not None:
                    vectors.append(
                        (str(tool), label, packed,
                         str(backend) if backend else None,
                         str(model_version) if model_version else None))
        conn = _database.connect()
        try:
            _ensure_schema(conn)
            conn.execute(
                "DELETE FROM pulse_prototypes WHERE tool_name = ?",
                (str(tool),))
            if vectors:
                now = time.time()
                conn.executemany(
                    "INSERT INTO pulse_prototypes"
                    " (tool_name, bank, ts, vector, backend,"
                    " model_version) VALUES (?, ?, ?, ?, ?, ?)",
                    [(t, b, now, v, bk, mv)
                     for t, b, v, bk, mv in vectors])
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception(
            "its-in-your-head: prototype bank persist failed for %s", tool)


def persist_capsule(capsule: dict, cap: int) -> None:
    """Append one encoded activity capsule (vector + ts only — never the
    text payload fields) and prune per-tool rows past ``cap`` (newest
    kept, mirroring the engine's deque bound). No-op for vector-less
    capsules. Best effort: logs and never raises.
    """
    if not isinstance(capsule, dict) or capsule.get("vector") is None:
        return
    tool = str(capsule.get("tool_name") or "unknown")
    ts = float(capsule.get("ts") or time.time())
    blob = pack_vector(capsule["vector"])
    if blob is None:
        return
    # v8 vector-space provenance: the encoder identity attached by the
    # engine at capsule build time (None pre-v8 / never attached =
    # unknown space).
    backend = capsule.get("backend")
    model_version = capsule.get("model_version")
    try:
        conn = _database.connect()
        try:
            _ensure_schema(conn)
            conn.execute(
                "INSERT INTO pulse_capsules (tool_name, ts, vector,"
                " backend, model_version) VALUES (?, ?, ?, ?, ?)",
                (tool, ts, blob,
                 str(backend) if backend else None,
                 str(model_version) if model_version else None))
            conn.execute(
                "DELETE FROM pulse_capsules WHERE tool_name = ? AND id"
                " NOT IN (SELECT id FROM pulse_capsules WHERE tool_name = ?"
                " ORDER BY ts DESC, id DESC LIMIT ?)",
                (tool, tool, max(1, int(cap))))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception(
            "its-in-your-head: capsule persist failed for %s", tool)


def persist_goal_vector(vector, ts: float | None = None,
                       backend: str | None = None) -> None:
    """Persist the derived goal vector (singleton row, id = 1): the
    embedding of the last user prompt. The raw prompt is NEVER written.
    Best effort: logs and never raises.
    """
    blob = pack_vector(vector)
    if blob is None:
        return
    try:
        conn = _database.connect()
        try:
            _ensure_schema(conn)
            conn.execute(
                "INSERT OR REPLACE INTO pulse_goal"
                " (id, ts, vector, backend) VALUES (1, ?, ?, ?)",
                (float(ts) if ts is not None else time.time(), blob,
                 str(backend) if backend else None))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception("its-in-your-head: goal vector persist failed")


def load_pulse_state() -> dict:
    """Legacy fault-isolated load (pre-v0.1.10 signature): returns the
    empty shape on any failure, never raises. New code uses
    :func:`load_pulse_state_checked` (success flag, retry-capable)."""
    state, _ok = load_pulse_state_checked()
    return state


def load_pulse_state_checked() -> tuple[dict, bool]:
    """Load persisted PULSE state for hydration (profile-scoped DB).

    Returns ``(state, ok)``: ``state`` has the shape
    {"banks": {tool: {"good":[...], "friction":[...],
    "objective_friction":[...]}}, "banks_meta": {tool: {label:
    {"backend": str|None, "model_version": str|None}}},
    "capsules": {tool: [{"vector":..., "ts":..., "tool_name":...,
    "backend": str|None, "model_version": str|None}]},
    "goal_vector": [...|None], "goal_ts": float|None,
    "goal_backend": str|None} and ``ok`` tells whether the database
    read actually succeeded (v0.1.10: the legacy wrapper could not
    distinguish a failed read from an empty database — a failed load
    must leave the engine un-hydrated for a later retry, not mark it
    hydrated with nothing loaded). Derived vectors only — no raw text
    is read (none is stored). Fault-isolated: logs and returns the
    empty shape with ``ok=False`` on failure, never raises.
    """
    empty = {"banks": {}, "banks_meta": {}, "capsules": {},
             "goal_vector": None, "goal_ts": None, "goal_backend": None}
    try:
        conn = _database.connect()
        try:
            _migration.schema_bootstrap(conn)
            banks: dict = {}
            banks_meta: dict = {}
            for tool, bank, blob, backend, model_version in conn.execute(
                    "SELECT tool_name, bank, vector, backend,"
                    " model_version FROM pulse_prototypes"
                    " ORDER BY tool_name, bank, id"):
                if bank not in _BANKS:
                    continue
                vec = unpack_vector(blob)
                if vec is None:
                    continue
                banks.setdefault(str(tool), {
                    "good": [], "friction": [],
                    "objective_friction": []})[bank].append(vec)
                # The writer delete+inserts a tool's whole bank under
                # one active encoder, so the LAST row's tags cover the
                # bank (newest rows carry the current space).
                banks_meta.setdefault(str(tool), {})[bank] = {
                    "backend": backend, "model_version": model_version}
            capsules: dict = {}
            for tool, ts, blob, backend, model_version in conn.execute(
                    "SELECT tool_name, ts, vector, backend,"
                    " model_version FROM pulse_capsules"
                    " ORDER BY tool_name, ts, id"):
                vec = unpack_vector(blob)
                if vec is None:
                    continue
                capsules.setdefault(str(tool), []).append(
                    {"vector": vec, "ts": ts, "tool_name": str(tool),
                     "backend": backend, "model_version": model_version})
            goal_vector = None
            goal_ts = None
            goal_backend = None
            row = conn.execute(
                "SELECT ts, vector, backend FROM pulse_goal WHERE id = 1"
            ).fetchone()
            if row is not None:
                goal_ts = row[0]
                goal_backend = row[2]
                goal_vector = unpack_vector(row[1])
            return {"banks": banks, "banks_meta": banks_meta,
                    "capsules": capsules, "goal_vector": goal_vector,
                    "goal_ts": goal_ts, "goal_backend": goal_backend,
                    }, True
        finally:
            conn.close()
    except Exception:
        logger.exception("its-in-your-head: pulse state load failed")
        return dict(empty), False
