"""Stage 3: four independent PULSE lanes (backend selectors + envelope).

Pins the v2 contract: each lane (LIVE/GOAL/GOOD/FRICTION) ranks its own
tools independently — different tools can be rank 1 in different lanes;
ties break deterministically; sub-hysteresis float noise never
reshuffles; ALL eligible tools are returned (desktop picks the prefix).
FRICTION carries independent user/objective parts. The dashboard /
pulse response is the additive v2 envelope (schema_version 2,
lane_order, lanes) with byte-compatible legacy rows/columns, and the
DB-down / no-tools case still returns the full envelope.
"""
import time

import pytest

from pulse.matrix import (
    HYSTERESIS, _assign_ranks, _q, _sem_sort_key, build_lanes,
    lanes_from_rows,
)

FORBIDDEN_ANIMATION_KEYS = {
    "destination_slot", "start", "end", "particles", "bubble", "animation",
    "transition", "travel_ms", "arrival_ms",
}


class _FakeRegistry:
    def __init__(self, rows):
        self._rows = rows

    def list_tools(self):
        return self._rows


class _FakeProtos:
    """Bank-by-tool fixture with per-category readiness attrs."""

    def __init__(self, banks, min_labeled=3, metas=None):
        self._banks = banks
        self._metas = metas or {}
        self.min_labeled = min_labeled
        self.goal_min_references: int | None = None
        self.good_min_samples: int | None = None
        self.user_friction_min_samples: int | None = None
        self.objective_friction_min_samples: int | None = None

    def bank(self, tool):
        return dict(self._banks.get(
            tool, {"good": [], "friction": [], "objective_friction": []}))

    def bank_meta(self, tool):
        """v0.1.10 provenance: label -> (backend, model_version) | None
        (None = legacy record without identifiable provenance)."""
        return dict(self._metas.get(tool, {}))

    def state(self, tool):
        return "learning"

    def labeled_count(self, tool):
        b = self._banks.get(tool) or {}
        return len(b.get("good", [])) + len(b.get("friction", []))


def _row(tool, tid, started=1, completed=1, errors=0, last=100.0):
    return {"tool_name": tool, "id": tid, "calls_started": started,
            "calls_completed": completed, "errors": errors,
            "first_seen": 0.0, "last_seen": last}


# --- independent first-place + all-eligible ---------------------------------

def test_each_lane_ranks_independently():
    reg = _FakeRegistry([
        _row("tool_goal", 1),
        _row("tool_good", 2),
        _row("tool_fric", 3),
        _row("tool_live", 4),
    ])
    protos = _FakeProtos({
        # tool_goal: capsule-only evidence (GOAL falls back to recent
        # capsules); 3 refs at value 1.0 beat tool_fric's 1 ref on the
        # support tie-break, without ever entering the FRICTION lane.
        "tool_goal": {"good": [], "friction": [],
                      "objective_friction": []},
        # tool_good: GOOD vector at cosine 0.7 (also a GOAL reference,
        # but weaker than tool_goal's 1.0).
        "tool_good": {"good": [[0.7, 0.714142842854285]], "friction": [],
                      "objective_friction": []},
        # tool_fric: objective friction at cosine 1.0.
        "tool_fric": {"good": [], "friction": [],
                      "objective_friction": [[1.0, 0.0]]},
        "tool_live": {"good": [], "friction": [], "objective_friction": []},
    })
    caps = {"tool_live": [{"vector": [0.0, 1.0]} for _ in range(9)],
            "tool_goal": [{"vector": [1.0, 0.0]} for _ in range(3)],
            "tool_good": [], "tool_fric": []}
    lanes = build_lanes(reg, protos, goal_vector=[1.0, 0.0],
                        capsules_by_tool=caps)
    assert set(lanes) == {"live", "goal", "good", "friction"}
    firsts = {lane: lanes[lane][0]["tool_name"] for lane in lanes}
    assert firsts["goal"] == "tool_goal"
    assert firsts["good"] == "tool_good"
    assert firsts["friction"] == "tool_fric"
    assert firsts["live"] == "tool_live"
    # All four firsts are different tools — proof of independence.
    assert len(set(firsts.values())) == 4


def test_all_eligible_tools_returned_with_one_based_ranks():
    names = ["alpha", "beta", "gamma", "delta"]
    reg = _FakeRegistry([_row(n, i + 1) for i, n in enumerate(names)])
    lanes = build_lanes(reg, _FakeProtos({}), goal_vector=[1.0, 0.0])
    for lane, entries in lanes.items():
        assert len(entries) == 4, lane
        assert {e["tool_name"] for e in entries} == set(names), lane
        assert [e["rank"] for e in entries] == [1, 2, 3, 4], lane


