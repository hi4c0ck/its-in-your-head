"""E3 top-3 persistence (schema v7): the engine persists up to THREE
derived pattern_affinity_history rows per paired turn — rank 1 winner
(byte-identical to the pre-v7 single row) plus runner-up ranks 2/3
carrying that pattern's OWN derived values. ``turn_key`` groups a
turn's rows. Prune stays 512 rows by id. Prompt-only turns persist
nothing; the winner-name invariant is unchanged.

Byte-sized: v6-shaped file sqlite + direct engine-method seams, no
network, deterministic fakes only (s12-style FakeBackend).
"""
import hashlib
import math
import time

import pytest
import sqlite3

import metrics
from perception import anchors


PRE_V7_DDL = """CREATE TABLE pattern_affinity_history (
    id                    INTEGER PRIMARY KEY,
    ts                    REAL NOT NULL,
    top_pattern           TEXT,
    top_prompt_affinity   REAL,
    top_agent_affinity    REAL,
    top_margin            REAL,
    top_amplification     REAL,
    evidence              INTEGER NOT NULL DEFAULT 0,
    confidence            REAL,
    repeated              INTEGER NOT NULL DEFAULT 0
)"""


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
            vec = [0.0] * self.dim
            t = text.casefold()
            grams = t if len(t) >= 3 else (" " + t + " ")
            for i in range(len(grams) - 2):
                h = hashlib.blake2b(
                    grams[i:i + 3].encode(), digest_size=8).digest()
                vec[int.from_bytes(h, "big") % self.dim] += 1.0
            n = math.sqrt(sum(v * v for v in vec))
            out.append([v / n for v in vec] if n else vec)
        return out


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


def _rows(db, where="1=1"):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            f"SELECT ts, top_pattern, top_prompt_affinity, "
            f"top_agent_affinity, top_margin, top_amplification, "
            f"evidence, confidence, repeated, rank, turn_key "
            f"FROM pattern_affinity_history WHERE {where} "
            f"ORDER BY rowid ASC").fetchall()
    finally:
        conn.close()


def test_v6_to_v7_alter_preserves_rows(tmp_path):
    """A real v6-shaped DB upgrades in place: existing rows keep rank 1
    (column default) and turn_key NULL; data intact; version 7."""
    db = tmp_path / "v6.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(PRE_V7_DDL)
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', '6')")
        conn.execute(
            "INSERT INTO pattern_affinity_history "
            "(ts, top_pattern, top_prompt_affinity, top_agent_affinity, "
            " top_margin, top_amplification, evidence, confidence, repeated)"
            " VALUES (1.0, 'focused', 0.3, 0.9, 0.1, 0.6, 4, 0.8, 1)")
        conn.commit()
    finally:
        conn.close()
    from storage import migration
    conn = sqlite3.connect(str(db))
    try:
        assert migration.schema_bootstrap(conn) is True
        assert migration.get_schema_version(conn) == 7
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(pattern_affinity_history)")}
        assert {"rank", "turn_key"} <= cols
        row = conn.execute(
            "SELECT ts, top_pattern, top_prompt_affinity, "
            "top_agent_affinity, top_margin, top_amplification, "
            "evidence, confidence, repeated, rank, turn_key "
            "FROM pattern_affinity_history").fetchone()
        # legacy data intact, rank defaulted to 1, turn_key NULL
        assert row == (1.0, "focused", 0.3, 0.9, 0.1, 0.6, 4, 0.8, 1,
                       1, None)
    finally:
        conn.close()


def test_fresh_bootstrap_has_v7_columns(tmp_path):
    db = tmp_path / "fresh.db"
    conn = sqlite3.connect(str(db))
    try:
        from storage import migration
        assert migration.schema_bootstrap(conn) is True
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(pattern_affinity_history)")}
        assert {"rank", "turn_key"} <= cols
    finally:
        conn.close()


def _entries():
    return [
        {"name": "context_richness", "prompt_affinity": 0.7,
         "agent_affinity": 0.9, "margin": 0.12, "amplification": 0.2,
         "evidence": 0, "confidence": None},
        {"name": "directness", "prompt_affinity": 0.5,
         "agent_affinity": 0.78, "margin": None, "amplification": 0.28,
         "evidence": 0, "confidence": None},
        {"name": "exploration", "prompt_affinity": None,
         "agent_affinity": 0.6, "margin": None, "amplification": None,
         "evidence": 0, "confidence": None},
    ]


def test_persist_writes_three_rows_with_own_values(tmp_path):
    eng = _eng()
    eng._pattern_top_counts = {"context_richness": 1}
    eng._pattern_turns = 3
    ts = 1000.0
    eng._persist_pattern_rows(
        _entries(), ts,
        {"context_richness": 5, "directness": 2, "exploration": 1},
        True,  # repeated_top
        {"context_richness", "directness", "exploration"})
    rows = _rows(tmp_path / "engine.db")
    assert len(rows) == 3
    # rank 1: the winner, byte-identical value semantics to the legacy
    # single row (own evidence 5, repeated=1, margin 0.12)
    r1 = rows[0]
    assert (r1[1], r1[9], r1[6], r1[8]) == ("context_richness", 1, 5, 1)
    assert r1[3] == pytest.approx(0.9) and r1[4] == pytest.approx(0.12)
    # rank 2/3: OWN name/affinities, OWN evidence, repeated=0
    r2, r3 = rows[1], rows[2]
    assert (r2[1], r2[9], r2[6], r2[8]) == ("directness", 2, 2, 0)
    assert (r3[1], r3[9], r3[6], r3[8]) == ("exploration", 3, 1, 0)
    # None entry field -> NULL column (exploration prompt affinity None)
    assert r3[2] is None and r3[5] is None
    # confidence: rank 1 carries the winner's margin-gated value
    want1 = eng._pattern_confidence(
        "context_richness", 5, ts, margin=0.12)
    assert r1[7] == pytest.approx(want1)
    want3 = eng._pattern_confidence("exploration", 1, ts, margin=None)
    assert r3[7] == pytest.approx(want3)
    # turn_key: identical within the turn
    assert r1[10] == r2[10] == r3[10] == repr(float(ts))


