"""S5 tests: new backend routes + /mirror commands.

Every route is exercised against an empty DB (documented empty shapes,
never 500) and, where relevant, a seeded DB. Missing S3/S4 engine modules
must degrade to empty shapes. Commands are tested with a fake ctx that
records registrations.
"""
import importlib
import sqlite3
import time

import dashboard.plugin_api as api
from fastapi import FastAPI
from fastapi.testclient import TestClient

DIMS = ("directness", "context_richness", "exploration",
        "analytical_depth", "iteration", "scope")


class FakeCtx:
    """Records command registrations + config API (Hermes ctx surface)."""

    def __init__(self, config=None):
        self.commands = {}
        self._config = dict(config or {})

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler

    def get_config(self, key, default=None):
        return self._config.get(key, default)

    def set_config(self, key, value):
        self._config[key] = value


def _client(tmp_path, seed=False, ctx=None):
    importlib.reload(api)
    api._ephemeral_settings.clear()
    api.set_db_path(tmp_path / "data.db")
    api.set_ctx(ctx)
    # Parent (PG2 merge): isolate the process-level engine per client so
    # earlier modules' feeds can't leak live state into empty-shape tests.
    try:
        import engine
        engine._engine = None
    except ImportError:
        pass
    db = sqlite3.connect(tmp_path / "data.db")
    db.executescript(api._DDL)
    if seed:
        now = time.time()

        def ins(ts_offset, etype, tool_name=None, status=None,
                status_code=None, finish_reason=None, total_tokens=None):
            db.execute(
                "INSERT INTO events (ts, type, tool_name, status, status_code,"
                " finish_reason, total_tokens) VALUES (?,?,?,?,?,?,?)",
                (now + ts_offset, etype, tool_name, status, status_code,
                 finish_reason, total_tokens))
        ins(-120, "tool_start", tool_name="read_file", status="running")
        ins(-60, "tool_end", tool_name="read_file", status="ok")
        ins(-30, "error", tool_name="web_search", status_code=503)
        ins(-10, "api", finish_reason="stop", total_tokens=42)
        db.commit()
    db.close()
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


# --- empty-DB shapes (never 500, never crash on missing engines) ----------

def test_pulse_empty_shape(tmp_path):
    c = _client(tmp_path)
    r = c.get("/pulse")
    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == []
    assert len(body["columns"]) == 4
    assert body["available"] is False


def test_portrait_empty_shape(tmp_path):
    c = _client(tmp_path)
    r = c.get("/portrait")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert set(body["dimensions"]) == set(DIMS)
    assert all(v is None for v in body["dimensions"].values())
    assert set(body["confidence"]) == set(DIMS)
    assert set(body["user"]) == set(DIMS)
    assert set(body["agent"]) == set(DIMS)


def test_drift_empty_arrays(tmp_path):
    c = _client(tmp_path)
    r = c.get("/drift")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert set(body["series"]) == set(DIMS)
    assert all(v == [] for v in body["series"].values())


def test_context_static_rows(tmp_path):
    c = _client(tmp_path)
    r = c.get("/context")
    assert r.status_code == 200
    rows = r.json()["rows"]
    items = {row["item"] for row in rows}
    assert {"user message", "history extent", "system instructions",
            "SOUL.md", "USER.md", "MEMORY.md", "AGENTS.md",
            "active skills", "plugin adaptation"} <= items
    for row in rows:
        assert row["visibility"] in {"VISIBLE", "KNOWN", "NOT EXPOSED"}
        assert "source" in row


def test_settings_defaults_shape(tmp_path):
    c = _client(tmp_path)
    r = c.get("/settings")
    assert r.status_code == 200
    body = r.json()
    assert "settings" in body and "keys" in body
    assert body["settings"]["adaptation.enabled"] is False
    assert body["settings"]["observation.retain_days"] == 90
    # desktop walk: nested expansion resolves dotted keys
    assert body["pulse"]["thresholds"]["goal"] == 0.8
    assert set(body["keys"]) == set(body["settings"])


def test_no_db_file_still_empty_shapes(tmp_path):
    c = _client(tmp_path)
    for path in ("/pulse", "/portrait", "/drift", "/context", "/settings"):
        assert c.get(path).status_code == 200


