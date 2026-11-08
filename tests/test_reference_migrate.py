"""L-MIGRATE: the rolling reference persists to the meta table + rehydrates
across a process restart. Reversible: an absent row rolls back to the
seed-bank cold start (never a 500)."""
import math

from perception import reference as ref_mod
from perception.reference import RollingReference


def _n(v):
    s = math.sqrt(sum(x * x for x in v))
    return [x / s for x in v]


def _booted_conn(path):
    import storage.database as db
    import storage.migration as mig
    db.set_override(str(path))
    conn = db.connect()
    mig.schema_bootstrap(conn)
    return conn


def test_save_load_state_round_trip(tmp_path):
    conn = _booted_conn(tmp_path / "ref.db")
    ref = RollingReference(seed_bank={"directness": [_n([1, 0, 0])]})
    ref.reassemble("directness", _n([0.9, 0.1, 0]), ts=1.0)
    ref_mod.save_state(conn, ref.to_state())
    conn.commit()
    loaded = ref_mod.load_state(conn)
    assert loaded is not None
    back = RollingReference(state=loaded)
    # The reassembled (live) window survived the round-trip.
    assert back.windows["directness"].live_count == 1
    assert back.windows["directness"].seeded is False
    # An untouched dim stays empty (no fabricated vectors).
    assert back.windows["scope"].live_count == 0


def test_load_state_empty_db_returns_none(tmp_path):
    conn = _booted_conn(tmp_path / "empty.db")
    assert ref_mod.load_state(conn) is None


def test_engine_persist_and_rehydrate(tmp_path):
    import engine
    import storage.database as db

    db.set_override(str(tmp_path / "eng.db"))

    def _seed_anchors():
        return {d: {"positive": [_n([1, 0, 0])], "negative": []}
                for d in ("directness", "scope")}

    # Process 1: seed + reassemble + persist the reassembled window.
    e1 = engine.Engine()
    ref1 = e1._ensure_reference(backend=None, anchor_vectors=_seed_anchors())
    ref1.reassemble("directness", _n([0.9, 0.1, 0]), ts=1.0)
    e1._persist_reference_state()

    # Process 2 (fresh engine): rehydrates the PERSISTED window, not the seed.
    e2 = engine.Engine()
    ref2 = e2._ensure_reference(backend=None, anchor_vectors=_seed_anchors())
    assert ref2 is not None
    assert ref2.windows["directness"].live_count == 1
    assert ref2.windows["directness"].seeded is False


def test_engine_no_persisted_state_seeds_from_bank(tmp_path):
    import engine
    import storage.database as db

    db.set_override(str(tmp_path / "fresh.db"))
    anchors = {d: {"positive": [_n([0, 0, 1])], "negative": []}
               for d in ("scope",)}
    eng = engine.Engine()
    ref = eng._ensure_reference(backend=None, anchor_vectors=anchors)
    # Nothing persisted -> cold-start seed from the bank.
    assert ref.windows["scope"].seeded is True
    assert ref.windows["scope"].live_count == 0
    assert ref.windows["scope"].vectors()[0] == _n([0, 0, 1])
