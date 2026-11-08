"""PULSE tool-row momentum (Tier-0 positive feedback).

Pins the surge-momentum signal: a tool hotter than its long-run baseline
surfaces as 'surging' (value -> 1.0, ranked to the front) so a
terminal -> compute_use shift jumps forward in Sketch mode where the
semantic cosines are near 0. Also guards that the dashboard child's inline
mirror (_pulse_momentum) stays byte-equivalent to pulse/momentum.py.
"""
import pytest

from pulse.momentum import (
    MOMENTUM_BAND, surge_values, empty_momentum,
)
from pulse.matrix import build_matrix


# --- pure signal -----------------------------------------------------------

def test_terminal_dominant_then_compute_use_surges():
    """The user's exact case: terminal owned the long run, compute_use is
    now surging -> compute_use is hotter than baseline (value > 0.5),
    terminal cools below it (value < 0.5). Raw share-delta mapping."""
    recent = {"terminal": 2, "compute_use": 8}
    cum = {"terminal": 10, "compute_use": 2}
    res = surge_values(recent, cum)
    assert res["compute_use"]["state"] == "surging"
    assert res["compute_use"]["value"] == 0.8167
    assert res["compute_use"]["momentum"] == 0.6333
    assert res["terminal"]["state"] == "cooling"
    assert res["terminal"]["value"] == 0.1833
    assert res["terminal"]["momentum"] == -0.6333


def test_steady_when_recent_matches_long_run():
    res = surge_values({"a": 5, "b": 5}, {"a": 5, "b": 5})
    assert res["a"]["state"] == "steady"
    assert res["a"]["value"] == 0.5
    assert res["a"]["surge"] == 0.0


def test_empty_inputs_are_all_steady_05():
    res = surge_values({}, {})
    assert res == {}
    e = empty_momentum()
    assert e == {"momentum": 0.0, "surge": 0.0, "value": 0.5, "state": "steady"}


def test_deterministic_under_key_shuffle():
    import random
    recent = {"terminal": 2, "compute_use": 8, "editor": 3}
    cum = {"terminal": 10, "compute_use": 2, "editor": 6}
    base = surge_values(recent, cum)

    def shuffled(d):
        ks = list(d)
        random.shuffle(ks)
        return {k: d[k] for k in ks}

    for seed in range(8):
        got = surge_values(shuffled(recent), shuffled(cum))
        assert got == base, f"seed {seed} drifted"


def test_mild_surge_stays_near_steady_raw_mapping():
    """No expanding curve: a mild surger (small share-delta) reads just
    above 0.5 instead of being inflated toward 1.0. Simultaneous surgers
    spread by their real magnitude."""
    res = surge_values({"a": 2, "b": 4, "c": 6, "d": 8},
                       {"a": 6, "b": 6, "c": 6, "d": 6})
    # recent total 20 -> a .10 b .20 c .30 d .40; cum total 24 -> all .25.
    # surge: a -.15 b -.05 c +.05 d +.15 -> value = 0.5 + 0.5*surge.
    assert res["c"]["state"] == "surging" and res["a"]["state"] == "cooling"
    assert res["a"]["value"] == 0.425
    assert res["b"]["value"] == 0.475
    assert res["c"]["value"] == 0.525
    assert res["d"]["value"] == 0.575
    # nothing saturates at 1.0 unless the share-delta truly reaches 1.0.
    assert all(0.0 <= v["value"] < 0.9 for v in res.values())


# --- build_matrix attaches a momentum cell --------------------------------

class _FakeRegistry:
    def __init__(self, rows):
        self._rows = rows

    def list_tools(self):
        return self._rows


class _FakeProtos:
    def bank(self, tool):
        return {"good": [], "friction": [], "objective_friction": []}

    def state(self, tool):
        return "learning"

    def labeled_count(self, tool):
        return 0


def _rows():
    return [
        {"tool_name": "terminal", "id": 1, "calls_started": 10,
         "calls_completed": 10, "errors": 0, "first_seen": 0.0, "last_seen": 1.0},
        {"tool_name": "compute_use", "id": 2, "calls_started": 8,
         "calls_completed": 8, "errors": 0, "first_seen": 0.0, "last_seen": 2.0},
    ]


def test_build_matrix_attaches_momentum_per_row():
    reg = _FakeRegistry(_rows())
    protos = _FakeProtos()
    caps = {"terminal": [{"vector": [1.0, 0.0]}],
            "compute_use": [{"vector": [1.0, 0.0]} for _ in range(8)]}
    rows = build_matrix(reg, protos, capsules_by_tool=caps)
    by_tool = {r["tool_name"]: r for r in rows}
    assert set(by_tool) == {"terminal", "compute_use"}
    for name, r in by_tool.items():
        m = r["momentum"]
        assert set(m) == {"momentum", "surge", "value", "state"}
        assert 0.0 <= m["value"] <= 1.0
    # compute_use surges (8 recent vs low long-run share), terminal cools.
    assert by_tool["compute_use"]["momentum"]["state"] == "surging"
    assert by_tool["terminal"]["momentum"]["state"] == "cooling"


def test_build_matrix_momentum_neutral_without_capsules():
    reg = _FakeRegistry(_rows())
    rows = build_matrix(reg, _FakeProtos(), capsules_by_tool={})
    by_tool = {r["tool_name"]: r for r in rows}
    # No recent feed -> recent_share 0 for every tool -> all cool below
    # their long-run share; the shape is still present and well-formed.
    for r in rows:
        assert r["momentum"]["state"] in ("steady", "cooling")
        assert r["momentum"]["value"] <= 0.5 + 1e-9


# --- mirror sync: the dashboard child must match pulse/momentum.py --------

def test_dashboard_mirror_matches_pulse_momentum():
    from dashboard.plugin_api import _pulse_momentum
    recent = {"terminal": 2, "compute_use": 8, "editor": 3}
    cum = {"terminal": 10, "compute_use": 2, "editor": 6}
    assert _pulse_momentum(recent, cum) == surge_values(recent, cum)
    # and the degenerate shapes agree too
    assert _pulse_momentum({}, {}) == surge_values({}, {})
    assert _pulse_momentum({"x": 4}, {}) == surge_values({"x": 4}, {})