# --- /recent additive fields ----------------------------------------------

def test_recent_additive_tool_and_status_word(tmp_path):
    c = _client(tmp_path, seed=True)
    rows = c.get("/recent?limit=10").json()
    assert len(rows) == 4
    for row in rows:
        # pre-existing fields kept, additive fields present
        assert {"type", "ts", "model", "total_tokens", "finish_reason",
                "status_code"} <= set(row)
        assert {"tool", "tool_name", "status_word"} <= set(row)
        assert row["tool"] == row["tool_name"]
        assert row["status_word"] in {"ok", "fail", "running"}
    by_tool = {row["tool"]: row for row in rows if row["tool"]}
    assert by_tool["read_file"]["status_word"] in {"ok", "running"}
    assert by_tool["web_search"]["status_word"] == "fail"


def test_recent_error_row_status_word_fail(tmp_path):
    c = _client(tmp_path, seed=True)
    rows = c.get("/recent?limit=1").json()  # newest = api/stop
    assert rows[0]["status_word"] == "ok"


# --- /settings POST ---------------------------------------------------------

def test_settings_rejects_unknown_keys_400(tmp_path):
    c = _client(tmp_path, ctx=FakeCtx())
    r = c.post("/settings", json={"bogus.key": 1})
    assert r.status_code == 400
    assert "bogus.key" in r.json()["unknown"]


def test_settings_rejects_mixed_known_and_unknown(tmp_path):
    c = _client(tmp_path, ctx=FakeCtx())
    r = c.post("/settings", json={"observation.retain_days": 30,
                                  "nope": True})
    assert r.status_code == 400
    # nothing applied on 400
    assert c.get("/settings").json()["settings"]["observation.retain_days"] == 90


def test_settings_accepts_c4_keys_and_persists_via_ctx(tmp_path):
    ctx = FakeCtx()
    c = _client(tmp_path, ctx=ctx)
    r = c.post("/settings", json={"settings": {
        "observation.retain_days": 30, "adaptation.enabled": True}})
    assert r.status_code == 200
    body = r.json()
    assert body["persisted"] is True
    assert body["ephemeral"] is False
    assert ctx._config["observation.retain_days"] == 30
    assert c.get("/settings").json()["settings"]["adaptation.enabled"] is True


def test_settings_ephemeral_without_ctx(tmp_path):
    c = _client(tmp_path, ctx=None)
    r = c.post("/settings", json={"pulse.prototype_cap": 32})
    body = r.json()
    assert body["persisted"] is False
    assert body["ephemeral"] is True
    assert c.get("/settings").json()["settings"]["pulse.prototype_cap"] == 32


def test_settings_reads_ctx_config_values(tmp_path):
    ctx = FakeCtx(config={"observer": None, "observation.retain_days": 7})
    c = _client(tmp_path, ctx=ctx)
    assert c.get("/settings").json()["settings"]["observation.retain_days"] == 7


# --- /reset -----------------------------------------------------------------

def test_reset_wipes_events_and_idempotent(tmp_path):
    c = _client(tmp_path, seed=True)
    assert c.get("/metrics").json()["events"] == 4
    first = c.post("/reset")
    assert first.status_code == 200
    assert first.json()["wiped"]["events"] == 4
    assert c.get("/metrics").json()["events"] == 0
    second = c.post("/reset")
    assert second.status_code == 200
    assert second.json()["wiped"]["events"] == 0  # idempotent


def test_reset_does_not_touch_config(tmp_path):
    ctx = FakeCtx(config={"observation.retain_days": 7})
    c = _client(tmp_path, seed=True, ctx=ctx)
    c.post("/reset")
    assert ctx._config == {"observation.retain_days": 7}
    assert c.get("/settings").json()["settings"]["observation.retain_days"] == 7


def test_reset_empty_db_ok(tmp_path):
    c = _client(tmp_path)
    r = c.post("/reset")
    assert r.status_code == 200
    assert r.json() == {"wiped": {"events": 0, "portrait_history": 0,
                                   "pulse_tools": 0, "pulse_prototypes": 0,
                                   "pulse_goal": 0, "pulse_capsules": 0},
                        "reset": True}


