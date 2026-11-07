"""S10 reinforced-bank dirty set (L10, L5 open item (a)).

In-memory sqlite + byte-sized fake backend only. Counting connection
proves scored turns never touch pattern_bank after bootstrap.
"""
import hashlib
import sqlite3

import pytest

import engine
from perception import patterns
from storage import migration

DIM = 8


class FakeBackend:
    """Deterministic hash fake; records no DB interaction."""

    def __init__(self, model_version="test-backend-v1"):
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


class CountingConn:
    """Wraps one in-memory sqlite conn; records every SQL string."""

    def __init__(self, real):
        self._real = real
        self.sql = []

    def execute(self, statement, params=()):
        self.sql.append(statement)
        return self._real.execute(statement, params)

    def executemany(self, statement, params=()):
        self.sql.append(statement)
        return self._real.executemany(statement, params)

    def executescript(self, script):
        self.sql.append(script)
        return self._real.executescript(script)

    def commit(self):
        return self._real.commit()

    def close(self):
        pass  # shared in-memory conn stays alive for the test

    @property
    def pattern_bank_sql(self):
        return [s for s in self.sql if "pattern_bank" in s]

    def __getattr__(self, name):
        return getattr(self._real, name)


class _FakeDB:
    def __init__(self, wrapper):
        self._w = wrapper

    def connect(self):
        return self._w


def _seed_db(entry=None):
    """Real in-memory DB, migrated, optionally one reinforced row."""
    real = sqlite3.connect(":memory:")
    migration.schema_bootstrap(real)
    if entry is not None:
        patterns.persist_reinforced(real, {"directness": entry})
        real.commit()
    return real


def _entry(evidence=3, prompt=None, agent=None):
    return {
        "prompt_anchor": prompt if prompt is not None else
        [1.0] + [0.0] * (DIM - 1),
        "agent_anchor": agent if agent is not None else
        [0.0, 1.0] + [0.0] * (DIM - 2),
        "evidence": evidence,
    }


def _engine(monkeypatch, wrapper, backend=None):
    eng = engine.Engine()
    eng._backend = backend or FakeBackend()
    monkeypatch.setattr(
        engine.Engine, "_storage",
        staticmethod(lambda: (_FakeDB(wrapper), migration)))
    return eng


@pytest.fixture(autouse=True)
def _fresh_bank_cache():
    patterns._BANK_CACHE.clear()
    yield
    patterns._BANK_CACHE.clear()


def test_bootstrap_loads_persisted_bank_and_scored_turn_reuses_cache(
        monkeypatch):
    real = _seed_db(_entry(evidence=3))
    wrapper = CountingConn(real)
    eng = _engine(monkeypatch, wrapper)
    wrapper.sql.clear()

    eng._update_patterns_and_persist(_vec("hello"), None, 1000.0)

    assert eng._reinforced_loaded is True
    assert set(eng._reinforced_cache) == {"directness"}
    assert eng._reinforced_cache["directness"]["evidence"] == 3
    assert eng._reinforced_dirty == set()
    # Bootstrap did read the table (once).
    assert wrapper.pattern_bank_sql

    wrapper.sql.clear()
    eng._update_patterns_and_persist(_vec("hello again"), None, 1001.0)
    assert wrapper.pattern_bank_sql == []  # no per-turn DB read
    assert eng._reinforced_cache["directness"]["evidence"] == 3


def test_scored_turn_without_persistence_never_reads_pattern_bank(
        monkeypatch):
    real = _seed_db(_entry(evidence=1))
    wrapper = CountingConn(real)
    eng = _engine(monkeypatch, wrapper)
    eng._update_patterns_and_persist(_vec("first"), None, 1000.0)
    assert eng._reinforced_loaded is True

    wrapper.sql.clear()
    for i in range(3):
        eng._update_patterns_and_persist(_vec(f"turn {i}"), None, 1001.0 + i)
    assert wrapper.pattern_bank_sql == []
    assert eng._reinforced_dirty == set()


