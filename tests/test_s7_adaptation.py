"""S7 tests: directional adaptation rule engine + injection builder + surface.

Covers the v2 directional contract: three independent evidence channels
(GOOD / user FRICTION / objective FRICTION), directional fixed-template
instructions, conflicting-evidence suppression, GOAL independence,
per-category evidence readiness, vector-space compatibility, plus the
preserved gate / dedupe / bounded / fault-isolation / UI / G7 surfaces.
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

ON = {"adaptation.enabled": True,
      "pulse.thresholds.good": 0.6,
      "pulse.thresholds.friction": 0.4,
      "pulse.good_min_samples": 3,
      "pulse.user_friction_min_samples": 3,
      "pulse.objective_friction_min_samples": 3}
OFF = dict(ON, **{"adaptation.enabled": False})

PREfer = "Prefer web_search for this domain."
DO_NOT_REPEAT = "Do not repeat the previous web_search approach for this domain."
VERIFY = "Verify the result of web_search before proceeding in this domain."


@pytest.fixture(autouse=True)
def _clean():
    injection.clear_state()
    yield
    injection.clear_state()


def prompt(turn="t1", **over):
    p = {"vector": list(VEC), "turn_id": turn}
    p.update(over)
    return p


def good_state(**over):
    ev = {"good_similarity": 0.85, "good_count": 3}
    ev.update(over)
    return {"tools": {"web_search": ev}}


def user_friction_state(**over):
    ev = {"friction_similarity": 0.6, "friction_count": 3}
    ev.update(over)
    return {"tools": {"web_search": ev}}


def objective_state(**over):
    ev = {"objective_friction_similarity": 0.6, "objective_friction_count": 3}
    ev.update(over)
    return {"tools": {"web_search": ev}}


def conflict_state():
    return {"tools": {"web_search": {
        "good_similarity": 0.85, "good_count": 3,
        "friction_similarity": 0.6, "friction_count": 3}}}


def store_state(tool="web_search", space=("tier0-sketch", "v1")):
    store = PrototypeStore(cap=8, min_labeled=3)
    for _ in range(3):
        store.add(tool, list(VEC), "good", provenance=space)
    return {"prototypes": store}


# --- gate ------------------------------------------------------------------
def test_disabled_returns_none_even_when_qualifying():
    assert injection.build_injection(OFF, prompt(), good_state()) is None
    assert injection.build_injection({}, prompt(), good_state()) is None
    assert injection.application_count() == 0


# --- directional channels --------------------------------------------------
def test_good_only_fires_positive_instruction():
    out = injection.build_injection(ON, prompt(), good_state())
    assert out is not None
    assert out["context"] == PREfer
    assert out["meta"]["rule_id"] == "pulse-good"
    assert out["meta"]["tool"] == "web_search"
    assert out["meta"]["evidence"]["source"] == "good"


def test_user_friction_only_fires_corrective_instruction():
    out = injection.build_injection(ON, prompt(), user_friction_state())
    assert out is not None
    assert out["context"] == DO_NOT_REPEAT
    assert out["meta"]["rule_id"] == "pulse-user-friction"
    assert out["meta"]["evidence"]["source"] == "user_friction"


def test_objective_friction_only_fires_caution_instruction():
    out = injection.build_injection(ON, prompt(), objective_state())
    assert out is not None
    assert out["context"] == VERIFY
    assert out["meta"]["rule_id"] == "pulse-objective-friction"
    assert out["meta"]["evidence"]["source"] == "objective_friction"


def test_no_compatible_evidence_returns_none():
    # friction similarity below threshold, no good evidence.
    assert injection.build_injection(ON, prompt(), user_friction_state(
        friction_similarity=0.1)) is None
    assert injection.build_injection(ON, prompt(), {"tools": {}}) is None
    assert injection.build_injection(ON, prompt(), None) is None


def test_no_good_prototype_cannot_fire_good():
    # only objective evidence: the GOOD channel has no bank -> no 'Prefer'.
    out = injection.build_injection(ON, prompt(), objective_state())
    assert out is not None and out["context"] == VERIFY


def test_below_category_min_returns_none():
    assert injection.build_injection(ON, prompt(),
                                     good_state(good_count=2)) is None
    assert injection.build_injection(ON, prompt(),
                                     user_friction_state(friction_count=2)) \
        is None


def test_category_readiness_is_independent():
    # good ready, friction NOT ready (below min) -> good fires, friction
    # must not borrow good's readiness.
    state = {"tools": {"web_search": {
        "good_similarity": 0.85, "good_count": 3,
        "friction_similarity": 0.9, "friction_count": 1}}}
    out = injection.build_injection(ON, prompt(turn="t1"), state)
    assert out is not None and out["context"] == PREfer
    # and the inverse: friction ready, good below min (fresh turn).
    state2 = {"tools": {"web_search": {
        "good_similarity": 0.9, "good_count": 1,
        "friction_similarity": 0.9, "friction_count": 3}}}
    out2 = injection.build_injection(ON, prompt(turn="t2"), state2)
    assert out2 is not None and out2["context"] == DO_NOT_REPEAT


def test_conflicting_evidence_suppresses_injection():
    # good + user friction both qualify -> no clear direction -> no
    # instruction (banks still accumulate, but this turn is silent).
    assert injection.build_injection(ON, prompt(), conflict_state()) is None


def test_goal_maturity_is_not_a_gate():
    # no goal key at all, no weak/absent GOAL blocking an independent signal.
    out = injection.build_injection(ON, prompt(turn="t1"), good_state())
    assert out is not None and out["context"] == PREfer
    out2 = injection.build_injection(ON, prompt(turn="t2"),
                                     user_friction_state())
    assert out2 is not None and out2["context"] == DO_NOT_REPEAT


def test_two_qualifying_tools_still_one_instruction():
    state = {"tools": {
        "web_search": {"good_similarity": 0.85, "good_count": 3},
        "read_file": {"good_similarity": 0.7, "good_count": 3}}}
    out = injection.build_injection(ON, prompt(), state)
    assert out is not None
    assert out["context"] == "Prefer web_search for this domain."
    assert out["meta"]["tool"] == "web_search"  # highest similarity wins


# --- vector-space compatibility --------------------------------------------
def test_store_good_in_compatible_space_fires():
    p = dict(prompt(), vector_space=("tier0-sketch", "v1"))
    out = injection.build_injection(ON, p, store_state())
    assert out is not None and out["context"] == PREfer


def test_dimension_mismatch_prevents_injection():
    p = dict(prompt(), vector=[1.0, 0.0, 0.0])  # dim 3 vs 8-dim banks
    assert injection.build_injection(ON, p, store_state()) is None


def test_provenance_mismatch_prevents_injection():
    p = dict(prompt(), vector_space=("openai", "text-embed-3"))
    assert injection.build_injection(ON, p, store_state()) is None


def test_unrelated_prompt_no_fire():
    p = {"vector": list(OTHER), "turn_id": "t1",
         "vector_space": ("tier0-sketch", "v1")}
    # OTHER is orthogonal to the VEC-based good bank -> below threshold.
    assert injection.build_injection(ON, p, store_state()) is None


# --- dedupe + determinism + bounds -----------------------------------------
def test_dedupe_per_turn_and_new_turn_refires():
    first = injection.build_injection(ON, prompt(turn="t1"), good_state())
    assert first is not None
    assert injection.build_injection(ON, prompt(turn="t1"), good_state()) \
        is None
    assert injection.build_injection(ON, prompt(turn="t2"), good_state()) \
        is not None
    assert injection.application_count() == 2


def test_evaluate_rules_deterministic():
    a = rules.evaluate_rules(ON, prompt(), good_state())
    b = rules.evaluate_rules(ON, prompt(), good_state())
    assert a == b and len(a) == 1


def test_rules_listing_deterministic_and_inspectable():
    a = rules.list_rule_definitions()
    b = rules.list_rule_definitions()
    assert a == b and len(a) == 3
    names = {d["name"] for d in a}
    assert names == {"pulse-good", "pulse-user-friction",
                     "pulse-objective-friction"}
    templates = {d["template"] for d in a}
    assert PREfer.replace("web_search", "{tool}") in templates
    assert DO_NOT_REPEAT.replace("web_search", "{tool}") in templates
    assert VERIFY.replace("web_search", "{tool}") in templates


def test_instruction_bounded_no_prompt_echo():
    secret = "xyzzy secret prompt text that must never be echoed back qwerty"
    out = injection.build_injection(ON, prompt(text=secret), good_state())
    assert out["context"] == PREfer
    assert secret not in out["context"] and "xyzzy" not in out["context"]
    assert len(out["context"]) <= 72


def test_log_bounded():
    for i in range(140):
        injection.record_application({"rule_id": "r", "tool": "t", "i": i})
    assert injection.application_count() == injection.MAX_LOG_ENTRIES


def test_fault_isolation_bad_state_no_host_break():
    # malformed pulse_state must yield no injection, never raise.
    assert injection.build_injection(ON, prompt(), object()) is None
    assert injection.build_injection(ON, prompt(), {"tools": "not-a-dict"}) \
        is None


# --- commands surface ------------------------------------------------------
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
    injection.build_injection(ON, prompt(), good_state())
    status = h("adaptation status")
    assert "on" in status and "1 time(s)" in status
    assert PREfer in status


def test_adaptation_rules_lists_definitions():
    ctx = FakeCtx()
    h = _handler(ctx)
    out = h("adaptation rules")
    assert "pulse-good" in out
    assert "pulse-user-friction" in out
    assert "pulse-objective-friction" in out
    assert "Prefer {tool} for this domain." in out
    assert "Do not repeat the previous {tool} approach for this domain." \
        in out
    assert "Verify the result of {tool} before proceeding in this domain." \
        in out
    assert out == h("adaptation rules")  # deterministic


# --- /context surface -------------------------------------------------------
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
    injection.build_injection(ON, prompt(), good_state())
    rows = _rows(client)
    row = rows["injected instruction"]
    assert row["present"] is True and row["visibility"] == "VISIBLE"
    assert row["source"] == ("pre_llm_call context injection; "
                             "may persist in API sidecar")
    assert rows["user message"]["present"] is True
    assert rows["system instructions"]["visibility"] == "NOT EXPOSED"


# --- G7 boundaries ---------------------------------------------------------
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
    out = injection.build_injection(ON, prompt(), good_state())
    text = out["context"].lower()
    for banned in ("identity", "permission", "memory", "system prompt"):
        assert banned not in text
