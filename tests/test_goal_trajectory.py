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
    # A later aligned op close to the earlier exploration promotes it.
    ev.add_intent("c2", "read_file", "read_file(path:str)",
                  V_FOLLOW, 4.0, st)
    ev.complete("c2", "ok", 5.0, st)
    assert ev.operations[0].category == "productive_detour"
    assert ev.signal()["detours"] == 1


def test_sustained_drift_then_recovery():
    st = gt.ObjectiveState()
    gt.update_objective(st, V_ALIGN, 1.0)
    ev = gt.TrajectoryEvaluator()
    for i, vec in enumerate((V_FAR, V_FAR, V_FOLLOW)):
        ev.add_intent("c%d" % i, "terminal", "terminal(command:str)",
                      vec, 2.0 + i, st)
        ev.complete("c%d" % i, "ok", 2.5 + i, st)
    assert ev.operations[0].category == "drifting"
    assert ev.operations[1].category == "drifting"
    assert ev.signal()["category"] == "recovering"


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
    assert len(loaded["operations"]) == 1
    assert loaded["operations"][0]["call_id"] == "c1"


def test_intention_blend_is_not_a_self_echo():
    op = [0.0, 1.0]
    blended = gt.blend_intention(V_ALIGN, op)
    assert blended is not None
    assert 0.0 < (gt.cosine(blended, V_ALIGN) or 0) < 0.99
    assert gt.blend_intention(V_ALIGN, None) is None
