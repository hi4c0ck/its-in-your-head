"""E3 reader-side: the dashboard fallback is rank-aware on a v7 table.

* per-name entry = that name's latest row at ANY rank (runner-up
  evidence now surfaces under its name);
* strongest card (first entry) = the latest RANK-1 row;
* repeated window counts RANK-1 rows only (repeated = WON the top);
* a hand-crafted table with no rank-1 row falls back to the E1
  ordering (ts desc), never a 500;
* all-rank-1 (legacy shape) data behaves exactly as pre-E3.

Seeds raw v7 rows (rank + turn_key) and drives the fallback through
the same seam as s13: ``_portrait_live_view`` stubbed to None,
``_engine_first`` stubbed, direct ``_portrait_shape`` read.
Byte-sized: file sqlite only.
"""
import importlib
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
from storage import migration  # noqa: E402

# Ground-truth = the exact s13 seed (s13._D), extended with rank +
# turn_key (all rank 1: pre-v7 shape).
s13_D = (
    (1000.0, "exploration", 0.30, 0.50, 0.05, 0.10, 1, 0.40, 0, 1,
     "tk1000"),
    (1001.0, "analytical_depth", 0.40, 0.60, 0.05, 0.10, 1, 0.40, 0, 1,
     "tk1001"),
    (1002.0, "directness", 0.50, 0.70, 0.05, 0.10, 1, 0.40, 0, 1,
     "tk1002"),
    (1003.0, "scope", 0.60, 0.80, 0.05, 0.10, 1, 0.40, 0, 1, "tk1003"),
    (1004.0, "context_richness", 0.50, 0.90, 0.05, 0.10, 1, 0.40, 0, 1,
     "tk1004"),
    (1005.0, "context_richness", 0.55, 0.92, 0.06, 0.10, 2, 0.50, 0, 1,
     "tk1005"),
    (1006.0, "context_richness", 0.58, 0.95, 0.07, 0.10, 3, 0.60, 0, 1,
     "tk1006"),
    (1007.0, "context_richness", 0.61, 0.99, 0.08, 0.20, 4, 0.70, 0, 1,
     "tk1007"),
)


def _api(db):
    import dashboard.plugin_api as api
    importlib.reload(api)
    api.set_db_path(db)
    return api


def _seed(db, rows):
    """rows: (ts, name, prompt_aff, agent_aff, margin, amp, evidence,
    confidence, repeated, rank, turn_key)."""
    conn = sqlite3.connect(str(db))
    try:
        migration.schema_bootstrap(conn)
        for r in rows:
            conn.execute(
                "INSERT INTO pattern_affinity_history "
                "(ts, top_pattern, top_prompt_affinity, "
                "top_agent_affinity, top_margin, top_amplification, "
                "evidence, confidence, repeated, rank, turn_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)", r)
        conn.commit()
    finally:
        conn.close()


def _perc(db, monkeypatch):
    api = _api(db)
    monkeypatch.setattr(api, "_portrait_live_view", lambda: None)
    monkeypatch.setattr(api, "_engine_first", lambda *a, **k: (None, None))
    shape = api._portrait_shape()
    return shape.get("perception", {}), shape


def test_same_ts_triple_card_is_latest_rank1(tmp_path, monkeypatch):
    """Turn T2 (ts 1002) writes r1=A, r2=B, r3=C; T1 (ts 1001) r1=A.
    The card must be T2's rank-1 row (A), NOT the newest-inserted
    row (C) — id-desc alone would surface a runner-up."""
    db = tmp_path / "db1.db"
    _seed(db, [
        (1001.0, "A", 0.1, 0.5, 0.1, 0.4, 1, 0.5, 0, 1, "tk1"),
        (1002.0, "A", 0.2, 0.8, 0.2, 0.6, 2, 0.7, 0, 1, "tk2"),
        (1002.0, "B", 0.3, 0.6, 0.1, 0.3, 3, 0.6, 0, 2, "tk2"),
        (1002.0, "C", 0.4, 0.5, 0.0, 0.1, 4, 0.4, 0, 3, "tk2"),
    ])
    p, _ = _perc(db, monkeypatch)
    assert p["perception_available"] is True
    # ts desc, id desc: within tk2 the later-inserted C sorts first;
    # the card is A (latest rank-1 row), then B's/C's own latest rows
    assert [e["name"] for e in p["patterns"]] == ["A", "C", "B"]
    card = p["patterns"][0]
    assert card["name"] == "A"
    assert card["evidence"] == 2
    assert card["confidence"] == pytest.approx(0.7)
    assert card["repeated"] is True    # A won T1 (rank-1 window)
    b, c = p["patterns"][2], p["patterns"][1]
    assert b["evidence"] == 3 and c["evidence"] == 4
    assert b["repeated"] is False and c["repeated"] is False


