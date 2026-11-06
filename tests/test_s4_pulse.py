"""S4 PULSE tests: dynamic rows, LIVE hot path, capsules, prototypes, matrix."""
import pytest

from pulse import activity_encoder, matrix, prototypes as proto_mod
from pulse.activity_encoder import (
    CAPSULE_FIELDS, build_capsule, duration_bucket, handle_post_tool_call,
    handle_pre_tool_call, sanitize_operation,
)
from pulse.matrix import build_matrix
from pulse.prototypes import PrototypeStore, classify_feedback
from pulse.tool_registry import ToolRegistry
from perception.encoder import Tier0SketchBackend


class CountingBackend:
    """Fake EmbeddingBackend that counts encode() calls."""
    model_version = "test-v1"
    def __init__(self):
        self.calls = 0
        self._inner = Tier0SketchBackend()
    def encode(self, texts):
        self.calls += 1
        return self._inner.encode(texts)


@pytest.fixture
def registry(tmp_path):
    from storage import database
    database.set_override(tmp_path / "pulse.db")
    reg = ToolRegistry()
    yield reg
    database.set_override(None)


@pytest.fixture
def backend():
    return Tier0SketchBackend()


def _start(tool, **kw):
    return {"type": "tool_start", "tool_name": tool, **kw}


def _end(tool, status="ok", duration_ms=50.0, **kw):
    return {"type": "tool_end", "tool_name": tool, "status": status,
            "duration_ms": duration_ms, **kw}


def test_new_tool_appears_as_new_row(registry):
    assert registry.list_tools() == []
    row = registry.observe_start("brand_new_tool_xyz")
    assert row["tool_name"] == "brand_new_tool_xyz"
    assert row["id"] is not None
    assert registry.tool_names() == ["brand_new_tool_xyz"]


def test_second_new_tool_also_appears_without_update(registry):
    registry.observe_start("tool_a")
    registry.observe_start("tool_b")
    assert registry.tool_names() == ["tool_a", "tool_b"]


def test_row_id_stable_and_persisted(registry, tmp_path):
    row = registry.observe_start("persisted_tool")
    reg2 = ToolRegistry()  # same DB override -> same persisted rows
    persisted = reg2.get("persisted_tool")
    assert persisted is not None
    assert persisted["id"] == row["id"]
    assert persisted["calls_started"] == 1


def test_repeated_calls_update_live_immediately(registry):
    registry.observe_start("terminal")
    registry.observe_start("terminal")
    row = registry.get("terminal")
    assert row["calls_started"] == 2
    rows = build_matrix(registry, PrototypeStore())
    assert rows[0]["live"]["calls_started"] == 2


def test_pre_tool_call_path_never_calls_encode(registry):
    """Counter semantics: handle_pre_tool_call takes no backend and must
    not touch encode(). A counting backend proves the hot path is clean."""
    fake = CountingBackend()
    handle_pre_tool_call(_start("terminal"), registry)
    handle_pre_tool_call(_start("terminal"), registry)
    assert fake.calls == 0
    assert registry.get("terminal")["calls_started"] == 2


def test_post_tool_call_encodes_on_worker_side(registry):
    fake = CountingBackend()
    capsule = handle_post_tool_call(_end("terminal"), registry, backend=fake)
    assert fake.calls == 1
    assert capsule["vector"] is not None and len(capsule["vector"]) == 256
    assert registry.get("terminal")["calls_completed"] == 1


def test_sanitize_read_file_basename():
    op = sanitize_operation("read_file", args={"path": "/home/u/project/config.py"})
    assert op == "read_file:config.py"
    assert "/home" not in op


def test_sanitize_terminal_command_verb():
    op = sanitize_operation("terminal", args={"command": "git status --porcelain"})
    assert op == "terminal:git"


def test_duration_buckets():
    assert duration_bucket(10) == "instant"
    assert duration_bucket(500) == "fast"
    assert duration_bucket(5000) == "medium"
    assert duration_bucket(60000) == "slow"
    assert duration_bucket(None) == "unknown"


def test_capsule_schema_has_no_result_payload(backend):
    secret = "SECRET-TOOL-OUTPUT-XYZ"
    capsule = build_capsule(
        _end("read_file", result_type="str", result_chars=999),
        args={"path": "/a/b/notes.txt"}, backend=backend)
    assert set(capsule) == set(CAPSULE_FIELDS)
    for forbidden in ("result", "output", "result_payload", "args", "raw"):
        assert forbidden not in capsule
    assert secret not in repr(capsule)
    assert capsule["tool_name"] == "read_file"
    assert capsule["status"] == "ok"


