"""L4 engine+metrics wiring: Tier-1 dim regression, perception key,
pattern_affinity_history rows, privacy sentinel, C6 ordering.

Byte-sized fakes only.
"""
import hashlib
import math
import sqlite3
import time

import pytest

import metrics
from perception import anchors, patterns as patterns_mod


@pytest.fixture(autouse=True)
def _fresh_engine(tmp_path, monkeypatch):
    db = tmp_path / "engine.db"
    metrics.set_db_path(db)
    import engine
    monkeypatch.setattr(engine, "_engine", None)
    monkeypatch.setattr(
        anchors, "get_anchor_vectors",
        lambda backend, **kw: anchors.build_anchor_vectors(backend))
    yield db
    try:
        metrics.stop_worker()
    except Exception:
        pass
    while metrics._items:
        metrics._items.popleft()
    try:
        import engine as _engine_mod
        _engine_mod._engine = None
    except Exception:
        pass


def _eng():
    import engine
    return engine.Engine()


class FakeBackend:
    """Deterministic 384-dim backend (Tier-1 shape, no network)."""

    model_version = "fake-384-v1"
    dim = 384

    def __init__(self):
        self.calls = []

    def encode(self, texts):
        out = []
        for text in texts:
            text = text or ""
            self.calls.append(text)
            if "MISMATCH-MARKER" in text:
                out.append([1.0, 0.0])  # wrong space on purpose
                continue
            vec = [0.0] * self.dim
            t = text.casefold()
            grams = t if len(t) >= 3 else (" " + t + " ")
            for i in range(len(grams) - 2):
                h = hashlib.blake2b(grams[i:i + 3].encode(),
                                    digest_size=8).digest()
                vec[int.from_bytes(h, "big") % self.dim] += 1.0
            n = math.sqrt(sum(v * v for v in vec))
            out.append([v / n for v in vec] if n else vec)
        return out


def test_tier1_reasoning_scores_patterns_no_noop_and_guard_fires():
    eng = _eng()
    fake = FakeBackend()
    eng._backend = fake
    eng.on_events([{"type": "user_turn", "ts": time.time(),
                    "session_id": "s1", "turn_id": "t384",
                    "user_message": "Brainstorm options and think sideways",
                    "user_chars": 34}])
    eng.on_events([{"type": "reasoning_seg", "ts": time.time(),
                    "session_id": "s1", "turn_id": "t384",
                    "reasoning_text": "I am brainstorming alternatives "
                    "before deciding",
                    "reasoning_chars": 50}])
    # 384-dim fake: reasoning path produces scored patterns, no no-op
    snap = eng.portrait.snapshot()
    assert snap["patterns"] is not None
    assert len(snap["patterns"]) == len(patterns_mod.PATTERN_BANK)
    assert eng._interpreted_evidence_count == 1
    assert any(p["agent_affinity"] is not None
               for p in snap["patterns"])
    assert any("brainstorming alternatives" in c for c in fake.calls)
    # dim guard still fires for a genuinely different-space vector
    eng2 = _eng()
    fake2 = FakeBackend()
    eng2._backend = fake2
    eng2.on_events([{"type": "reasoning_seg", "ts": time.time(),
                     "session_id": "s1", "turn_id": "tX",
                     "reasoning_text": "MISMATCH-MARKER payload",
                     "reasoning_chars": 20}])
    assert eng2.portrait.snapshot()["interpreted"]["evidence_count"] == 0
    assert eng2._interpreted_evidence_count == 0


def test_portrait_view_perception_empty_shape():
    eng = _eng()
    view = eng.portrait_view()
    assert set(view) == {"available", "dimensions", "confidence",
                         "user", "agent", "perception"}
    dims = set(view["perception"]["dim_signals"])
    assert dims == {d for d in
                    ["directness", "context_richness", "exploration",
                     "analytical_depth", "iteration", "scope"]}
    assert view["perception"] == {
        "patterns": [],
        "dim_signals": {d: {"prompt": None, "agent": None}
                        for d in dims},
        "perception_available": False}


def test_portrait_view_perception_filled_shape():
    eng = _eng()
    eng.on_events([{"type": "user_turn", "ts": time.time(),
                    "session_id": "s1", "turn_id": "tF",
                    "user_message": "Give the short answer, no preamble",
                    "user_chars": 30}])
    eng.on_events([{"type": "reasoning_seg", "ts": time.time(),
                    "session_id": "s1", "turn_id": "tF",
                    "reasoning_text": "I am giving the shortest direct "
                    "answer I can", "reasoning_chars": 40}])
    perc = eng.portrait_view()["perception"]
    assert set(perc) == {"patterns", "dim_signals", "perception_available"}
    assert perc["perception_available"] is True
    for p in perc["patterns"]:
        assert set(p) == {"name", "prompt_affinity", "agent_affinity",
                          "margin", "amplification", "evidence",
                          "confidence", "repeated"}
        assert isinstance(p["evidence"], int)
        assert isinstance(p["repeated"], bool)
    assert len(perc["patterns"]) == len(patterns_mod.PATTERN_BANK)
    assert any(p["agent_affinity"] is not None
               for p in perc["patterns"])
    for sig in perc["dim_signals"].values():
        assert set(sig) == {"prompt", "agent"}


