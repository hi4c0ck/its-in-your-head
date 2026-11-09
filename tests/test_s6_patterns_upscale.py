"""S6 pattern-bank upscaling (D3 routes A+B, L5). Byte-sized fakes only."""
import hashlib
import math
import sqlite3

import pytest

from perception import patterns
from perception.anchors import DIMENSIONS
from core import settings

DIM = 8


class FakeBackend:
    """Deterministic local fake: records every encode call."""

    def __init__(self, model_version="fake-v1"):
        self.model_version = model_version
        self.calls = []

    def encode(self, texts):
        self.calls.append(list(texts))
        return [_vec(t) for t in texts]


def _vec(text):
    out = []
    for i in range(DIM):
        h = hashlib.sha256(f"{text}:{i}".encode()).digest()
        out.append((h[0] / 127.5) - 1.0)
    return out


def _norm(v):
    return math.sqrt(sum(x * x for x in v))


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b)) / (_norm(a) * _norm(b))


@pytest.fixture(autouse=True)
def _fresh_cache():
    patterns._BANK_CACHE.clear()
    yield
    patterns._BANK_CACHE.clear()


def _custom(name="custom_x", prompt=None, agent=None):
    return {
        "name": name,
        "prompt": prompt if prompt is not None else ["custom prompt alpha"],
        "agent": agent if agent is not None else ["custom agent beta"],
    }


# -- settings defaults -------------------------------------------------------

def test_settings_upscale_defaults():
    assert settings.DEFAULTS["perception.custom_patterns"] == []
    assert settings.DEFAULTS["perception.reinforce"] is True
    assert settings.DEFAULTS["perception.reinforce_min_amplification"] == 0.15
    snap = settings.load(object())
    assert snap["perception.custom_patterns"] == []
    assert snap["perception.reinforce"] is True
    assert snap["perception.reinforce_min_amplification"] == pytest.approx(0.15)


# -- custom merge (route B) --------------------------------------------------

def test_custom_fresh_name_encodes_own_lists():
    backend = FakeBackend()
    custom = _custom(prompt=["my prompt one", "my prompt two"],
                     agent=["my agent one"])
    bank = patterns.build_pattern_bank(backend, "v1", custom_patterns=[custom])
    assert "custom_x" in bank
    flat = [s for call in backend.calls for s in call]
    assert "my prompt one" in flat and "my prompt two" in flat
    assert "my agent one" in flat
    assert _norm(bank["custom_x"]["prompt_anchor"]) == pytest.approx(1.0)
    assert _norm(bank["custom_x"]["agent_anchor"]) == pytest.approx(1.0)
    assert set(DIMENSIONS) <= set(bank)  # preset base intact


def test_custom_same_name_beats_preset():
    backend = FakeBackend()
    preset_name = DIMENSIONS[0]
    custom = _custom(name=preset_name, prompt=["override prompt"],
                     agent=["override agent"])
    bank = patterns.build_pattern_bank(backend, "v1", custom_patterns=[custom])
    flat = [s for call in backend.calls for s in call]
    assert "override prompt" in flat and "override agent" in flat
    expected_p = patterns._mean_normalized([_vec("override prompt")])
    expected_a = patterns._mean_normalized([_vec("override agent")])
    assert bank[preset_name]["prompt_anchor"] == pytest.approx(expected_p)
    assert bank[preset_name]["agent_anchor"] == pytest.approx(expected_a)


def test_reinforced_beats_preset_but_loses_to_custom():
    backend = FakeBackend()
    preset_name = DIMENSIONS[0]
    re_anchor = [0.5] * DIM
    reinforced = {preset_name: {
        "prompt_anchor": [0.25] * DIM,
        "agent_anchor": re_anchor,
        "evidence": 3,
    }}
    bank = patterns.build_pattern_bank(backend, "v1", reinforced=reinforced)
    assert bank[preset_name]["agent_anchor"] == pytest.approx(re_anchor)
    assert bank[preset_name]["prompt_anchor"] == pytest.approx([0.25] * DIM)
    assert bank[preset_name]["evidence"] == 3

    custom = _custom(name=preset_name, prompt=["custom wins prompt"],
                     agent=["custom wins agent"])
    bank2 = patterns.build_pattern_bank(
        backend, "v1", custom_patterns=[custom], reinforced=reinforced)
    assert bank2[preset_name]["agent_anchor"] == pytest.approx(
        patterns._mean_normalized([_vec("custom wins agent")]))
    assert bank2[preset_name]["agent_anchor"] != pytest.approx(re_anchor)


