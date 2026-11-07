"""E1 reader-side completeness: /portrait perception fallback serves the
LATEST ROW PER top_pattern (deterministic per-name ts desc, id desc
tie-break) instead of only the 4 most recent rows, so a restart-time
pane surfaces every pattern that ever won a turn, each from its own
most recent row. Honesty holds: a pattern with zero persisted wins has
no entry ('-' in the UI); the overall-latest entry keeps the legacy
card's repeated-window semantics; faults degrade to the documented
empty shape, never a 500.

Byte-sized: in-memory/file sqlite, direct SQL seeds, no network.
"""
import importlib
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
from storage import database, migration  # noqa: E402


_D = (  # (ts, top_pattern, p_aff, a_aff, margin, ampl, evidence, conf, rep)
    (1000.0, "exploration", 0.30, 0.50, 0.05, 0.10, 1, 0.40, 0),
    (1001.0, "analytical_depth", 0.40, 0.60, 0.05, 0.10, 1, 0.40, 0),
    (1002.0, "directness", 0.50, 0.70, 0.05, 0.10, 1, 0.40, 0),
    (1003.0, "scope", 0.60, 0.80, 0.05, 0.10, 1, 0.40, 0),
    (1004.0, "context_richness", 0.50, 0.90, 0.05, 0.10, 1, 0.40, 0),
    (1005.0, "context_richness", 0.55, 0.92, 0.06, 0.10, 2, 0.50, 0),
    (1006.0, "context_richness", 0.58, 0.95, 0.07, 0.10, 3, 0.60, 0),
    (1007.0, "context_richness", 0.61, 0.99, 0.08, 0.20, 4, 0.70, 0),
)


def _api(db):
    import dashboard.plugin_api as api
    importlib.reload(api)
    api.set_db_path(db)
    return api


def _seed(db, rows):
    conn = sqlite3.connect(str(db))
    try:
        migration.schema_bootstrap(conn)
        for r in rows:
            conn.execute(
                "INSERT INTO pattern_affinity_history "
                "(ts, top_pattern, top_prompt_affinity, top_agent_affinity,"
                " top_margin, top_amplification, evidence, confidence,"
                " repeated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", r)
        conn.commit()
    finally:
        conn.close()


def _perc(db, monkeypatch, extra_rows=()):
    api = _api(db)
    monkeypatch.setattr(api, "_portrait_live_view", lambda: None)
    monkeypatch.setattr(api, "_engine_first", lambda *a, **k: (None, None))
    shape = api._portrait_shape()
    return shape.get("perception", {}), shape


def test_latest_row_per_pattern_live_shape(tmp_path, monkeypatch):
    db = tmp_path / "e1.db"
    _seed(db, _D)
    perc, _ = _perc(db, monkeypatch)
    assert perc["perception_available"] is True
    names = [p["name"] for p in perc["patterns"]]
    # (a) every name with >=1 win gets an entry from its OWN latest row
    assert names == ["context_richness", "scope", "directness",
                     "analytical_depth", "exploration"], names
    by = {p["name"]: p for p in perc["patterns"]}
    assert by["context_richness"] == {
        "name": "context_richness", "prompt_affinity": 0.61,
        "agent_affinity": 0.99, "margin": 0.08, "amplification": 0.20,
        "evidence": 4, "confidence": 0.70, "repeated": True}
    assert by["scope"] == {
        "name": "scope", "prompt_affinity": 0.60,
        "agent_affinity": 0.80, "margin": 0.05, "amplification": 0.10,
        "evidence": 1, "confidence": 0.40, "repeated": False}
    assert by["exploration"]["confidence"] == 0.40
    # (b) a pattern that never won a turn has NO entry (honest '-')
    assert "iteration" not in names
    # (c) the strongest-card source = overall-latest row
    assert perc["patterns"][0]["name"] == "context_richness"
    assert perc["patterns"][0]["repeated"] is True


def test_empty_table_documented_shape(tmp_path, monkeypatch):
    db = tmp_path / "empty.db"
    _seed(db, ())
    perc, _ = _perc(db, monkeypatch)
    assert perc["perception_available"] is False
    assert perc["patterns"] == []


def test_missing_table_degrades_no_raise(tmp_path, monkeypatch):
    db = tmp_path / "notable.db"
    sqlite3.connect(str(db)).close()  # zero tables
    perc, _ = _perc(db, monkeypatch)
    assert perc["perception_available"] is False
    assert perc["patterns"] == []


def test_db_fault_degrades_no_raise(tmp_path, monkeypatch):
    # Point the resolver at a directory: _database.connect() cannot open
    # it ("unable to open database file") -> the connect fault path in
    # _portrait_db_fallback returns the already-empty perception shape.
    adir = tmp_path / "not_a_db"
    adir.mkdir()
    api = _api(adir)
    monkeypatch.setattr(api, "_portrait_live_view", lambda: None)
    monkeypatch.setattr(api, "_engine_first", lambda *a, **k: (None, None))
    shape = api._portrait_shape()  # must not raise, must not 500
    perc = shape["perception"]
    assert perc["perception_available"] is False
    assert perc["patterns"] == []


def test_deterministic_two_calls(tmp_path, monkeypatch):
    db = tmp_path / "det.db"
    _seed(db, _D)
    p1, _ = _perc(db, monkeypatch)
    p2, _ = _perc(db, monkeypatch)
    assert p1 == p2
    # byte-stable ordering: ts desc of the per-name latest row
    assert [p["name"] for p in p2["patterns"]] == [
        "context_richness", "scope", "directness",
        "analytical_depth", "exploration"]


def test_prune_cap_512_hides_oldest(tmp_path, monkeypatch):
    db = tmp_path / "cap.db"
    cap = migration.PATTERN_AFFINITY_HISTORY_MAX
    conn = sqlite3.connect(str(db))
    try:
        migration.schema_bootstrap(conn)
        for i in range(cap + 1):
            conn.execute(
                "INSERT INTO pattern_affinity_history "
                "(ts, top_pattern, top_prompt_affinity, "
                "top_agent_affinity, top_margin, top_amplification, "
                "evidence, confidence, repeated) "
                "VALUES (?, 'context_richness', ?, ?, ?, ?, ?, ?, 0)",
                (float(i), 0.1 * (i % 10), 0.2 * (i % 10),
                 0.05, 0.1, i, 0.01 * i))
        conn.commit()
        migration.prune_pattern_affinity_history(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM pattern_affinity_history").fetchone()[0] \
            == cap
    finally:
        conn.close()
    perc, _ = _perc(db, monkeypatch)
    assert perc["perception_available"] is True
    p = perc["patterns"][0]
    # the pruned oldest row (i=0: evidence 0, confidence 0.0) is
    # invisible; the newest surviving row (i=cap) is what surfaces
    assert p["evidence"] == cap
    assert p["confidence"] == pytest.approx(0.01 * cap)