def test_reinforce_updates_cache_in_place_next_turn_uses_fresh_vectors(
        monkeypatch):
    real = _seed_db(_entry(evidence=3))
    wrapper = CountingConn(real)
    backend = FakeBackend()
    eng = _engine(monkeypatch, wrapper, backend)
    eng._update_patterns_and_persist(_vec("bootstrap"), None, 1000.0)
    before = {k: dict(v) for k, v in eng._reinforced_cache.items()}
    old_agent = list(before["directness"]["agent_anchor"])

    bank = patterns.build_pattern_bank(
        backend, backend.model_version,
        reinforced=eng._reinforced_cache)
    reaction = [1.0] + [0.0] * (DIM - 1)  # orthogonal-ish to old agent
    eng._maybe_reinforce_and_persist(
        bank, {"name": "directness", "amplification": 1.0}, reaction,
        patterns)

    assert eng._reinforced_dirty == {"directness"}
    fresh = eng._reinforced_cache["directness"]
    assert fresh["evidence"] == before["directness"]["evidence"] + 1
    assert fresh["agent_anchor"] != old_agent  # moved in place
    expected = patterns.reinforce(before["directness"], reaction)
    assert fresh["agent_anchor"] == pytest.approx(
        expected["agent_anchor"], abs=1e-9)
    # The DB row agrees (persist happened); the NEXT turn must not read.
    assert patterns.load_reinforced(real)["directness"]["evidence"] == \
        fresh["evidence"]

    captured = {}
    real_build = patterns.build_pattern_bank

    def _spy(backend_, model_version=None, data_dir=None,
             custom_patterns=None, reinforced=None):
        # Copy so later cache mutations cannot rewrite the capture.
        captured["reinforced"] = {
            k: dict(v) for k, v in (reinforced or {}).items()}
        return real_build(backend_, model_version, data_dir=data_dir,
                          custom_patterns=custom_patterns,
                          reinforced=reinforced)

    monkeypatch.setattr(patterns, "build_pattern_bank", _spy)
    wrapper.sql.clear()
    eng._update_patterns_and_persist(_vec("next"), None, 1002.0)

    assert wrapper.pattern_bank_sql == []
    seen = captured["reinforced"]["directness"]
    assert seen["agent_anchor"] == pytest.approx(
        fresh["agent_anchor"], abs=1e-9)
    assert seen["evidence"] == fresh["evidence"]
    assert seen["prompt_anchor"] == pytest.approx(
        fresh["prompt_anchor"], abs=1e-9)


def test_fresh_engine_sees_persisted_values_and_staleness_is_bootstrap_only(
        monkeypatch):
    real = _seed_db(_entry(evidence=2))
    wrapper = CountingConn(real)
    eng1 = _engine(monkeypatch, wrapper)
    eng1._update_patterns_and_persist(_vec("b"), None, 1000.0)
    bank = patterns.build_pattern_bank(
        eng1._backend, eng1._backend.model_version,
        reinforced=eng1._reinforced_cache)
    eng1._maybe_reinforce_and_persist(
        bank, {"name": "directness", "amplification": 1.0},
        [1.0] + [0.0] * (DIM - 1), patterns)
    persisted = patterns.load_reinforced(real)["directness"]

    # Fresh engine over the same DB: bootstrap sees persisted values.
    eng2 = _engine(monkeypatch, wrapper, FakeBackend())
    eng2._update_patterns_and_persist(_vec("b2"), None, 1001.0)
    assert eng2._reinforced_dirty == set()
    assert eng2._reinforced_cache["directness"]["evidence"] == \
        persisted["evidence"]
    assert eng2._reinforced_cache["directness"]["agent_anchor"] == \
        pytest.approx(persisted["agent_anchor"], abs=1e-6)

    # Cross-process staleness: an external persist is invisible to eng2
    # until bootstrap/reset — no mid-process re-read.
    external = patterns.reinforce(
        persisted, [0.0, 0.0, 1.0] + [0.0] * (DIM - 3))
    patterns.persist_reinforced(real, {"directness": external})
    cached_evidence = eng2._reinforced_cache["directness"]["evidence"]
    wrapper.sql.clear()
    eng2._update_patterns_and_persist(_vec("stale check"), None, 1002.0)
    assert wrapper.pattern_bank_sql == []
    assert eng2._reinforced_cache["directness"]["evidence"] == cached_evidence