def test_portrait_view_perception_none_agent_without_reasoning():
    eng = _eng()
    eng.on_events([{"type": "user_turn", "ts": time.time(),
                    "session_id": "s1", "turn_id": "tN",
                    "user_message": "Fix the login page", "user_chars": 17}])
    perc = eng.portrait_view()["perception"]
    assert perc["perception_available"] is True
    assert all(p["agent_affinity"] is None for p in perc["patterns"])
    assert all(s["agent"] is None
               for s in perc["dim_signals"].values())


def test_pattern_affinity_history_row_and_prune(tmp_db_conn=None):
    import storage.migration as mig
    preset = {e["name"] for e in patterns_mod.PATTERN_BANK}
    eng = _eng()
    eng.on_events([{"type": "user_turn", "ts": 1700000000.5,
                    "session_id": "s1", "turn_id": "tH",
                    "user_message": "Fix it now", "user_chars": 10}])
    eng.on_events([{"type": "reasoning_seg", "ts": 1700000000.6,
                    "session_id": "s1", "turn_id": "tH",
                    "reasoning_text": "I am keeping the change narrow",
                    "reasoning_chars": 30}])
    # The DB override is set by the fixture via metrics.set_db_path;
    # resolve it through storage.
    from storage import database as _db
    conn = _db.connect()
    try:
        mig.schema_bootstrap(conn)
        rows = conn.execute(
            "SELECT top_pattern, top_prompt_affinity, top_agent_affinity,"
            " top_margin, top_amplification, evidence, confidence, repeated"
            " FROM pattern_affinity_history").fetchall()
        assert rows, "pattern row missing"
        for row in rows:
            assert row[0] in preset
            for v in row[1:5]:
                assert v is None or isinstance(v, (int, float))
            assert isinstance(row[5], int)
            assert row[6] is None or isinstance(row[6], (int, float))
            assert row[7] in (0, 1)
            # paired row: agent columns carry derived numbers
            assert isinstance(row[2], (int, float))
        # prune-to-cap keeps the last PATTERN_AFFINITY_HISTORY_MAX rows
        first_id = conn.execute(
            "SELECT MIN(id) FROM pattern_affinity_history").fetchone()[0]
        conn.executemany(
            "INSERT INTO pattern_affinity_history "
            "(ts, top_pattern, evidence) VALUES (?, ?, 0)",
            [(1700000100.0 + i, "directness")
             for i in range(mig.PATTERN_AFFINITY_HISTORY_MAX + 7)])
        conn.commit()
        mig.prune_pattern_affinity_history(conn)
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM pattern_affinity_history").fetchone()[0]
        assert count == mig.PATTERN_AFFINITY_HISTORY_MAX
        new_min = conn.execute(
            "SELECT MIN(id) FROM pattern_affinity_history").fetchone()[0]
        assert new_min > first_id
    finally:
        conn.close()


def test_privacy_sentinel_in_no_table_and_no_log(tmp_path, caplog):
    sentinel = "SENTINEL-ZQX-SECRET-987654321"
    metrics.start_worker()
    metrics.enqueue({"type": "turn", "ts": time.time(),
                     "session_id": "sPriv", "turn_id": "tPriv",
                     "user_message": "handle " + sentinel,
                     "user_chars": len(sentinel)})
    metrics.enqueue({"type": "reasoning_seg", "ts": time.time(),
                     "session_id": "sPriv", "turn_id": "tPriv",
                     "reasoning_text": sentinel + " reasoning",
                     "reasoning_chars": len(sentinel)})
    result = metrics.flush(timeout=5.0)
    assert result.drained
    from storage import database as _db
    conn = _db.connect()
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        assert tables
        for table in tables:
            for row in conn.execute(f"SELECT * FROM {table}"):
                for value in row:
                    if isinstance(value, bytes):
                        assert sentinel.encode() not in value, table
                    elif isinstance(value, str):
                        assert sentinel not in value, table
        sketch = conn.execute(
            "SELECT sketch FROM events WHERE type='reasoning_seg'"
        ).fetchall()
        assert sketch and all(s[0] is None for s in sketch)
    finally:
        conn.close()
    assert sentinel not in caplog.text


def test_c6_ordering_raw_available_during_engine_feed_then_purged(
        tmp_path, monkeypatch):
    seen = {}

    class CaptureEngine:
        def on_events(self, batch):
            seen["batch"] = [dict(ev) for ev in batch if isinstance(ev, dict)]

    monkeypatch.setattr(metrics, "_engine", CaptureEngine())
    sent = "C6-ORDERING-RAW-TEXT-xyz"
    metrics.start_worker()
    metrics.enqueue({"type": "reasoning_seg", "ts": time.time(),
                     "session_id": "sC6", "turn_id": "tC6",
                     "reasoning_text": sent, "reasoning_chars": len(sent)})
    result = metrics.flush(timeout=5.0)
    assert result.drained
    assert seen["batch"], "engine feed never ran"
    fed = [e for e in seen["batch"] if e.get("type") == "reasoning_seg"]
    assert fed and fed[0].get("reasoning_text") == sent  # available pre-purge
    from storage import database as _db
    conn = _db.connect()
    try:
        rows = conn.execute(
            "SELECT user_message, sketch FROM events "
            "WHERE type='reasoning_seg'").fetchall()
        assert rows and all(r[0] is None and r[1] is None for r in rows)
    finally:
        conn.close()