# --- pinned routes still live ---------------------------------------------

def test_pinned_routes_still_live(tmp_path):
    c = _client(tmp_path, seed=True)
    assert c.get("/metrics").json()["events"] == 4
    assert c.get("/heatmap").status_code == 200
    assert len(c.get("/recent").json()) == 4


# --- /mirror commands -------------------------------------------------------

def test_mirror_registers_via_register_commands(tmp_path):
    import commands
    ctx = FakeCtx()
    # point the API at a tmp DB so command reads don't touch the real DB
    _client(tmp_path, seed=True)
    names = commands.register_commands(ctx)
    assert names == ["mirror"]
    assert "mirror" in ctx.commands


def test_mirror_summary_and_subcommands_text(tmp_path):
    import commands
    _client(tmp_path, seed=True)
    ctx = FakeCtx()
    commands.register_commands(ctx)
    handler = ctx.commands["mirror"]
    summary = handler("")
    assert isinstance(summary, str) and "events" in summary
    assert "{" not in summary  # plain text, no JSON dumps
    for sub in ("pulse", "portrait", "drift", "context"):
        out = handler(sub)
        assert isinstance(out, str) and out
        assert not out.lstrip().startswith("{")


def test_mirror_adaptation_on_off_rules(tmp_path):
    """S7 semantics supersede S5's read-only pin: on/off toggle
    adaptation.enabled (persisted via ctx config), rules lists the
    deterministic rule definitions."""
    import commands
    _client(tmp_path)
    ctx = FakeCtx()
    commands.register_commands(ctx)
    handler = ctx.commands["mirror"]
    status = handler("adaptation status")
    assert "off" in status
    on = handler("adaptation on")
    assert "on" in on and "persisted" in on
    assert ctx._config.get("adaptation.enabled") is True
    off = handler("adaptation off")
    assert "off" in off
    assert ctx._config.get("adaptation.enabled") is False
    rules = handler("adaptation rules")
    assert "adaptation rules" in rules


def test_mirror_unknown_subcommand_usage(tmp_path):
    import commands
    _client(tmp_path)
    ctx = FakeCtx()
    commands.register_commands(ctx)
    assert "/mirror" in ctx.commands["mirror"]("bogus")


def test_mirror_reset_requires_confirmation(tmp_path):
    """Spec §13: a bare /mirror reset never wipes without explicit
    confirmation. FakeCtx has no ask_choice prompt, so the command
    refuses and points at the --yes flag; the dataset stays intact."""
    import commands
    c = _client(tmp_path, seed=True)
    ctx = FakeCtx()
    commands.register_commands(ctx)
    out = ctx.commands["mirror"]("reset")
    assert "confirmation required" in out
    assert c.get("/metrics").json()["events"] == 4  # untouched


def test_mirror_reset_yes_flag_wipes(tmp_path):
    """The --yes flag IS the explicit confirmation: it wipes directly."""
    import commands
    c = _client(tmp_path, seed=True)
    ctx = FakeCtx()
    commands.register_commands(ctx)
    out = ctx.commands["mirror"]("reset --yes")
    assert "wiped 4 events" in out
    assert c.get("/metrics").json()["events"] == 0


class AskCtx(FakeCtx):
    """FakeCtx + the host's interactive ask_choice (Wipe/Cancel prompt)."""

    def __init__(self, answer="wipe", config=None):
        super().__init__(config=config)
        self._answer = answer

    def ask_choice(self, question, choices):
        return self._answer


def test_mirror_reset_ask_choice(tmp_path):
    """With a host prompt available, Wipe confirms the wipe, Cancel
    (or an unanswerable prompt) leaves the dataset intact."""
    import commands
    c = _client(tmp_path, seed=True)
    cancelled = AskCtx(answer="cancel")
    commands.register_commands(cancelled)
    out = cancelled.commands["mirror"]("reset")
    assert "cancelled" in out
    assert c.get("/metrics").json()["events"] == 4
    wiped = AskCtx(answer="wipe")
    commands.register_commands(wiped)
    out = wiped.commands["mirror"]("reset")
    assert "wiped 4 events" in out
    assert c.get("/metrics").json()["events"] == 0