def test_live_entry_shape_and_momentum_states():
    reg = _FakeRegistry([_row("hot", 1), _row("cold", 2)])
    caps = {"hot": [{"vector": [1.0, 0.0]} for _ in range(8)],
            "cold": [{"vector": [1.0, 0.0]}]}
    lanes = build_lanes(reg, _FakeProtos({}), capsules_by_tool=caps)
    hot = next(e for e in lanes["live"] if e["tool_name"] == "hot")
    assert set(hot) == {"tool_id", "tool_name", "rank", "activity", "momentum"}
    assert set(hot["activity"]) == {
        "calls_started", "calls_completed", "errors", "first_seen",
        "last_seen", "recent_capsules"}
    assert set(hot["momentum"]) == {"momentum", "surge", "value", "state"}
    assert hot["momentum"]["state"] in ("surging", "steady", "cooling")
    assert 0.0 <= hot["momentum"]["value"] <= 1.0


# --- deterministic tie-break + hysteresis ------------------------------------

def test_deterministic_tie_break_equal_scores():
    reg = _FakeRegistry([_row("zeta", 5), _row("alpha", 3), _row("mid", 4)])
    protos = _FakeProtos(
        {n: {"good": [[1.0, 0.0]], "friction": [],
             "objective_friction": []} for n in ("zeta", "alpha", "mid")})
    lanes = build_lanes(reg, protos, goal_vector=[1.0, 0.0])
    order = [e["tool_name"] for e in lanes["good"]]
    assert order == ["alpha", "mid", "zeta"]  # equal values -> name order
    assert [e["rank"] for e in lanes["good"]] == [1, 2, 3]


def test_hysteresis_sub_noise_never_reshuffles():
    # Within one HYSTERESIS bucket, name order wins over raw float value.
    assert _q(0.5) == _q(0.5 + 5e-7)
    assert _q(0.5) != _q(0.5 + 2e-6)
    close = [
        {"tool_id": 2, "tool_name": "alpha", "rank": 0, "value": 0.5,
         "support": {"count": 1, "required": 3, "ratio": 1 / 3}},
        {"tool_id": 1, "tool_name": "zeta", "rank": 0, "value": 0.5 + 5e-7,
         "support": {"count": 1, "required": 3, "ratio": 1 / 3}},
    ]
    ordered = _assign_ranks([dict(e) for e in close], _sem_sort_key)
    assert ordered[0]["tool_name"] == "alpha"  # noise ignored, name wins
    far = [
        dict(close[0]),
        dict(close[1], value=0.5 + 2e-6),
    ]
    ordered2 = _assign_ranks(far, _sem_sort_key)
    assert ordered2[0]["tool_name"] == "zeta"  # meaningful gap wins
    assert HYSTERESIS > 0


# --- friction user/objective parts -------------------------------------------

def test_friction_user_objective_parts_independent():
    reg = _FakeRegistry([_row("browser", 7)])
    protos = _FakeProtos({
        "browser": {"good": [], "friction": [[1.0, 0.0]] * 3,
                    "objective_friction": [[1.0, 0.0]]},
    })
    lanes = build_lanes(reg, protos, goal_vector=[1.0, 0.0])
    (fr,) = lanes["friction"]
    assert set(fr) == {"tool_id", "tool_name", "rank", "value", "threshold",
                       "level", "state", "dominant_source", "user", "objective"}
    assert fr["threshold"] == 0.4
    assert fr["value"] == 1.0
    # Tie in value -> support ratio decides (user 3/3 = 1.0 > 1/3).
    assert fr["dominant_source"] == "user"
    assert fr["state"] == "ready"
    for part in ("user", "objective"):
        assert set(fr[part]) == {"value", "level", "state", "support"}
        assert set(fr[part]["support"]) == {"count", "required", "ratio"}
    assert fr["user"]["support"]["count"] == 3
    assert fr["objective"]["support"]["count"] == 1
    assert fr["objective"]["state"] == "provisional"

    # Objective-only evidence: user stays learning, never summed.
    protos2 = _FakeProtos({
        "browser": {"good": [], "friction": [],
                    "objective_friction": [[1.0, 0.0]] * 3},
    })
    (fr2,) = build_lanes(reg, protos2, goal_vector=[1.0, 0.0])["friction"]
    assert fr2["dominant_source"] == "objective"
    assert fr2["state"] == "ready"
    assert fr2["user"]["state"] == "learning"
    assert fr2["user"]["value"] is None
    assert fr2["objective"]["state"] == "ready"


