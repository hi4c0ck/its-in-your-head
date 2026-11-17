"""Gates for the metrics emulation suite.

These exercise the REAL engine with short synthetic events; no scoring is
reimplemented. Runs here use 12 turns for speed. Full 80-turn stories are
produced by the CLI (see README.md).
"""
from __future__ import annotations

import json
from pathlib import Path

from tests.metrics_emulation import harness, schema, stories

ROOT = Path(__file__).resolve().parents[1]
TURNS = 12


def test_real_engine_flow():
    run = harness.run_story("friction-recovery", turns=TURNS, seed=1,
                            include_provenance=False)
    assert run["schema"] == schema.SCHEMA_ID
    assert len(run["turns"]) == TURNS
    last = run["turns"][-1]
    assert last["portrait"]["available"] is True
    assert set(last["portrait"]["dimensions"]) == set(schema.DIMS)
    assert all(isinstance(v, (int, float)) for v in last["portrait"]["dimensions"].values())
    assert len(last["drift"]["series"]["directness"]) == TURNS
    assert last["pulse"]["rows"] > 0
    assert set(last["pulse"]["lanes"]) == {"live", "goal", "good", "friction"}
    assert isinstance(last["pulse"]["upset"].get("value"), (int, float))


def test_no_rescoring_in_harness():
    src = (ROOT / "tests" / "metrics_emulation" / "harness.py").read_text(encoding="utf-8")
    assert "engine.Engine()" in src and "eng.on_events(" in src
    for forbidden in ("build_lanes(", "build_matrix(", "resemblance_all(",
                      "cosine(", "update_observed("):
        assert forbidden not in src, forbidden
    views = (ROOT / "tests" / "metrics_emulation" / "schema.py").read_text(encoding="utf-8")
    assert "eng.portrait_view()" not in views  # extraction lives in harness only


def test_all_three_stories_valid():
    for sid in stories.STORY_ORDER:
        run = harness.run_story(sid, turns=TURNS, seed=7, include_provenance=False)
        assert run["run"]["story"] == sid
        assert run["run"]["drawing"]["template"] in (
            "neutral-pencil", "angular-pencil", "soft-flow-study")
        assert run["corners"], sid
        assert [c["turn"] for c in run["corners"]] == sorted(
            c["turn"] for c in run["corners"])
        assert run["summary"]["portrait_span"]
        assert run["summary"]["friction_peak"] is not None


def test_explicit_feedback_drives_good_and_friction_lanes():
    """Regression: feedback association must reach the real prototype banks.

    Before the collector-path fix, GOOD was 100% 0.0 and FRICTION was 0.0
    except the ~0.3 upset pseudo-entry. Both lanes must carry graded values.
    """
    run = harness.run_story("friction-recovery", turns=24, seed=3,
                            include_provenance=False)

    def values(lane):
        return {round(e["value"], 3) for t in run["turns"]
                for e in t["pulse"]["lanes"][lane]
                if isinstance(e.get("value"), (int, float))}

    assert len(values("good")) > 1, values("good")
    assert len(values("friction")) > 1, values("friction")
    src = (ROOT / "tests" / "metrics_emulation" / "harness.py").read_text(encoding="utf-8")
    assert "pulse_feedback.apply_feedback" in src
    assert "detect_feedback(prompt)" in src


def test_speedrun_forces_engine_polls_at_chosen_times():
    r = harness.run_story("friction-recovery", turns=12, seed=5,
                          poll_offsets=(0.0, 0.6, 0.85, 2.0))
    assert r["speedrun"]["enabled"] is True
    assert r["speedrun"]["poll_offsets"] == [0.0, 0.6, 0.85, 2.0]
    assert len(r["turns"][0]["polls"]) == 4
    assert [p["at"] for p in r["turns"][0]["polls"]] == [0.0, 0.6, 0.85, 2.0]
    engine_src = (ROOT / "engine.py").read_text(encoding="utf-8")
    assert "def pulse_view(self, now: float | None = None)" in engine_src
    assert "build_lanes(**common, now=now)" in engine_src


def test_story_prompts_come_from_repo_vocabulary():
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from pulse import feedback
    anch = (ROOT / "perception" / "resemblance.py").read_text(encoding="utf-8")
    phrases = set()
    for p in stories.STORIES.values():
        for ph in p["phases"]:
            phrases.update(ph["prompts"])
    rules = {phrase for _, phrase in feedback.FEEDBACK_RULES}
    for text in phrases:
        assert text in rules or text in anch or text in {
            "fix the login", "bad", "still broken", "still failing",
            "it failed", "do it again", "wrong"}, text


def test_deterministic_seed_and_roundtrip(tmp_path):
    a = harness.run_story("volatile-amplification", turns=TURNS, seed=99,
                          include_provenance=True)
    b = harness.run_story("volatile-amplification", turns=TURNS, seed=99,
                          include_provenance=True)
    assert [t["event_summary"] for t in a["turns"]] == [t["event_summary"] for t in b["turns"]]
    assert [c["turn"] for c in a["corners"]] == [c["turn"] for c in b["corners"]]
    assert a["run"]["drawing"] == b["run"]["drawing"]
    p = harness.write_run(a, tmp_path)
    loaded = harness.load_run(p)
    assert loaded["schema"] == schema.SCHEMA_ID
    assert json.loads(p.read_text(encoding="utf-8"))["run"]["seed"] == 99