def test_invalid_custom_entries_skipped_without_raising():
    backend = FakeBackend()
    bad = [
        {},
        {"name": "no_lists"},
        {"name": "bad_prompt", "prompt": "notalist", "agent": ["x"]},
        {"name": "bad_items", "prompt": [1, 2], "agent": ["x"]},
        {"name": "empty", "prompt": [], "agent": ["x"]},
        "not-a-dict",
        _custom(name="dup", prompt=["first prompt"], agent=["first agent"]),
        _custom(name="dup", prompt=["second prompt"], agent=["second agent"]),
        _custom(),
    ]
    bank = patterns.build_pattern_bank(backend, "v1", custom_patterns=bad)
    assert set(DIMENSIONS) <= set(bank)
    assert "custom_x" in bank
    assert "no_lists" not in bank and "bad_prompt" not in bank
    assert "bad_items" not in bank and "empty" not in bank
    flat = [s for call in backend.calls for s in call]
    assert "first prompt" in flat and "second prompt" not in flat
    # Empty / None inputs are fine too.
    assert patterns.build_pattern_bank(backend, "v1", custom_patterns=[]) \
        == patterns.build_pattern_bank(backend, "v1", custom_patterns=None)


def test_cache_independent_per_input_set_and_hits_identical():
    backend = FakeBackend()
    c1 = _custom(name="c_one")
    c2 = _custom(name="c_two")
    b1 = patterns.build_pattern_bank(backend, "v1", custom_patterns=[c1])
    n_after_b1 = len(backend.calls)
    # Identical inputs: cache hit, no new encode calls.
    b1_again = patterns.build_pattern_bank(backend, "v1", custom_patterns=[c1])
    assert len(backend.calls) == n_after_b1
    assert b1_again == b1
    # Different custom set on the same backend: independent anchors.
    b2 = patterns.build_pattern_bank(backend, "v1", custom_patterns=[c2])
    assert len(backend.calls) > n_after_b1
    assert "c_one" not in b2 and "c_two" in b2
    assert "c_one" in b1
    # Reinforced input also changes the key.
    re = {"directness": {"prompt_anchor": [1.0] * DIM,
                         "agent_anchor": [0.0] * DIM, "evidence": 1}}
    b3 = patterns.build_pattern_bank(backend, "v1", reinforced=re)
    assert b3["directness"]["agent_anchor"] == pytest.approx([0.0] * DIM)


# -- reinforce (route A) -----------------------------------------------------

def test_reinforce_single_step_exact_formula():
    entry = {"prompt_anchor": [1.0, 0.0], "agent_anchor": [1.0, 0.0],
             "evidence": 0}
    out = patterns.reinforce(entry, [0.0, 1.0])  # alpha defaults to 0.05
    assert patterns.REINFORCE_ALPHA == 0.05
    blended = [0.95 * 1.0 + 0.05 * 0.0, 0.95 * 0.0 + 0.05 * 1.0]
    n = math.sqrt(sum(x * x for x in blended))
    expected = [x / n for x in blended]
    assert out["agent_anchor"] == pytest.approx(expected, abs=1e-12)
    assert out["evidence"] == 1
    # Input not mutated.
    assert entry["agent_anchor"] == [1.0, 0.0] and entry["evidence"] == 0


def test_reinforce_repeated_approaches_reaction_direction():
    entry = {"prompt_anchor": [1.0, 0.0], "agent_anchor": [1.0, 0.0],
             "evidence": 0}
    reaction = [0.0, 1.0]
    cosines = []
    cur = entry
    for _ in range(5):
        cur = patterns.reinforce(cur, reaction)
        cosines.append(_cos(cur["agent_anchor"], reaction))
    assert all(b > a for a, b in zip(cosines, cosines[1:])), cosines
    assert cur["evidence"] == 5
    assert _norm(cur["agent_anchor"]) == pytest.approx(1.0)


def test_reinforce_prompt_anchor_byte_identical():
    entry = {"prompt_anchor": [0.3, -0.7, 0.123], "agent_anchor": [1.0, 0.0, 0.0],
             "evidence": 2}
    out = patterns.reinforce(entry, [0.0, 1.0, 0.0])
    assert out["prompt_anchor"] == entry["prompt_anchor"]
    assert out["prompt_anchor"] is not entry["prompt_anchor"]  # a copy


