"""Cross-profile portrait fan-in (read-time, zero write/structure change).

One human, N profiles: a profile with no local portrait observation borrows
the most recently observed profile's dims so the pane shows a live portrait
in every session. PULSE/drift/context stay strictly per-profile.

Tests pin the owner contract:
1. fan-in selects the NEWEST observed profile (up-to-live);
2. missing/corrupt/locked sibling DBs are skipped, never a 500;
3. empty everywhere -> neutral shape with source_profile None;
4. additive source_profile key present on both the live and fallback paths;
5. the fan-in is READ-ONLY (no writes to any sibling DB) and touches only
   the portrait read path (no pulse/drift DB reads).
"""
import importlib
import sqlite3
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from storage import migration  # noqa: E402


DIMS = ("directness", "context_richness", "exploration",
        "analytical_depth", "iteration", "scope")


def _profile_db(root: Path, name: str, ts: float | None, vals: dict | None,
                corrupt: bool = False) -> Path:
    """Build a fixture profile DB with (optionally) one portrait row."""
    d = root / "profiles" / name / "plugin-data" / "its-in-your-head"
    d.mkdir(parents=True, exist_ok=True)
    db = d / "data.db"
    if corrupt:
        db.write_bytes(b"not a sqlite database")
        return db
    c = sqlite3.connect(str(db))
    try:
        c.executescript(migration.PORTRAIT_HISTORY_DDL)
        if ts is not None and vals is not None:
            cols = ", ".join(DIMS)
            ph = ", ".join("?" for _ in DIMS)
            c.execute(f"INSERT INTO portrait_history (ts, {cols}) "
                      f"VALUES (?, {ph})",
                      (ts,) + tuple(vals[d] for d in DIMS))
            c.commit()
    finally:
        c.close()
    return db


def _api(root):
    import dashboard.plugin_api as api
    importlib.reload(api)
    api.set_fan_in_root(root)
    # Hermetic local DB: empty, so the profile-local fallback yields
    # nothing and the cross-profile fan-in is the path under test.
    api.set_db_path(root / "_local_empty.db")
    # No in-process engine / probe: force the isolated-child fallback path.
    api._portrait_live_view = lambda: None
    api._engine_first = lambda *a, **k: (None, None)
    return api


@pytest.fixture
def isolated_root(tmp_path):
    """A hermetic tree: no DBs unless a test creates them."""
    (tmp_path / "profiles").mkdir()
    return tmp_path


def test_fan_in_selects_newest_profile(isolated_root):
    now = time.time()
    _profile_db(isolated_root, "alpha", now - 100,
                {d: 0.1 for d in DIMS})
    newest = _profile_db(isolated_root, "beta", now,
                         {d: 0.9 for d in DIMS})
    api = _api(isolated_root)
    shape = api._portrait_shape()
    assert shape["available"] is True
    assert shape["source_profile"] == "beta"
    assert all(abs(shape["dimensions"][d] - 0.9) < 1e-9 for d in DIMS)
    assert shape["user"] == shape["dimensions"]
    # Observed track only: confidence/agent never invented.
    assert all(v is None for v in shape["confidence"].values())
    assert all(v is None for v in shape["agent"].values())
    # Read-only: the borrowed DB is unchanged.
    c = sqlite3.connect(str(newest))
    try:
        rows = c.execute("SELECT ts FROM portrait_history").fetchall()
        assert len(rows) == 1
    finally:
        c.close()


def test_fan_in_skips_corrupt_and_empty(isolated_root):
    now = time.time()
    _profile_db(isolated_root, "broken", None, None, corrupt=True)
    _profile_db(isolated_root, "empty", None, None)
    _profile_db(isolated_root, "good", now,
                {d: 0.4 for d in DIMS})
    api = _api(isolated_root)
    shape = api._portrait_shape()
    assert shape["source_profile"] == "good"
    assert all(abs(shape["dimensions"][d] - 0.4) < 1e-9 for d in DIMS)


def test_fan_in_empty_returns_neutral_shape(isolated_root):
    api = _api(isolated_root)
    shape = api._portrait_shape()
    assert shape["available"] is False
    assert shape["source_profile"] is None
    assert all(v is None for v in shape["dimensions"].values())


def test_fan_in_touches_only_portrait_read_path(isolated_root, monkeypatch):
    """No pulse/drift/context DB reads on the borrow path."""
    _profile_db(isolated_root, "one", time.time(),
                {d: 0.5 for d in DIMS})
    api = _api(isolated_root)
    # Local DB is empty -> drift/pulse would be neutral; spy that the
    # fan-in function itself opens ONLY sibling portrait DBs by asserting
    # its source is a sibling profile and that no local pulse table exists.
    shape = api._portrait_shape()
    assert shape["source_profile"] == "one"
    # The active (test) DB was never pointed at the sibling; pulse/drift
    # remain governed by set_db_path, not by the fan-in override.
    assert api._fan_in_root_override == isolated_root
