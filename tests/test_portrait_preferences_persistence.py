"""Portrait UI preferences persist durably (DRAWING template + INK toggle).

Owner-reported bug: portrait selections did not survive restarts/session
changes. Root causes: (1) the INK toggle was module-memory only (no
persistence door at all); (2) the DRAWING template lived ONLY in renderer
localStorage, which the desktop app quarantines on multi-instance lock
contention (observed: the user's selection sat in Local Storage.stuck2
while the live store held the default).

Fix contract pinned here: portrait.template / portrait.ink are settings
keys whose SOURCE OF TRUTH is the profile-DB user-data store — POST
/settings writes them durably, GET /settings exposes the merged values
AND the raw override document ("user") the pane hydrates from, and a
fresh client (restart) against the same DB reads them back. The isolated
child's fallback key set must accept them too (the encoder-switcher
"unknown settings keys" bug class).
"""
import importlib
import sqlite3
import sys

import pytest

import dashboard.plugin_api as api
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(tmp_path):
    importlib.reload(api)
    api._ephemeral_settings.clear()
    api.set_db_path(tmp_path / "data.db")
    api.set_ctx(None)
    db = sqlite3.connect(tmp_path / "data.db")
    db.executescript(api._DDL)
    db.close()
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_portrait_keys_roundtrip_and_survive_restart(tmp_path):
    c = _client(tmp_path)
    # Cold read: defaults present, NO user overrides yet.
    body = c.get("/settings").json()
    assert body["settings"]["portrait.template"] == "neutral-pencil"
    assert body["settings"]["portrait.ink"] is False
    assert body["user"] == {}
    assert body["portrait"]["template"] == "neutral-pencil"  # nested walk

    r = c.post("/settings", json={"settings": {
        "portrait.template": "soft-flow-study", "portrait.ink": True}})
    assert r.status_code == 200
    out = r.json()
    assert out["persisted"] is True and out["ephemeral"] is False
    assert out["accepted"] == {"portrait.template": "soft-flow-study",
                               "portrait.ink": True}

    body = c.get("/settings").json()
    assert body["settings"]["portrait.template"] == "soft-flow-study"
    assert body["settings"]["portrait.ink"] is True
    # The override document carries exactly the user-set keys.
    assert body["user"]["portrait.template"] == "soft-flow-study"
    assert body["user"]["portrait.ink"] is True

    # Restart simulation: a FRESH client (module reload, new connections)
    # against the same profile DB reads the selections back.
    c2 = _client(tmp_path)
    body2 = c2.get("/settings").json()
    assert body2["settings"]["portrait.template"] == "soft-flow-study"
    assert body2["settings"]["portrait.ink"] is True
    assert body2["user"]["portrait.ink"] is True


def test_portrait_ink_false_is_a_real_override(tmp_path):
    c = _client(tmp_path)
    c.post("/settings", json={"settings": {"portrait.ink": True}})
    c.post("/settings", json={"settings": {"portrait.ink": False}})
    body = c.get("/settings").json()
    assert body["settings"]["portrait.ink"] is False
    # False must stay in the override document (not collapse to "unset").
    assert body["user"]["portrait.ink"] is False


def test_unknown_settings_key_still_400(tmp_path):
    c = _client(tmp_path)
    r = c.post("/settings", json={"settings": {"portrait.bogus": 1}})
    assert r.status_code == 400
    assert r.json()["unknown"] == ["portrait.bogus"]


def test_isolated_child_fallback_accepts_portrait_keys(tmp_path, monkeypatch):
    """The child cannot import core.settings; its inline fallback literal
    must carry the portrait keys or POST /settings 400s in the live child
    (the exact encoder-switcher failure mode)."""
    for m in [k for k in list(sys.modules)
              if k == "core" or k.startswith("core.")]:
        monkeypatch.delitem(sys.modules, m, raising=False)
    monkeypatch.setitem(sys.modules, "core", None)
    monkeypatch.setitem(sys.modules, "core.settings", None)
    for m in [k for k in list(sys.modules) if k.startswith("dashboard")]:
        monkeypatch.delitem(sys.modules, m, raising=False)
    import dashboard.plugin_api as pa
    assert "portrait.template" in pa._settings_defaults()
    assert "portrait.ink" in pa._settings_defaults()

    importlib.reload(pa)
    pa._ephemeral_settings.clear()
    pa.set_db_path(tmp_path / "child.db")
    pa.set_ctx(None)
    db = sqlite3.connect(tmp_path / "child.db")
    db.executescript(pa._DDL)
    db.close()
    app = FastAPI()
    app.include_router(pa.router)
    c = TestClient(app)
    r = c.post("/settings", json={"settings": {
        "portrait.template": "angular-pencil", "portrait.ink": True}})
    assert r.status_code == 200
    assert r.json()["persisted"] is True
