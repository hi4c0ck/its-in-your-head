"""Global GOAL trajectory model — pure logic + persistence roundtrip."""
from __future__ import annotations

from pathlib import Path

import pytest

from pulse import goal_trajectory as gt
from pulse import persistence as pers
import metrics


V_ALIGN = [1.0, 0.0]
V_FOLLOW = [0.95, 0.31]
V_EXPLORE = [0.5, 0.866]
V_NEW = [-0.9, 0.44]
V_FAR = [-1.0, 0.0]
V_ORTHOGONAL = [0.0, 1.0]


def test_followup_keeps_objective_continuity():
    st = gt.ObjectiveState()
    gt.update_objective(st, V_ALIGN, 1.0)
    gt.update_objective(st, V_FOLLOW, 2.0)
    assert st.version == 2
    assert len(st.history) == 1
    assert (gt.cosine(st.vector, V_ALIGN) or 0) > 0.8
    assert st.ts == 2.0


def test_new_topic_moves_more_but_keeps_history():
    st = gt.ObjectiveState()
    gt.update_objective(st, V_ALIGN, 1.0)
    gt.update_objective(st, V_NEW, 2.0)
    distance = 1.0 - (gt.cosine(st.vector, V_ALIGN) or 0)
    assert distance > 0.6
    assert [v.version for v in st.history] == [1]


def test_pre_post_link_and_retrospective_detour():
    st = gt.ObjectiveState()
    gt.update_objective(st, V_ALIGN, 1.0)
    ev = gt.TrajectoryEvaluator()
    ev.add_intent("c1", "search_files", "search_files(query:str)",
                  V_EXPLORE, 2.0, st)
    assert ev.pending["c1"]["historical_assessment"] is not None
    rec = ev.complete("c1", "ok", 3.0, st)
    assert rec is not None and rec.category == "exploring"
    assert rec.historical_category == "exploring"
    assert rec.result_assessment == rec.current_assessment
    # A later operation aligned with the objective confirms the detour.
    ev.add_intent("c2", "read_file", "read_file(path:str)",
                  V_FOLLOW, 4.0, st)
    ev.complete("c2", "ok", 5.0, st)
    assert ev.operations[0].category == "productive_detour"
    assert ev.signal(st)["detours"] == 1


def test_detour_not_promoted_by_similarity_alone():
    st = gt.ObjectiveState()
    gt.update_objective(st, V_ALIGN, 1.0)
    ev = gt.TrajectoryEvaluator()
    ev.add_intent("c1", "search_files", "search_files(query:str)",
                  V_EXPLORE, 2.0, st)
    ev.complete("c1", "ok", 3.0, st)
    # Similar to c1 but NOT aligned with the objective: no confirmation.
    ev.add_intent("c2", "read_file", "read_file(path:str)",
                  V_EXPLORE, 4.0, st)
    ev.complete("c2", "ok", 5.0, st)
    assert ev.operations[0].category == "exploring"


def test_success_is_not_progress_evidence():
    st = gt.ObjectiveState()
    gt.update_objective(st, V_ALIGN, 1.0)
    ev = gt.TrajectoryEvaluator()
    ev.add_intent("c1", "terminal", "terminal(command:str)",
                  V_FAR, 2.0, st)
    rec = ev.complete("c1", "ok", 3.0, st)
    assert rec is not None and rec.category == "drifting"
    assert rec.current_assessment == 0.0


def test_historical_category_survives_objective_change():
    st = gt.ObjectiveState()
    gt.update_objective(st, V_ALIGN, 1.0)
    ev = gt.TrajectoryEvaluator()
    ev.add_intent("c1", "terminal", "terminal(command:str)",
                  V_ALIGN, 2.0, st)
    ev.complete("c1", "ok", 3.0, st)
    old = ev.operations[0]
    assert old.historical_category == "aligned"
    gt.update_objective(st, V_FAR, 4.0, new_topic=True)
    ev.reevaluate(st)
    assert old.historical_category == "aligned"
    assert old.category == "drifting"


def test_sustained_drift_then_recovery():
    st = gt.ObjectiveState()
    gt.update_objective(st, V_ALIGN, 1.0)
    ev = gt.TrajectoryEvaluator()
    for i, vec in enumerate((V_FAR, V_FAR, V_FOLLOW)):
        ev.add_intent("c%d" % i, "terminal", "terminal(command:str)",
                      vec, 2.0 + i, st)
        ev.complete("c%d" % i, "ok", 2.5 + i, st)
    assert ev.signal(st)["category"] == "recovering"


def test_pending_intents_are_bounded_and_expire():
    st = gt.ObjectiveState()
    gt.update_objective(st, V_ALIGN, 1.0)
    ev = gt.TrajectoryEvaluator()
    for i in range(gt.PENDING_MAX + 8):
        ev.add_intent("c%d" % i, "terminal", "terminal(command:str)",
                      V_ALIGN, 2.0 + i, st)
    assert len(ev.pending) == gt.PENDING_MAX
    assert "c0" not in ev.pending
    ev.add_intent("fresh", "terminal", "terminal(command:str)",
                  V_ALIGN, 1000.0, st)
    assert len(ev.pending) <= gt.PENDING_MAX
    assert "fresh" in ev.pending


def test_signal_uses_current_objective_version_and_history_count():
    st = gt.ObjectiveState()
    gt.update_objective(st, V_ALIGN, 1.0)
    ev = gt.TrajectoryEvaluator()
    ev.add_intent("c1", "read_file", "read_file(path:str)",
                  V_ALIGN, 2.0, st)
    ev.complete("c1", "ok", 2.5, st)
    gt.update_objective(st, V_FOLLOW, 3.0)
    ev.reevaluate(st)
    signal = ev.signal(st)
    assert signal["version"] == st.version == 2
    assert signal["history_count"] == 1


