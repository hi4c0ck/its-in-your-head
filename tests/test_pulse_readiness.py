"""Stage 2: per-cell readiness + feedback unification.

Readiness is per tool x metric with independent evidence per metric
(GOAL = reference/capsule vectors, GOOD = GOOD samples, user and
objective FRICTION separate, never summed). A computable score stays
VISIBLE as provisional below threshold with its true level — level is
never forced neutral by state. pulse/feedback.py FEEDBACK_RULES is the
single phrase table consumed by both the detector and
PrototypeStore.classify_feedback. Bridge mirror parity: the dashboard
fallback (_pulse_shape) computes the identical cell math.
"""
import pytest

from core.settings import DEFAULTS, load, readiness_thresholds
from pulse import matrix as _matrix
from pulse.activity_encoder import build_capsule
from pulse.feedback import FEEDBACK_RULES, detect_feedback
from pulse.matrix import build_matrix
from pulse.prototypes import PrototypeStore, classify_feedback
from pulse.tool_registry import ToolRegistry
from perception.encoder import Tier0SketchBackend


TOOL = "read_tool_xyz"
GOAL_TEXT = "read the config file please"


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


def _end(tool, status="ok", **kw):
    return {"type": "tool_end", "tool_name": tool, "status": status,
            "duration_ms": 50.0, **kw}


def _caps(backend, n):
    return [build_capsule(_end(TOOL), backend=backend) for _ in range(n)]


def _row(registry, store, backend, goal_text=GOAL_TEXT, capsules=None):
    registry.observe_start(TOOL)
    rows = build_matrix(registry, store, goal_text=goal_text,
                        backend=backend,
                        capsules_by_tool=(capsules or {}))
    assert len(rows) == 1
    return rows[0]


# --- settings ---------------------------------------------------------------

def test_settings_keys_present_with_legacy_fallback_default():
    for key in ("pulse.goal_min_references", "pulse.good_min_samples",
                "pulse.user_friction_min_samples",
                "pulse.objective_friction_min_samples"):
        assert key in DEFAULTS
    # Unset (None) -> every category falls back to legacy min_labeled.
    assert readiness_thresholds(dict(DEFAULTS)) == {
        "goal": 3, "good": 3, "user_friction": 3, "objective_friction": 3}
    # Legacy change propagates to unset categories.
    snap = dict(DEFAULTS, **{"pulse.min_labeled": 5})
    assert readiness_thresholds(snap) == {
        "goal": 5, "good": 5, "user_friction": 5, "objective_friction": 5}
    # Explicit overrides win per category, independently.
    snap = dict(DEFAULTS, **{"pulse.min_labeled": 5,
                             "pulse.good_min_samples": 2,
                             "pulse.user_friction_min_samples": 1})
    assert readiness_thresholds(snap) == {
        "goal": 5, "good": 2, "user_friction": 1, "objective_friction": 5}
    # Legacy snapshots without the new keys still resolve.
    assert readiness_thresholds({"pulse.min_labeled": 4}) == {
        "goal": 4, "good": 4, "user_friction": 4, "objective_friction": 4}
    assert readiness_thresholds(None) == {
        "goal": 3, "good": 3, "user_friction": 3, "objective_friction": 3}


def test_settings_load_carries_new_keys():
    snap = load(None)
    assert snap["pulse.min_labeled"] == 3
    for key in ("pulse.goal_min_references", "pulse.good_min_samples",
                "pulse.user_friction_min_samples",
                "pulse.objective_friction_min_samples"):
        assert snap[key] is None


def test_store_readiness_fallback_and_override():
    store = PrototypeStore(min_labeled=2)
    assert store.readiness_required("goal") == 2
    assert store.readiness_required("good") == 2
    assert store.readiness_required("user_friction") == 2
    assert store.readiness_required("objective_friction") == 2
    store = PrototypeStore(min_labeled=2, good_min_samples=5)
    assert store.readiness_required("good") == 5
    assert store.readiness_required("goal") == 2


# --- GOOD: 1 -> provisional, 2 -> provisional, 3 -> ready --------------------