def test_persist_two_entry_list_writes_two_rows(tmp_path):
    eng = _eng()
    eng._pattern_turns = 1
    eng._persist_pattern_rows(
        _entries()[:2], 1001.0, {"directness": 1}, False,
        {"context_richness", "directness"})
    rows = _rows(tmp_path / "engine.db")
    assert [r[9] for r in rows] == [1, 2]
    assert [r[1] for r in rows] == ["context_richness", "directness"]
    assert rows[1][10] == repr(1001.0)


def test_invalid_winner_persists_nothing(tmp_path):
    eng = _eng()
    eng._pattern_turns = 1
    eng._persist_pattern_rows(
        [{"name": "not_a_bank_name"}, {"name": "directness"}], 1002.0,
        {}, False, {"directness"})
    # an invalid winner persists NOTHING — not even runner-up rows;
    # bootstrap the table (the persist path returned before creating it)
    from storage import migration
    conn = sqlite3.connect(str(tmp_path / "engine.db"))
    try:
        migration.schema_bootstrap(conn)
    finally:
        conn.close()
    assert _rows(tmp_path / "engine.db") == []


def test_prompt_only_turn_persists_nothing(tmp_path):
    """Engine-level gate: user_turn without reasoning_seg -> no rows."""
    eng = _eng()
    eng._backend = FakeBackend()
    eng.on_events([{
        "type": "user_turn", "ts": time.time(), "session_id": "s1",
        "turn_id": "t1", "user_message": "Just the number, no prose",
        "user_chars": 25,
    }])
    conn = sqlite3.connect(str(tmp_path / "engine.db"))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM pattern_affinity_history").fetchone()[0]
    finally:
        conn.close()
    assert n == 0


def test_paired_turn_persists_top3(tmp_path):
    eng = _eng()
    eng._backend = FakeBackend()
    eng.on_events([{
        "type": "user_turn", "ts": time.time(), "session_id": "s1",
        "turn_id": "t1",
        "user_message": "Brainstorm options and think sideways",
        "user_chars": 34,
    }])
    eng.on_events([{
        "type": "reasoning_seg", "ts": time.time(), "session_id": "s1",
        "turn_id": "t1", "reasoning_text":
        "I am brainstorming alternatives before deciding",
        "reasoning_chars": 43,
    }])
    rows = _rows(tmp_path / "engine.db")
    assert [r[9] for r in rows] == [1, 2, 3]
    tk = {r[10] for r in rows}
    assert len(tk) == 1  # one turn, one key
    assert rows[0][8] in (0, 1)  # winner row carries the repeated flag
    assert rows[1][8] == 0 and rows[2][8] == 0


def test_turn_key_distinct_across_turns(tmp_path):
    eng = _eng()
    eng._pattern_turns = 1
    eng._persist_pattern_rows(
        _entries(), 1000.0, {}, False,
        {"context_richness", "directness", "exploration"})
    eng._persist_pattern_rows(
        _entries(), 2000.0, {}, False,
        {"context_richness", "directness", "exploration"})
    rows = _rows(tmp_path / "engine.db")
    assert len(rows) == 6
    keys = [r[10] for r in rows]
    assert keys[0] == keys[1] == keys[2] == repr(1000.0)
    assert keys[3] == keys[4] == keys[5] == repr(2000.0)
    assert keys[0] != keys[3]


def test_prune_cap_512_on_v7_table(tmp_path):
    from storage import migration
    db = tmp_path / "prune.db"
    conn = sqlite3.connect(str(db))
    try:
        migration.schema_bootstrap(conn)
        cap = migration.PATTERN_AFFINITY_HISTORY_MAX
        for i in range(cap + 1):
            conn.execute(
                "INSERT INTO pattern_affinity_history "
                "(ts, top_pattern, rank, turn_key) "
                "VALUES (?, ?, ?, ?)",
                (float(i % 7), "focused", 1 + (i % 3), "tk" + str(i // 3)))
        conn.commit()
        migration.prune_pattern_affinity_history(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM pattern_affinity_history").fetchone()[0] \
            == cap
        # the oldest row (i=0, turn 'tk0') is pruned; its turn window
        # (i=0,1,2) keeps the two newer rows
        assert conn.execute(
            "SELECT COUNT(*) FROM pattern_affinity_history "
            "WHERE turn_key = 'tk0'").fetchone()[0] == 2
        assert conn.execute(
            "SELECT MIN(id) FROM pattern_affinity_history").fetchone()[0] \
            == 2
    finally:
        conn.close()