def test_goal_and_good_reach_ready_independently():
    reg = _FakeRegistry([_row("t", 9)])
    protos = _FakeProtos({
        "t": {"good": [[1.0, 0.0]] * 3, "friction": [],
              "objective_friction": []},
    })
    lanes = build_lanes(reg, protos, goal_vector=[1.0, 0.0])
    assert lanes["good"][0]["state"] == "ready"
    assert lanes["good"][0]["evidence_source"] == "good_prototypes"
    assert lanes["goal"][0]["evidence"]["source"] == "prototype_banks"
    assert lanes["goal"][0]["evidence"]["prototype_count"] == 3


def test_no_animation_fields_in_lane_entries():
    reg = _FakeRegistry([_row("a", 1)])
    lanes = build_lanes(reg, _FakeProtos(
        {"a": {"good": [[1.0, 0.0]], "friction": [[1.0, 0.0]],
               "objective_friction": []}}), goal_vector=[1.0, 0.0])
    for entries in lanes.values():
        for entry in entries:
            assert not (set(entry) & FORBIDDEN_ANIMATION_KEYS)
            json_keys = __import__("json").dumps(entry)
            # No animation/bubble FIELD names anywhere in the entry
            # (keys only — values like calls_started are live data).
            import re
            for key in FORBIDDEN_ANIMATION_KEYS:
                assert not re.search(rf'"{re.escape(key)}"\s*:', json_keys)


def test_lanes_from_rows_matches_build_lanes_ordering():
    reg = _FakeRegistry([_row("b_tool", 2), _row("a_tool", 1)])
    protos = _FakeProtos({
        "b_tool": {"good": [[1.0, 0.0]], "friction": [],
                   "objective_friction": []},
        "a_tool": {"good": [], "friction": [[0.0, 1.0]],
                   "objective_friction": []},
    })
    from pulse.matrix import build_matrix
    rows = build_matrix(reg, protos, goal_vector=[1.0, 0.0])
    from_rows = lanes_from_rows(rows)
    direct = build_lanes(reg, protos, goal_vector=[1.0, 0.0])
    for lane in ("live", "goal", "good", "friction"):
        assert [e["tool_name"] for e in from_rows[lane]] == \
               [e["tool_name"] for e in direct[lane]], lane


# --- additive envelope (dashboard) -------------------------------------------

def test_db_down_envelope(tmp_path):
    from dashboard import plugin_api as api

    api.set_db_path(tmp_path / "down.db")
    shape = api._pulse_shape()
    assert shape["schema_version"] == 2
    assert shape["lane_order"] == ["live", "goal", "good", "friction"]
    assert shape["lanes"] == {"live": [], "goal": [], "good": [],
                              "friction": []}
    assert shape["rows"] == [] and shape["available"] is False
    assert shape["columns"] == ["goal", "good", "friction", "unlabeled"]


def test_envelope_additive_rows_unchanged(tmp_path, monkeypatch):
    import engine
    from dashboard import plugin_api as api
    from storage import database

    db = tmp_path / "env.db"
    database.set_override(db)
    engine._engine = None
    eng = engine.get_engine()
    now = time.time()
    eng.on_events([
        {"type": "tool_start", "tool_name": "terminal", "ts": now},
        {"type": "tool_end", "tool_name": "terminal", "status": "ok",
         "duration_ms": 10, "ts": now + 0.1},
    ])
    eng.on_events([{"type": "user_turn", "ts": now + 0.2,
                    "user_message": "please fix the lanes test"}])
    eng.on_events([])
    monkeypatch.setattr(api, "_pulse_live_view", lambda: None)
    api.set_db_path(db)
    shape = api._pulse_shape()
    # Envelope shape.
    assert shape["schema_version"] == 2
    assert shape["lane_order"] == ["live", "goal", "good", "friction"]
    assert set(shape["lanes"]) == {"live", "goal", "good", "friction"}
    assert shape["available"] is True
    # Legacy rows are the compatibility projection, still complete.
    assert len(shape["rows"]) == 1
    row = shape["rows"][0]
    assert set(row) == {"tool_name", "tool_id", "state", "labeled", "live",
                        "goal", "good", "friction", "momentum"}
    assert shape["columns"] == ["goal", "good", "friction", "unlabeled"]
    # Every lane carries the tool, ranked from 1.
    for entries in shape["lanes"].values():
        assert len(entries) == 1
        assert entries[0]["rank"] == 1
        assert entries[0]["tool_name"] == "terminal"
    engine._engine = None
    database.set_override(None)


# --- v0.1.10 (review P0-2): vector-space provenance gate -------------------
# Persisted vectors carry (backend, model_version) provenance. In an
# active embedding space, dimension-compatible vectors are SCORED but
# only KNOWN provenance equal to the active space counts toward
# readiness SUPPORT; legacy records (unknown provenance) are never
# silently assumed compatible. Legacy callers (no active space) keep
# the pre-gate behavior. This is the "numeric weak score with a ready
# state, despite an invalid comparison" defect class — pinned dead.