def test_closed_connection_bootstrap_degrades_to_empty(monkeypatch, caplog):
    real = sqlite3.connect(":memory:")
    real.close()

    class _DeadDB:
        def connect(self):
            return real  # closed conn: execute() raises

    eng = engine.Engine()
    eng._backend = FakeBackend()
    monkeypatch.setattr(
        engine.Engine, "_storage",
        staticmethod(lambda: (_DeadDB(), migration)))
    with caplog.at_level("ERROR"):
        # Direct bootstrap: never raises, degrades to {}.
        bank = eng._reinforced_bank(patterns)
        assert bank == {}
        # And a scored turn on top of it does not raise either.
        eng._update_patterns_and_persist(_vec("x"), None, 1000.0)
    assert eng._reinforced_cache == {}
    assert caplog.records


def test_equivalence_bank_from_cache_equals_bank_from_live_db():
    real = _seed_db(None)
    patterns.persist_reinforced(real, {"directness": _entry(evidence=4)})
    real.commit()
    live = patterns.load_reinforced(real)
    assert set(live) == {"directness"}
    # Cache side: the same dict bootstrap would have stored (exact copy).
    cache = {k: {"prompt_anchor": list(v["prompt_anchor"]),
                 "agent_anchor": list(v["agent_anchor"]),
                 "evidence": v["evidence"],
                 "updated_at": v["updated_at"]}
             for k, v in live.items()}
    backend = FakeBackend()
    patterns._BANK_CACHE.clear()
    from_cache = patterns.build_pattern_bank(
        backend, "v1", reinforced=cache)
    patterns._BANK_CACHE.clear()
    from_db = patterns.build_pattern_bank(
        backend, "v1", reinforced=live)
    assert set(from_cache) == set(from_db)
    for name in from_db:
        c, d = from_cache[name], from_db[name]
        assert c["prompt_anchor"] == pytest.approx(
            d["prompt_anchor"], abs=1e-9)
        assert c["agent_anchor"] == pytest.approx(
            d["agent_anchor"], abs=1e-9)
        assert c["evidence"] == d["evidence"]
        assert c["updated_at"] == d["updated_at"]


def test_reset_clears_dirty_set_and_rereads(monkeypatch):
    real = _seed_db(_entry(evidence=2))
    wrapper = CountingConn(real)
    eng = _engine(monkeypatch, wrapper)
    eng._update_patterns_and_persist(_vec("b"), None, 1000.0)
    bank = patterns.build_pattern_bank(
        eng._backend, eng._backend.model_version,
        reinforced=eng._reinforced_cache)
    eng._maybe_reinforce_and_persist(
        bank, {"name": "directness", "amplification": 1.0},
        [1.0] + [0.0] * (DIM - 1), patterns)
    assert eng._reinforced_dirty == {"directness"}

    # External change while eng is running (invisible until re-bootstrap).
    external = _entry(
        evidence=99,
        agent=[0.0, 0.0, 1.0] + [0.0] * (DIM - 3))
    patterns.persist_reinforced(real, {"directness": external})
    real.commit()
    assert eng._reinforced_cache["directness"]["evidence"] != 99

    eng.reset()
    assert eng._reinforced_dirty == set()
    assert eng._reinforced_loaded is True
    assert eng._reinforced_cache["directness"]["evidence"] == 99