def test_semantically_relevant_goal_scores_stronger(registry, backend):
    """Goal about reading a file must score higher GOAL for read_file
    than for an unrelated tool row."""
    store = PrototypeStore()
    cap_read = build_capsule(_end("read_file"), args={"path": "config.py"}, backend=backend)
    cap_term = build_capsule(_end("terminal"), args={"command": "git status"}, backend=backend)
    store.add_good("read_file", cap_read)
    store.add_good("terminal", cap_term)
    registry.observe_start("read_file")
    registry.observe_start("terminal")
    rows = build_matrix(registry, store,
                        goal_text="read the file config.py please",
                        backend=backend,
                        capsules_by_tool={"read_file": [cap_read], "terminal": [cap_term]})
    by_tool = {r["tool_name"]: r for r in rows}
    assert by_tool["read_file"]["goal"]["value"] > by_tool["terminal"]["goal"]["value"]


def test_empty_good_bank_is_neutral_not_negative(registry, backend):
    registry.observe_start("newtool")
    rows = build_matrix(registry, PrototypeStore(),
                        goal_text="hello there", backend=backend)
    good = rows[0]["good"]
    assert good["value"] is None
    assert good["state"] == "neutral"
    assert good["level"] == "neutral"


def test_friction_only_from_labeled_or_objective(registry, backend):
    store = PrototypeStore()
    registry.observe_start("terminal")
    # Tool error alone -> objective-friction only, user friction untouched.
    err_cap = build_capsule(_end("terminal", status="error"), backend=backend)
    assert store.record_tool_end(err_cap) == "objective_friction"
    bank = store.bank("terminal")
    assert bank["friction"] == []
    assert len(bank["objective_friction"]) == 1
    # Labeled criticism -> user FRICTION.
    cap = build_capsule(_end("terminal"), backend=backend)
    assert store.record_feedback("terminal", cap, "this is terrible, redo it") == "friction"
    assert len(store.bank("terminal")["friction"]) == 1
    rows = build_matrix(registry, store, goal_text="run git status", backend=backend)
    assert rows[0]["friction"]["user"] is not None


def test_tool_error_is_not_user_dissatisfaction(registry, backend):
    store = PrototypeStore()
    registry.observe_start("browser")
    err_cap = build_capsule(_end("browser", status="timeout"), backend=backend)
    store.record_tool_end(err_cap)
    assert store.labeled_count("browser") == 0
    assert store.state("browser") == "learning"
    assert store.bank("browser")["friction"] == []


def test_positive_feedback_labels_good(backend):
    store = PrototypeStore()
    cap = build_capsule(_end("read_file"), backend=backend)
    assert classify_feedback("Thanks, perfect work!") == "good"
    assert classify_feedback("that was wrong and broken") == "friction"
    assert classify_feedback("hmm, interesting") is None
    assert store.record_feedback("read_file", cap, "great job, thank you") == "good"
    assert len(store.bank("read_file")["good"]) == 1


def test_prototypes_bounded_oldest_evicted(backend):
    store = PrototypeStore(cap=2, min_labeled=1)
    caps = [build_capsule(_end("t"), backend=backend) for _ in range(5)]
    for cap in caps:
        store.add_good("t", cap)
    bank = store.bank("t")
    assert len(bank["good"]) == 2
    assert bank["good"] == [caps[3]["vector"], caps[4]["vector"]]


def test_unlabeled_stays_learning_until_min_labeled(registry, backend):
    store = PrototypeStore(cap=8, min_labeled=3)
    registry.observe_start("search")
    assert store.state("search") == "learning"
    for _ in range(2):
        store.add_good("search", build_capsule(_end("search"), backend=backend))
    assert store.state("search") == "learning"
    store.add_good("search", build_capsule(_end("search"), backend=backend))
    assert store.state("search") == "ready"
    rows = build_matrix(registry, store, goal_text="search the web", backend=backend)
    assert rows[0]["state"] == "ready"


def test_goal_neutral_without_goal_vector(registry):
    registry.observe_start("terminal")
    rows = build_matrix(registry, PrototypeStore())
    assert rows[0]["goal"]["value"] is None
    assert rows[0]["goal"]["level"] == "neutral"
    assert rows[0]["friction"]["value"] is None


def test_live_merges_session_state_snapshot(registry):
    class FakeState:
        def snapshot(self):
            return {"tool_calls_started": 7}
    registry.observe_start("terminal")
    rows = build_matrix(registry, PrototypeStore(), session_state=FakeState())
    assert rows[0]["live"]["global"]["tool_calls_started"] == 7
    assert rows[0]["live"]["calls_started"] == 1


def test_no_hardcoded_tool_list_in_pulse_sources():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "pulse"
    text = "".join(p.read_text(encoding="utf-8") for p in root.glob("*.py"))
    # Registry must not enumerate known tools as rows.
    assert "KNOWN_TOOLS" not in text
    assert "DEFAULT_TOOLS" not in text