SKETCH_SPACE = {"id": "sketch", "model_version": 1, "dim": 4}
MINILM_SPACE = {"id": "minilm", "model_version": 2, "dim": 4}


def _lane_tool(lanes, lane, tool):
    return {e["tool_name"]: e for e in lanes[lane]}[tool]


def test_vector_space_provenance_gate():
    reg = _FakeRegistry([_row("terminal", 1), _row("read_file", 2)])
    banks = {
        "terminal": {"good": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]],
                     "friction": [], "objective_friction": []},
        "read_file": {"good": [], "friction": [], "objective_friction": []},
    }
    metas = {"terminal": {"good": ("sketch", 1)}}
    protos = _FakeProtos(banks, min_labeled=3, metas=metas)
    goal_vec = [1, 0, 0, 0]
    capsules = {"read_file": [
        {"vector": [1, 0, 0, 0], "backend": "sketch", "model_version": 1},
        {"vector": [0, 0, 1, 0], "backend": "minilm", "model_version": 2},
        {"vector": [0, 1, 0, 0]},  # legacy record: no provenance tag
    ]}

    def run(space, backend=None):
        return build_lanes(reg, protos, goal_vector=goal_vec,
                           capsules_by_tool=capsules,
                           vector_space=space, goal_backend=backend)

    # Legacy: no active space identified — every dimension-comparable
    # vector scores AND supports (pre-gate behavior preserved).
    lanes = run(None)
    g = _lane_tool(lanes, "good", "terminal")
    assert g["value"] is not None and g["state"] == "ready"
    assert g["support"]["count"] == 3
    gl = _lane_tool(lanes, "goal", "read_file")
    assert gl["evidence"]["source"] == "recent_capsules"
    assert gl["evidence"]["capsule_count"] == 3
    assert gl["state"] == "ready"
    gt = _lane_tool(lanes, "goal", "terminal")
    assert gt["evidence"]["source"] == "prototype_banks"
    assert gt["evidence"]["prototype_count"] == 3

    # Active space = sketch: the good bank's vectors share the sketch
    # label provenance (provenance is per-label in the store) — all
    # support; the capsule fallback only supports the sketch-tagged
    # capsule (unknown + minilm-tagged never count).
    lanes = run(SKETCH_SPACE)
    g = _lane_tool(lanes, "good", "terminal")
    assert g["state"] == "ready" and g["support"]["count"] == 3
    gl = _lane_tool(lanes, "goal", "read_file")
    assert gl["support"]["count"] == 1, \
        "only the sketch-tagged capsule supports (unknown + minilm never)"
    assert gl["state"] == "provisional", "1/3 support: numeric score, NOT ready"
    # Provisional short-fall: raise the readiness minimum — known
    # support (3) stays below it while the numeric score persists.
    protos.good_min_samples = 4
    lanes = run(SKETCH_SPACE)
    g = _lane_tool(lanes, "good", "terminal")
    assert g["value"] is not None, "comparable vectors still produce a score"
    assert g["state"] == "provisional", \
        "3/4 support: NOT ready (the old ready-with-invalid-comparison hole)"
    protos.good_min_samples = None

    # Encoder switch to minilm: the sketch-provenanced vectors lose ALL
    # support but remain dimension-comparable — they score without
    # ever counting. The minilm-tagged capsule now supports.
    lanes = run(MINILM_SPACE)
    g = _lane_tool(lanes, "good", "terminal")
    assert g["value"] is not None, "same-dim foreign-space vectors still score"
    assert g["support"]["count"] == 0
    assert g["state"] == "learning", \
        "zero support in the active space: learning, not ready"
    gl = _lane_tool(lanes, "goal", "read_file")
    assert gl["support"]["count"] == 1, \
        "the minilm-tagged capsule now supports; sketch/legacy do not"

    # Goal-vector gate: a goal vector from a KNOWN different encoder
    # is not comparable at all — gated out before any lane is built.
    lanes = run(SKETCH_SPACE, backend="minilm")
    assert _lane_tool(lanes, "goal", "terminal")["state"] == "empty"
    assert _lane_tool(lanes, "goal", "read_file")["state"] == "empty"
    assert _lane_tool(lanes, "good", "terminal")["state"] == "empty"
    assert _lane_tool(lanes, "friction", "terminal")["state"] == "empty"

    # Dimension mismatch: an 8-dim vector in a 4-dim space is excluded
    # from scoring and support alike (its label provenance is moot).
    banks["terminal"]["good"].append([0.1] * 8)
    lanes = run(SKETCH_SPACE)
    g = _lane_tool(lanes, "good", "terminal")
    assert g["support"]["count"] == 3, \
        "the 8-dim vector contributes nothing to the 4-dim space"