def test_per_name_latest_at_any_rank(tmp_path, monkeypatch):
    """B's newest row is a RANK-2 row (T2 r2) — B's entry must come
    from it, even though B also has an older rank-1 row (T1)."""
    db = tmp_path / "db2.db"
    _seed(db, [
        (1001.0, "B", 0.1, 0.2, 0.1, 0.1, 1, 0.3, 0, 1, "tk1"),
        (1002.0, "D", 0.1, 0.9, 0.2, 0.8, 2, 0.9, 0, 1, "tk2"),
        (1002.0, "B", 0.3, 0.5, 0.1, 0.2, 3, 0.5, 0, 2, "tk2"),
    ])
    p, _ = _perc(db, monkeypatch)
    assert [e["name"] for e in p["patterns"]] == ["D", "B"]
    b = next(e for e in p["patterns"] if e["name"] == "B")
    assert b["evidence"] == 3          # the rank-2 row, not T1's rank-1
    assert b["confidence"] == pytest.approx(0.5)
    assert b["repeated"] is True       # B's older rank-1 row is in window


def test_repeated_counts_rank1_only(tmp_path, monkeypatch):
    """B appears as rank-2 inside the window — that is NOT 'won the
    top'. D appears as rank-1 in two turns — repeated True. The
    repeated flag must be driven by rank-1 rows ONLY."""
    db = tmp_path / "db3.db"
    _seed(db, [
        (1001.0, "D", 0.1, 0.9, 0.2, 0.8, 1, 0.9, 0, 1, "tk1"),
        (1002.0, "A", 0.1, 0.9, 0.2, 0.8, 1, 0.9, 0, 1, "tk2"),
        (1002.0, "B", 0.2, 0.7, 0.1, 0.5, 2, 0.8, 0, 2, "tk2"),
        (1003.0, "D", 0.1, 0.9, 0.2, 0.8, 2, 0.9, 0, 1, "tk3"),
        (1003.0, "B", 0.2, 0.7, 0.1, 0.5, 3, 0.8, 0, 2, "tk3"),
        (1003.0, "C", 0.3, 0.5, 0.0, 0.2, 1, 0.5, 0, 3, "tk3"),
    ])
    p, _ = _perc(db, monkeypatch)
    by = {e["name"]: e for e in p["patterns"]}
    assert by["D"]["repeated"] is True   # D rank-1 in T1 and T3
    assert by["B"]["repeated"] is False  # B only ever rank-2 -> never "top"
    assert by["A"]["repeated"] is False  # A's window holds D's rank-1 only


def test_no_rank1_rows_falls_back_to_ts_order(tmp_path, monkeypatch):
    """Hand-crafted runner-up-only data: no 500, E1 ordering."""
    db = tmp_path / "db4.db"
    _seed(db, [
        (1001.0, "B", 0.2, 0.7, 0.1, 0.5, 2, 0.8, 0, 2, "tk1"),
        (1002.0, "C", 0.3, 0.5, 0.0, 0.2, 1, 0.5, 0, 3, "tk2"),
    ])
    p, _ = _perc(db, monkeypatch)
    assert [e["name"] for e in p["patterns"]] == ["C", "B"]
    assert all(e["repeated"] is False for e in p["patterns"])


def test_legacy_all_rank1_identical_to_s13(tmp_path, monkeypatch):
    """All-rank-1 data must reproduce the pre-E3 (s13) result exactly."""
    db = tmp_path / "db5.db"
    _seed(db, s13_D)
    p, _ = _perc(db, monkeypatch)
    assert p["perception_available"] is True
    # s13 (pre-E3) ground-truth, byte-identical: the all-rank-1 seed
    # must reproduce the exact E1/E13 entries.
    assert p["patterns"] == [
        {"name": "context_richness", "prompt_affinity": 0.61,
         "agent_affinity": 0.99, "margin": 0.08, "amplification": 0.2,
         "evidence": 4, "confidence": 0.7, "repeated": True},
        {"name": "scope", "prompt_affinity": 0.6, "agent_affinity": 0.8,
         "margin": 0.05, "amplification": 0.1, "evidence": 1,
         "confidence": 0.4, "repeated": False},
        {"name": "directness", "prompt_affinity": 0.5,
         "agent_affinity": 0.7, "margin": 0.05, "amplification": 0.1,
         "evidence": 1, "confidence": 0.4, "repeated": False},
        {"name": "analytical_depth", "prompt_affinity": 0.4,
         "agent_affinity": 0.6, "margin": 0.05, "amplification": 0.1,
         "evidence": 1, "confidence": 0.4, "repeated": False},
        {"name": "exploration", "prompt_affinity": 0.3,
         "agent_affinity": 0.5, "margin": 0.05, "amplification": 0.1,
         "evidence": 1, "confidence": 0.4, "repeated": False},
    ]
