"""D1-D6: the user-upset pseudo-entry on the FRICTION lane.

Additive coverage on top of tests/test_pulse_lanes.py (same fake
harness): when the engine's upset view is in the D4 lane tier or
above, build_lanes gains ONE ``user`` pseudo-entry (no registry tool,
no banks) whose value is F' itself; below the tier — or the legacy
call without an upset view — the lanes are byte-identical to the
pre-accumulator behavior.
"""
from pulse.matrix import build_lanes


class _Reg:
    def __init__(self, rows):
        self._rows = rows

    def list_tools(self):
        return self._rows


class _Protos:
    min_labeled = 3

    def bank(self, tool):
        return {"good": [], "friction": [], "objective_friction": []}

    def bank_meta(self, tool):
        return {}

    def state(self, tool):
        return "learning"

    def labeled_count(self, tool):
        return 0


def _reg():
    return _Reg([
        {"tool_name": "read_file", "id": 1, "calls_started": 3,
         "calls_completed": 3, "errors": 0, "first_seen": 0.0,
         "last_seen": 10.0}])


def _upset(value=0.8, tier=2, turns=4):
    return {"value": value, "tier": tier, "upset_turns": turns,
            "mood": 0.4, "prognosis": None}


def test_friction_lane_user_entry_at_tier():
    lanes = build_lanes(_reg(), _Protos(), upset=_upset())
    user = [e for e in lanes["friction"] if e.get("upset")]
    assert len(user) == 1
    e = user[0]
    assert e["tool_name"] == "user"
    assert e["tool_id"] == -1
    assert e["value"] == 0.8
    assert e["dominant_source"] == "user"
    assert e["user"]["value"] == 0.8
    assert e["user"]["support"]["count"] == 4
    assert e["objective"]["value"] is None   # an honest absence, never invented


def test_friction_lane_no_user_entry_below_tier():
    lanes = build_lanes(
        _reg(), _Protos(),
        upset={"value": 0.2, "tier": 0, "upset_turns": 0,
               "mood": 0.5, "prognosis": None})
    assert not [e for e in lanes["friction"] if e.get("upset")]


def test_friction_lane_legacy_path_byte_untouched():
    """upset omitted (None) -> no user entry at all: the pre-
    accumulator lanes are unchanged (the owner's byte-identity rule)."""
    lanes = build_lanes(_reg(), _Protos())
    assert not [e for e in lanes["friction"] if e.get("upset")]
    names = [e["tool_name"] for e in lanes["friction"]]
    assert names == ["read_file"]


def test_user_entry_ranks_first_when_strongest():
    lanes = build_lanes(_reg(), _Protos(),
                        upset=_upset(value=0.9, tier=2))
    top = lanes["friction"][0]
    assert top["tool_name"] == "user"
    assert top["rank"] == 1
    assert top["level"] == "strong"   # 0.9 >= the 0.4 friction threshold


def test_user_entry_never_leaks_into_other_lanes():
    lanes = build_lanes(_reg(), _Protos(), upset=_upset())
    for lane in ("live", "goal", "good"):
        assert not [e for e in lanes[lane] if e.get("upset")]


# -- D3 cross-process handoff (the isolated dashboard serves the mirror) ---- #
def test_fallback_surfaces_user_entry_from_persisted_mirror(tmp_path):
    """End-to-end handoff: the agent half persists the F' mirror to the
    profile DB; the isolated dashboard (NO live engine) reconstructs the
    friction-lane ``user`` pseudo-entry + the top-level ``upset`` block
    from that mirror — this is what finally makes the pane's friction
    lane + glitch reachable."""
    import sqlite3
    import time
    from storage import database
    from storage import migration as _mig
    from dashboard import plugin_api as api
    from pulse.upset import UpsetAccumulator

    db = tmp_path / "mirror.db"
    database.set_override(db)
    api.set_db_path(db)
    try:
        conn = sqlite3.connect(str(db))
        _mig.schema_bootstrap(conn)
        conn.execute(
            "INSERT INTO pulse_tools (id, tool_name, first_seen, last_seen,"
            " calls_started, calls_completed, errors)"
            " VALUES (1, 'terminal', 0, 10, 3, 3, 0)")
        conn.commit()
        # The agent half's handoff: an upset turn is stepped and the F'
        # mirror is persisted (only derived numbers cross the boundary).
        acc = UpsetAccumulator()
        acc._session("s1", 0.0)
        st = acc._sessions["s1"]
        acc._step(st, 1.0)
        acc._step(st, 0.5)
        st["upset_turns"] = 1
        acc.persist_state(conn, ts=time.time(), session_id="s1")
        conn.close()
        shape = api._pulse_shape()
        assert shape["available"] is True
        user = [e for e in shape["lanes"]["friction"] if e.get("upset")]
        assert len(user) == 1
        assert user[0]["tool_name"] == "user"
        assert user[0]["tool_id"] == -1
        assert user[0]["value"] > 0.0
        assert shape["upset"]["tier"] >= 1
    finally:
        database.set_override(None)
