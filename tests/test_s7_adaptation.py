"""S7 tests: adaptation rule engine + injection builder + surface fill.

Acceptance slice per task: disabled gate, single bounded instruction,
per-turn dedupe, unrelated -> None, deterministic rules, on/off
persistence paths, /context row flips, G7 boundary assertions.
"""
import importlib
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import dashboard.plugin_api as api
from adaptation import injection, rules
from pulse.prototypes import PrototypeStore

VEC = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
OTHER = [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

ON = {"adaptation.enabled": True, "pulse.thresholds.friction": 0.4,
      "pulse.thresholds.goal": 0.8, "pulse.min_labeled": 3}
OFF = dict(ON, **{"adaptation.enabled": False})


@pytest.fixture(autouse=True)
def _clean():
    injection.clear_state()
    yield
    injection.clear_state()


def qualifying_state(tool="web_search", **over):
    ev = {"friction": 0.9, "goal": 0.9, "labeled": 4, "good_count": 2,
          "objective_friction": 1}
    ev.update(over)
    return {"tools": {tool: ev}}


def prompt(tool="web_search", turn="t1", **over):
    p = {"vector": list(VEC), "tool": tool, "turn_id": turn}
    p.update(over)
    return p


def store_state(tool="web_search"):
    store = PrototypeStore(cap=8, min_labeled=3)
    store.add_good(tool, list(VEC))
    store.add_good(tool, list(VEC))
    store.add_friction(tool, list(VEC))
    store.add_friction(tool, list(VEC))
    store.add_objective_friction(tool, list(VEC))
    return {"prototypes": store, "capsules": {tool: [list(VEC)]}}


# --- gate + firing --------------------------------------------------------

def test_disabled_returns_none_even_when_qualifying():
    assert injection.build_injection(OFF, prompt(), qualifying_state()) is None
    assert injection.build_injection({}, prompt(), qualifying_state()) is None
    assert injection.application_count() == 0


def test_enabled_qualifying_fires_exactly_one_instruction():
    out = injection.build_injection(ON, prompt(), qualifying_state())
    assert out is not None
    assert out["context"] == "Prefer web_search for this domain."
    assert out["meta"]["rule_id"] == "domain-friction-prefer-good"
    assert out["meta"]["tool"] == "web_search"
    assert out["meta"]["evidence"]["friction"] == 0.9
    assert out["meta"]["evidence"]["labeled"] == 4


def test_nested_settings_shape_enables():
    nested = {"adaptation": {"enabled": True},
              "pulse": {"thresholds": {"friction": 0.4, "goal": 0.8},
                        "min_labeled": 3}}
    out = injection.build_injection(nested, prompt(), qualifying_state())
    assert out["context"] == "Prefer web_search for this domain."


def test_prototype_store_state_qualifies():
    out = injection.build_injection(ON, prompt(), store_state())
    assert out["context"] == "Prefer web_search for this domain."
    assert out["meta"]["evidence"]["objective_friction"] == 1


def test_dedupe_per_turn_and_new_turn_refires():
    first = injection.build_injection(ON, prompt(turn="t1"), qualifying_state())
    assert first is not None
    assert injection.build_injection(ON, prompt(turn="t1"), qualifying_state()) is None
    assert injection.build_injection(ON, prompt(turn="t2"), qualifying_state()) is not None
    assert injection.application_count() == 2


def test_unrelated_task_returns_none():
    state = {"tools": {"web_search": {"friction": 0.9, "goal": 0.1,
                                      "labeled": 4, "good_count": 2}}}
    p = {"vector": list(OTHER), "turn_id": "t1"}
    assert injection.build_injection(ON, p, state) is None


def test_below_friction_threshold_returns_none():
    state = qualifying_state(friction=0.2)
    assert injection.build_injection(ON, prompt(), state) is None


def test_below_min_labeled_returns_none():
    state = qualifying_state(labeled=2)
    assert injection.build_injection(ON, prompt(), state) is None


def test_no_good_prototype_returns_none():
    state = qualifying_state(good_count=0)
    assert injection.build_injection(ON, prompt(), state) is None


def test_objective_friction_alone_does_not_qualify():
    state = {"tools": {"web_search": {"objective_friction": 5, "goal": 0.9,
                                      "labeled": 4, "good_count": 2}}}
    assert injection.build_injection(ON, prompt(), state) is None


def test_two_qualifying_tools_still_one_instruction():
    state = {"tools": {
        "web_search": {"friction": 0.9, "goal": 0.9, "labeled": 4, "good_count": 2},
        "read_file": {"friction": 0.8, "goal": 0.9, "labeled": 4, "good_count": 2},
    }}
    out = injection.build_injection(ON, prompt(tool="web_search"), state)
    assert out["context"] == "Prefer web_search for this domain."


# --- transitions + determinism + bounds -----------------------------------

def test_new_topic_suppresses_moderate_friction():
    state = qualifying_state(friction=0.5)
    assert injection.build_injection(
        ON, prompt(), state, {"label": "new_topic"}) is None


def test_correction_signal_allows_firing():
    out = injection.build_injection(
        ON, prompt(), qualifying_state(), {"label": "correction"})
    assert out is not None


def test_evaluate_rules_deterministic():
    a = rules.evaluate_rules(ON, prompt(), qualifying_state())
    b = rules.evaluate_rules(ON, prompt(), qualifying_state())
    assert a == b and len(a) == 1


def test_rules_listing_deterministic_and_inspectable():
    a = rules.list_rule_definitions()
    b = rules.list_rule_definitions()
    assert a == b and a
    for d in a:
        assert {"id", "name", "match", "template", "evidence"} <= set(d)
        assert d["template"] == "Prefer {tool} for this domain."


def test_instruction_bounded_no_prompt_echo():
    secret = "xyzzy secret prompt text that must never be echoed back qwerty"
    out = injection.build_injection(
        ON, prompt(text=secret), qualifying_state())
    assert out["context"] == "Prefer web_search for this domain."
    assert secret not in out["context"] and "xyzzy" not in out["context"]
    assert len(out["context"]) <= 64


def test_log_bounded():
    for i in range(140):
        injection.record_application({"rule_id": "r", "tool": "t", "i": i})
    assert injection.application_count() == injection.MAX_LOG_ENTRIES


# --- commands surface -----------------------------------------------------

class FakeCtx:
    def __init__(self, config=None, with_setter=True):
        self.commands = {}
        self._config = dict(config or {})
        self.with_setter = with_setter

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler

    def get_config(self, key, default=None):
        return self._config.get(key, default)

    def set_config(self, key, value):
        if not self.with_setter:
            raise AttributeError("no setter")
        self._config[key] = value


def _handler(ctx):
    import commands
    commands.register_commands(ctx)
    return ctx.commands["mirror"]


def test_adaptation_on_off_persist_via_ctx():
    ctx = FakeCtx()
    h = _handler(ctx)
    out = h("adaptation on")
    assert "on" in out and "persisted" in out
    assert ctx._config["adaptation.enabled"] is True
    out = h("adaptation off")
    assert "off" in out and "persisted" in out
    assert ctx._config["adaptation.enabled"] is False


def test_adaptation_on_off_ephemeral_without_setter():
    api._ephemeral_settings.clear()
    ctx = FakeCtx(with_setter=False)
    h = _handler(ctx)
    out = h("adaptation on")
    assert "on" in out and "ephemeral" in out
    assert api._ephemeral_settings["adaptation.enabled"] is True
    api._ephemeral_settings.clear()


def test_adaptation_status_shows_last_and_count():
    ctx = FakeCtx(config={"adaptation.enabled": True})
    h = _handler(ctx)
    assert "last: none" in h("adaptation status")
    injection.build_injection(ON, prompt(), qualifying_state())
    status = h("adaptation status")
    assert "on" in status and "1 time(s)" in status
    assert "Prefer web_search for this domain." in status


def test_adaptation_rules_lists_definitions():
    ctx = FakeCtx()
    h = _handler(ctx)
    out = h("adaptation rules")
    assert "domain-friction-prefer-good" in out
    assert "Prefer {tool} for this domain." in out
    assert out == h("adaptation rules")  # deterministic


# --- /context surface -----------------------------------------------------

def _client(tmp_path, ctx=None):
    importlib.reload(api)
    injection.clear_state()
    api._ephemeral_settings.clear()
    api.set_db_path(tmp_path / "data.db")
    api.set_ctx(ctx)
    db = sqlite3.connect(tmp_path / "data.db")
    db.executescript(api._DDL)
    db.close()
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def _rows(client):
    rows = client.get("/context").json()["rows"]
    return {r["item"]: r for r in rows}


def test_context_adaptation_row_disabled(tmp_path):
    rows = _rows(_client(tmp_path, ctx=FakeCtx()))
    assert rows["plugin adaptation"]["present"] is False
    assert rows["injected instruction"]["present"] is False
    assert rows["injected instruction"]["visibility"] == "KNOWN"


def test_context_adaptation_row_enabled(tmp_path):
    ctx = FakeCtx(config={"adaptation.enabled": True})
    rows = _rows(_client(tmp_path, ctx=ctx))
    assert rows["plugin adaptation"]["present"] is True
    assert rows["plugin adaptation"]["source"] == "adaptation engine"


def test_context_injected_row_visible_after_fire(tmp_path):
    ctx = FakeCtx(config={"adaptation.enabled": True})
    client = _client(tmp_path, ctx=ctx)
    injection.build_injection(ON, prompt(), qualifying_state())
    rows = _rows(client)
    row = rows["injected instruction"]
    assert row["present"] is True and row["visibility"] == "VISIBLE"
    assert row["source"] == ("pre_llm_call context injection; "
                             "may persist in API sidecar")
    # unrelated static rows unchanged
    assert rows["user message"]["present"] is True
    assert rows["system instructions"]["visibility"] == "NOT EXPOSED"


# --- G7 boundaries --------------------------------------------------------

def test_adaptation_no_file_writes_or_memory_retrieval():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "adaptation"
    sources = {p.name: p.read_text(encoding="utf-8")
               for p in root.glob("*.py")}
    assert set(sources) == {"__init__.py", "rules.py", "injection.py"}
    blob = "\n".join(sources.values())
    for banned in ("open(", "write_text", "USER.md", "MEMORY.md", "SOUL.md",
                   "AGENTS.md", "memory_provider", "MemoryProvider",
                   "requests.", "urllib", "random", "np.random"):
        assert banned not in blob, banned


def test_instruction_never_marks_identity_or_permissions():
    out = injection.build_injection(ON, prompt(), qualifying_state())
    text = out["context"].lower()
    for banned in ("identity", "permission", "memory", "system prompt"):
        assert banned not in text