# -- gates -------------------------------------------------------------------

def test_reinforce_gates():
    import engine
    base = dict(settings.DEFAULTS)
    # Below threshold: no update.
    assert engine._reinforce_allowed(base, 0.14) is False
    assert engine._reinforce_allowed(base, None) is False
    # At/above threshold: update.
    assert engine._reinforce_allowed(base, 0.15) is True
    assert engine._reinforce_allowed(base, 0.9) is True
    # Disabled: no update regardless of amplification.
    off = dict(base, **{"perception.reinforce": False})
    assert engine._reinforce_allowed(off, 5.0) is False
    # Custom threshold honoured.
    hi = dict(base, **{"perception.reinforce_min_amplification": 0.5})
    assert engine._reinforce_allowed(hi, 0.3) is False
    assert engine._reinforce_allowed(hi, 0.5) is True


# -- persist round-trip ------------------------------------------------------

def test_persist_round_trip_and_monotonic_updated_at():
    conn = sqlite3.connect(":memory:")
    from storage import migration
    migration.schema_bootstrap(conn)
    entry = {"prompt_anchor": [1.0, 0.0, 0.5], "agent_anchor": [0.0, 1.0, -0.25],
             "evidence": 4}
    patterns.persist_reinforced(conn, {"directness": entry})
    loaded = patterns.load_reinforced(conn)
    assert set(loaded) == {"directness"}
    assert loaded["directness"]["prompt_anchor"] == pytest.approx(
        entry["prompt_anchor"], abs=1e-6)
    assert loaded["directness"]["agent_anchor"] == pytest.approx(
        entry["agent_anchor"], abs=1e-6)
    assert loaded["directness"]["evidence"] == 4

    ts1 = conn.execute(
        "SELECT updated_at FROM pattern_bank WHERE pattern_name='directness'"
    ).fetchone()[0]
    entry2 = dict(entry, evidence=5)
    patterns.persist_reinforced(conn, {"directness": entry2})
    ts2 = conn.execute(
        "SELECT updated_at FROM pattern_bank WHERE pattern_name='directness'"
    ).fetchone()[0]
    assert ts2 >= ts1
    assert patterns.load_reinforced(conn)["directness"]["evidence"] == 5
    conn.close()


def test_persist_closed_connection_logs_and_never_raises(caplog):
    conn = sqlite3.connect(":memory:")
    conn.close()
    entry = {"prompt_anchor": [1.0, 0.0], "agent_anchor": [1.0, 0.0],
             "evidence": 1}
    with caplog.at_level("ERROR"):
        patterns.persist_reinforced(conn, {"directness": entry})  # no raise
        assert patterns.load_reinforced(conn) == {}               # no raise
    assert caplog.records  # the failure was logged


# -- schema v6 ---------------------------------------------------------------

def test_schema_v6_pattern_bank_ddl(tmp_path):
    from storage import database, migration
    database.set_override(tmp_path / "v6.db")
    try:
        conn = database.connect()
        try:
            assert migration.schema_bootstrap(conn) is True
            assert migration.get_schema_version(conn) == migration.CURRENT_SCHEMA_VERSION
            assert migration.CURRENT_SCHEMA_VERSION == 8
            cols = conn.execute(
                "PRAGMA table_info(pattern_bank)").fetchall()
            names = [r[1] for r in cols]
            types = {r[1]: r[2].upper() for r in cols}
            assert names == ["pattern_name", "prompt_anchor", "agent_anchor",
                             "evidence", "updated_at"], names
            assert types["pattern_name"] == "TEXT"
            assert types["prompt_anchor"] == "BLOB"
            assert types["agent_anchor"] == "BLOB"
            assert types["evidence"] == "INTEGER"
            assert types["updated_at"] == "REAL"
            # PRIMARY KEY shape: pattern_name is the sole pk column.
            pk = [r[1] for r in cols if r[5]]
            assert pk == ["pattern_name"], pk
            defaults = {r[1]: r[4] for r in cols}
            assert defaults["evidence"] == "0"
            # No free-text column: no TEXT besides the bank-name key.
            text_cols = [n for n, t in types.items() if t == "TEXT"]
            assert text_cols == ["pattern_name"], text_cols
            notnull = {r[1]: r[3] for r in cols}
            assert notnull["prompt_anchor"] == 1
            assert notnull["agent_anchor"] == 1
            assert notnull["updated_at"] == 1
        finally:
            conn.close()
    finally:
        database.set_override(None)