def test_good_sample_counts_provisional_to_ready(registry, backend):
    store = PrototypeStore(min_labeled=3)
    row = _row(registry, store, backend)
    assert row["good"]["state"] == "learning"
    assert row["good"]["value"] is None

    store.add_good(TOOL, _caps(backend, 1)[0])
    row = _row(registry, store, backend)
    good = row["good"]
    assert good["state"] == "provisional"
    assert good["value"] is not None  # score stays visible
    assert good["level"] in ("weak", "strong")  # never forced neutral
    assert good["support"] == {"count": 1, "required": 3, "ratio": 0.3333}
    assert good["support_count"] == 1
    assert good["readiness_threshold"] == 3

    for cap in _caps(backend, 1):
        store.add_good(TOOL, cap)
    row = _row(registry, store, backend)
    assert row["good"]["state"] == "provisional"
    assert row["good"]["support"]["count"] == 2
    assert row["good"]["support"]["ratio"] == 0.6667

    for cap in _caps(backend, 1):
        store.add_good(TOOL, cap)
    row = _row(registry, store, backend)
    assert row["good"]["state"] == "ready"
    assert row["good"]["support"] == {"count": 3, "required": 3,
                                      "ratio": 1.0}
    # Legacy row projection is untouched by per-cell readiness.
    assert row["state"] == "ready"
    assert row["labeled"] == 3


def test_good_independent_of_user_friction(registry, backend):
    store = PrototypeStore(min_labeled=3)
    # Three user-FRICTION samples alone must not make GOOD ready.
    for cap in _caps(backend, 3):
        store.add_friction(TOOL, cap)
    row = _row(registry, store, backend)
    assert row["good"]["state"] == "learning"
    assert row["good"]["support"]["count"] == 0
    assert row["friction"]["user_state"] == "ready"
    assert row["friction"]["dominant_source"] == "user"
    assert row["state"] == "ready"  # legacy labeled (GOOD+FRICTION)


# --- FRICTION: user/objective independent, never summed -----------------------

def test_objective_failure_never_increments_user_friction(registry, backend):
    store = PrototypeStore(min_labeled=3)
    for _ in range(3):
        assert store.record_tool_end(
            build_capsule(_end(TOOL, status="error"), backend=backend)
        ) == "objective_friction"
    bank = store.bank(TOOL)
    assert len(bank["objective_friction"]) == 3
    assert bank["friction"] == []
    row = _row(registry, store, backend)
    fric = row["friction"]
    assert fric["user"] is None
    assert fric["objective"] is not None
    assert fric["user_state"] == "learning"
    assert fric["objective_state"] == "ready"
    assert fric["dominant_source"] == "objective"
    assert fric["state"] == "ready"  # combined follows dominant source
    assert fric["support"] == {"count": 3, "required": 3, "ratio": 1.0}
    # Legacy labeled count ignores objective friction.
    assert store.labeled_count(TOOL) == 0
    assert row["labeled"] == 0
    assert row["state"] == "learning"


def test_objective_ready_user_provisional_combined_follows_dominant(
        registry, backend):
    store = PrototypeStore(min_labeled=3)
    for cap in _caps(backend, 1):
        store.add_friction(TOOL, cap)
    for _ in range(3):
        store.record_tool_end(
            build_capsule(_end(TOOL, status="error"), backend=backend))
    row = _row(registry, store, backend)
    fric = row["friction"]
    assert fric["user_support"]["count"] == 1
    assert fric["objective_support"]["count"] == 3
    assert fric["user_state"] == "provisional"
    assert fric["objective_state"] == "ready"
    # Combined state = state of the dominant source (greater score).
    assert fric["dominant_source"] in ("user", "objective")
    assert fric["state"] in ("provisional", "ready")
    # Counts are exposed separately; combined support is the dominant's.
    assert fric["support"]["count"] in (1, 3)
    dom = {"user": fric["user_support"],
           "objective": fric["objective_support"]}[fric["dominant_source"]]
    assert fric["support"] == dom


# --- GOAL: references without feedback; empty bank fabricates nothing -------

