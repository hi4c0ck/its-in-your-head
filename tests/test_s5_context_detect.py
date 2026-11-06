"""L5: /context filesystem source detection from the actual profile.

Filesystem rows (SOUL.md, USER.md, MEMORY.md, AGENTS.md, active skills)
must be detected at request time: present where the file/dir exists in
the fixture profile, absent where it does not, unknown when profile
resolution is unavailable (test double without a profile path) — never
present by default. Non-filesystem rows keep their S5/S7 semantics.
"""
import importlib
import sqlite3

import dashboard.plugin_api as api
from fastapi import FastAPI
from fastapi.testclient import TestClient

FS_ITEMS = ("SOUL.md", "USER.md", "MEMORY.md", "AGENTS.md", "active skills")


class FakeCtx:
    def __init__(self, config=None, profile_dir=None):
        self.commands = {}
        self._config = dict(config or {})
        if profile_dir is not None:
            self.profile_dir = profile_dir

    def register_command(self, name, handler, description="",
                         args_hint=""):
        self.commands[name] = handler

    def get_config(self, key, default=None):
        return self._config.get(key, default)

    def set_config(self, key, value):
        self._config[key] = value


def _client(tmp_path, ctx=None, profile=None):
    importlib.reload(api)
    api._ephemeral_settings.clear()
    api.set_db_path(tmp_path / "data.db")
    api.set_ctx(ctx)
    api.set_profile_path(profile)
    db = sqlite3.connect(tmp_path / "data.db")
    db.executescript(api._DDL)
    db.close()
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def _rows(client):
    r = client.get("/context")
    assert r.status_code == 200
    rows = r.json()["rows"]
    return {row["item"]: row for row in rows}, rows


def _make_profile(root, files=(), dirs=()):
    root.mkdir(parents=True, exist_ok=True)
    for rel in files:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    for rel in dirs:
        (root / rel).mkdir(parents=True, exist_ok=True)
    return root


def test_partial_profile_rows_reflect_actual_presence(tmp_path):
    profile = _make_profile(
        tmp_path / "profile",
        files=("SOUL.md", "memories/MEMORY.md"),
        dirs=("skills",))
    rows, _ = _rows(_client(tmp_path, ctx=FakeCtx(), profile=profile))
    assert rows["SOUL.md"]["state"] == "present"
    assert rows["SOUL.md"]["present"] is True
    assert rows["MEMORY.md"]["state"] == "present"
    assert rows["MEMORY.md"]["present"] is True
    assert rows["active skills"]["state"] == "present"
    # Absent rows stay in the table, marked absent.
    assert rows["USER.md"]["state"] == "absent"
    assert rows["USER.md"]["present"] is False
    assert rows["AGENTS.md"]["state"] == "absent"
    assert rows["AGENTS.md"]["present"] is False


def test_empty_profile_all_filesystem_rows_absent(tmp_path):
    profile = _make_profile(tmp_path / "profile")
    rows, _ = _rows(_client(tmp_path, ctx=FakeCtx(), profile=profile))
    for item in FS_ITEMS:
        assert rows[item]["state"] == "absent", item
        assert rows[item]["present"] is False, item


def test_ctx_profile_dir_attribute_is_used(tmp_path):
    profile = _make_profile(tmp_path / "profile", files=("USER.md",))
    # USER.md at profile root is an accepted candidate (see mapping).
    rows, _ = _rows(_client(tmp_path, ctx=FakeCtx(profile_dir=profile)))
    assert rows["USER.md"]["state"] == "present"
    assert rows["SOUL.md"]["state"] == "absent"


def test_unavailable_profile_resolution_degrades_to_unknown(tmp_path):
    # Ctx double without any profile path and no test override.
    rows, _ = _rows(_client(tmp_path, ctx=FakeCtx()))
    for item in FS_ITEMS:
        assert rows[item]["state"] == "unknown", item
        assert rows[item]["present"] is False, item


def test_context_shape_stability(tmp_path):
    profile = _make_profile(tmp_path / "profile", files=("SOUL.md",))
    rows, raw = _rows(_client(tmp_path, ctx=FakeCtx(), profile=profile))
    assert {"user message", "history extent", "system instructions",
            "SOUL.md", "USER.md", "MEMORY.md", "AGENTS.md",
            "active skills", "plugin adaptation",
            "injected instruction"} <= set(rows)
    for row in raw:
        assert row["visibility"] in {"VISIBLE", "KNOWN", "NOT EXPOSED"}
        assert "source" in row
        assert "present" in row and "state" in row
        assert row["state"] in {"present", "absent", "unknown"}
        assert row["present"] == (row["state"] == "present") or \
            row["item"] in ("user message", "history extent",
                            "system instructions",
                            "plugin adaptation", "injected instruction")
    # Non-filesystem S5/S7 semantics preserved.
    assert rows["user message"]["present"] is True
    assert rows["system instructions"]["visibility"] == "NOT EXPOSED"
    assert rows["plugin adaptation"]["present"] is False


def test_detection_is_request_time_not_cached(tmp_path):
    profile = _make_profile(tmp_path / "profile")
    client = _client(tmp_path, ctx=FakeCtx(), profile=profile)
    rows, _ = _rows(client)
    assert rows["MEMORY.md"]["state"] == "absent"
    _make_profile(profile, files=("memories/MEMORY.md",))
    rows, _ = _rows(client)
    assert rows["MEMORY.md"]["state"] == "present"