def test_trajectory_persistence_roundtrip(tmp_path: Path):
    metrics.set_db_path(tmp_path / "iyyh.db")
    st = gt.ObjectiveState()
    gt.update_objective(st, V_ALIGN, 1.0)
    gt.update_objective(st, V_FOLLOW, 2.0)
    ev = gt.TrajectoryEvaluator()
    ev.add_intent("c1", "terminal", "terminal(command:str)",
                  V_FOLLOW, 3.0, st)
    ev.complete("c1", "ok", 3.5, st)
    pers.persist_trajectory(st, ev.operations)
    loaded = pers.load_trajectory_state()
    assert loaded["objective"]["version"] == st.version
    assert [v["version"] for v in loaded["objective"]["history"]] == [1]
    assert len(loaded["operations"]) == 1
    assert loaded["operations"][0]["call_id"] == "c1"
    assert loaded["operations"][0]["ended_ts"] == 3.5
    assert loaded["operations"][0]["historical_category"] == "aligned"


def test_production_descriptors_separate_relevant_from_unrelated():
    """REAL sanitizer output from REAL tool args — not preconstructed strings.

    The privacy-strict vocabulary (action class + tool + kind: category +
    arg key/type tags) separates at the action/category level. Command and
    query values contribute no tokens, so terminal/search-only operations
    are ambiguous (unmeasured) — never a drift claim.
    """
    import json

    from core import events as core_events
    from perception.encoder import Tier0SketchBackend

    fixture = json.loads(
        (Path(__file__).parent / "fixtures"
         / "production_operation_descriptors.json").read_text(encoding="utf-8"))
    backend = Tier0SketchBackend()
    (objective_vector,) = backend.encode([fixture["objective"]])
    scored = {}
    for case in fixture["cases"]:
        descriptor = core_events.sanitize_operation(case["tool"], case["args"])
        if not core_events.has_operation_semantics(descriptor):
            scored[case["id"]] = (None, case)
            continue
        (vector,) = backend.encode([descriptor])
        scored[case["id"]] = (
            round(gt._assessment(vector, objective_vector) or 0.0, 4), case)
    relevant = [v for v, c in scored.values()
                if c["class"] == "relevant" and v is not None]
    unrelated = [v for v, c in scored.values()
                 if c["class"] == "unrelated" and v is not None]
    assert relevant and unrelated
    assert min(relevant) - max(unrelated) > 0.05
    ambiguous = [v for v, c in scored.values() if c["class"] == "ambiguous"]
    assert ambiguous and all(v is None for v in ambiguous)


def test_success_without_alignment_is_not_progress():
    """A successful irrelevant operation is execution evidence only."""
    import json

    from core import events as core_events
    from perception.encoder import Tier0SketchBackend

    fixture = json.loads(
        (Path(__file__).parent / "fixtures"
         / "production_operation_descriptors.json").read_text(encoding="utf-8"))
    backend = Tier0SketchBackend()
    (objective_vector,) = backend.encode([fixture["objective"]])
    st = gt.ObjectiveState()
    st.version = 1
    st.vector = objective_vector
    ev = gt.TrajectoryEvaluator()
    case = next(c for c in fixture["cases"] if c["id"] == "pizza-browser")
    descriptor = core_events.sanitize_operation(case["tool"], case["args"])
    (vector,) = backend.encode([descriptor])
    ev.add_intent("real-1", case["tool"], descriptor,
                  vector, 1.0, st)
    rec = ev.complete("real-1", "ok", 2.0, st)
    assert rec is not None and rec.category == "drifting"
    assert (rec.current_assessment or 0.0) < gt.ALIGN_THRESHOLD


def test_shared_signal_matches_evaluator():
    st = gt.ObjectiveState()
    gt.update_objective(st, V_ALIGN, 1.0)
    gt.update_objective(st, V_FOLLOW, 2.0)
    ev = gt.TrajectoryEvaluator()
    ev.add_intent("c1", "read_file", "read_file(path:str)",
                  V_FOLLOW, 3.0, st)
    ev.complete("c1", "ok", 3.5, st)
    snapshot = {"objective": st.snapshot(),
                "operations": [r.to_dict() for r in ev.operations]}
    assert gt.signal_from_state(snapshot["objective"],
                                snapshot["operations"]) == ev.signal(st)


def test_dashboard_fallback_uses_shared_evaluator(tmp_path: Path):
    """DB fallback and live engine must produce the identical signal."""
    import dashboard.plugin_api as api

    db = tmp_path / "iyyh.db"
    metrics.set_db_path(db)
    api.set_db_path(db)
    st = gt.ObjectiveState()
    gt.update_objective(st, V_ALIGN, 1.0)
    gt.update_objective(st, V_FOLLOW, 2.0)
    ev = gt.TrajectoryEvaluator()
    ev.add_intent("c1", "read_file",
                  "action:read tool:read_file kind:code arg:path:str",
                  V_FOLLOW, 3.0, st)
    ev.complete("c1", "ok", 3.5, st)
    pers.persist_trajectory(st, ev.operations)
    loaded = pers.load_trajectory_state()
    expected = gt.signal_from_state(loaded["objective"],
                                    loaded["operations"])
    assert api._pulse_shape()["goal_signal"] == expected
    assert api._pulse_shape()["goal_signal"] == ev.signal(st)