def test_goal_scores_with_references_zero_feedback(registry, backend):
    store = PrototypeStore(min_labeled=3)
    # Only objective-friction prototypes (operational evidence) exist:
    # no user feedback labels at all.
    for _ in range(2):
        store.record_tool_end(
            build_capsule(_end(TOOL, status="error"), backend=backend))
    row = _row(registry, store, backend)
    assert store.labeled_count(TOOL) == 0
    goal = row["goal"]
    assert goal["value"] is not None
    assert goal["state"] == "provisional"  # 2 references < 3 required
    assert goal["support"] == {"count": 2, "required": 3, "ratio": 0.6667}
    assert goal["level"] in ("weak", "strong")

    store.record_tool_end(
        build_capsule(_end(TOOL, status="error"), backend=backend))
    row = _row(registry, store, backend)
    assert row["goal"]["state"] == "ready"
    assert row["goal"]["support"]["count"] == 3


def test_empty_reference_bank_no_fabricated_goal(registry, backend):
    store = PrototypeStore(min_labeled=3)
    row = _row(registry, store, backend)
    goal = row["goal"]
    assert goal["value"] is None
    assert goal["state"] == "learning"
    assert goal["level"] == "neutral"
    assert goal["support"] == {"count": 0, "required": 3, "ratio": 0.0}


def test_goal_provisional_score_keeps_level(registry, backend):
    """One reference yields a numeric score with its true level even
    while the cell is provisional (the forced-neutral bug is gone)."""
    store = PrototypeStore(min_labeled=3)
    cap = build_capsule(_end(TOOL), args={"path": "config.py"},
                        backend=backend)
    store.add_good(TOOL, cap)
    row = _row(registry, store, backend,
               goal_text="read the config file please")
    goal = row["goal"]
    assert goal["state"] == "provisional"
    assert goal["value"] is not None
    expected_level = "strong" if goal["value"] >= 0.8 else "weak"
    assert goal["level"] == expected_level


def test_no_goal_vector_all_cells_empty(registry, backend):
    store = PrototypeStore(min_labeled=3)
    store.add_good(TOOL, _caps(backend, 1)[0])
    registry.observe_start(TOOL)
    row = build_matrix(registry, store)[0]
    assert row["goal"]["state"] == "empty"
    assert row["good"]["state"] == "empty"
    assert row["friction"]["state"] == "empty"
    assert row["friction"]["dominant_source"] is None


# --- phrase-table unification -------------------------------------------------

def test_single_phrase_table_consumed_by_both_paths():
    assert ("good", "great") in FEEDBACK_RULES
    assert ("good", "perfect") in FEEDBACK_RULES
    assert ("good", "excellent") in FEEDBACK_RULES
    # classify_feedback delegates to the same detector: one table.
    for text, expected in [("great", "good"), ("perfect", "good"),
                           ("excellent", "good"), ("thanks", "good"),
                           ("still broken", "friction"),
                           ("that was wrong", "friction"),
                           ("hmm, interesting", None), ("", None),
                           (None, None)]:
        assert classify_feedback(text) == expected == detect_feedback(text)
    assert classify_feedback("thank you " + "please " * 12) is None


def test_mixed_utterance_negative_precedence():
    assert detect_feedback("good job but still broken") == "friction"
    assert classify_feedback("brilliant, totally useless") == "friction"
    assert classify_feedback("perfect! except it failed") == "friction"


# --- bridge mirror parity -----------------------------------------------------

def _api(db):
    import importlib
    import dashboard.plugin_api as api
    importlib.reload(api)
    api.set_db_path(db)
    return api


_CELL_KEYS = ("value", "state", "level", "support", "support_count",
              "readiness_threshold")


def _compare_cells(live_row, fallback_row):
    for key in _CELL_KEYS:
        for cell in ("goal", "good"):
            assert (fallback_row[cell][key] == live_row[cell][key]
                    if key != "value" else
                    fallback_row[cell][key] == pytest.approx(
                        live_row[cell][key], abs=1e-6)
                    if live_row[cell][key] is not None else
                    fallback_row[cell][key] is None), (cell, key)
    fric_l, fric_f = live_row["friction"], fallback_row["friction"]
    for key in ("state", "level", "user", "objective", "dominant_source",
                "user_state", "objective_state", "user_support",
                "objective_support", "support", "support_count",
                "readiness_threshold"):
        lv, fv = fric_l[key], fric_f[key]
        if key in ("user", "objective") and isinstance(lv, float):
            assert fv == pytest.approx(lv, abs=1e-6), key
        else:
            assert lv == fv, (key, lv, fv)
    assert fric_l["value"] == fric_f["value"] or (
        fric_l["value"] is not None and fric_f["value"] is not None
        and fric_l["value"] == pytest.approx(fric_f["value"], abs=1e-6))
    assert live_row["state"] == fallback_row["state"]
    assert live_row["labeled"] == fallback_row["labeled"]


def test_db_fallback_equals_live_view(agent_env):
    import engine
    eng, db = agent_env
    _feed(eng, db)
    live = {r["tool_name"]: r for r in eng.pulse_view()["rows"]}
    api = _api(db)
    fallback = {r["tool_name"]: r for r in api._pulse_shape()["rows"]}
    assert set(live) == set(fallback)
    for tool, live_row in live.items():
        _compare_cells(live_row, fallback[tool])
    # One GOOD sample crossed live and fallback identically: provisional.
    assert live["read_file"]["good"]["state"] == "provisional"
    assert fallback["read_file"]["good"]["state"] == "provisional"
    assert live["terminal"]["friction"]["objective_state"] == "provisional"
    assert fallback["terminal"]["friction"]["objective_state"] == "provisional"


# --- fixtures mirroring the bridge suite -------------------------------------

@pytest.fixture
def agent_env(tmp_path):
    from storage import database
    db = tmp_path / "agent.db"
    database.set_override(db)
    import engine as _engine
    _engine._engine = None
    eng = _engine.get_engine()
    yield eng, db
    _engine._engine = None
    database.set_override(None)


def _feed(eng, db):
    import time
    from pulse import feedback
    now = time.time()
    eng.on_events([
        {"type": "tool_start", "tool_name": "terminal", "ts": now},
        {"type": "tool_start", "tool_name": "read_file", "ts": now},
    ])
    eng.on_events([
        {"type": "tool_end", "tool_name": "terminal", "status": "error",
         "duration_ms": 40, "ts": now + 0.1},
        {"type": "tool_end", "tool_name": "read_file", "status": "ok",
         "duration_ms": 10, "ts": now + 0.2},
    ])
    eng.on_events([
        {"type": "user_turn", "ts": now + 0.3,
         "user_message": "please fix the flaky pulse bridge test"},
    ])
    feedback.apply_feedback(eng.protos, eng.capsule_state(), "great job")
    eng.on_events([])


# --- Engine wiring: per-category settings reach both PrototypeStore sites ---

def test_engine_passes_per_category_readiness_settings():
    from engine import Engine
    eng = Engine(settings={
        "pulse.min_labeled": 3,
        "pulse.good_min_samples": 5,
        "pulse.goal_min_references": 2,
        "pulse.user_friction_min_samples": 1,
        "pulse.objective_friction_min_samples": 7,
    })
    assert eng.protos.readiness_required("goal") == 2
    assert eng.protos.readiness_required("good") == 5
    assert eng.protos.readiness_required("user_friction") == 1
    assert eng.protos.readiness_required("objective_friction") == 7
    # Unset categories fall back to the legacy min_labeled.
    eng = Engine(settings={"pulse.min_labeled": 4})
    for cat in ("goal", "good", "user_friction", "objective_friction"):
        assert eng.protos.readiness_required(cat) == 4


def test_engine_reset_rebootstrap_wires_per_category_settings():
    from engine import Engine
    eng = Engine()
    eng.reset(settings={
        "pulse.min_labeled": 3,
        "pulse.good_min_samples": 5,
        "pulse.objective_friction_min_samples": 1,
    })
    assert eng.protos.readiness_required("good") == 5
    assert eng.protos.readiness_required("objective_friction") == 1
    assert eng.protos.readiness_required("goal") == 3
