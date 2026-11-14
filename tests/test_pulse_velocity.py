"""C.1 / B: per-tool event-rate velocity + the /pulse tool_name backlog.

Pins:
 * event_velocity() counts persisted tool_start events in a rolling
   window (fast-decay momentum, free from the events table), cools as
   the window advances, excludes non-tool_start types, and is fault
   tolerant (DB down -> {}, never raises).
 * build_lanes(event_rates=...) makes the LIVE lane rank by velocity
   first — a tool that is surging NOW (many in-window events) beats an
   otherwise-identical tool that is idle, even when both share the same
   huge cumulative count (the capped-32 "cooling inversion" fix). The
   velocity field is additive; event_rates=None is byte-identical legacy.
 * _pulse_activity_events(tool_name=...) restricts the activity page to
   ONE tool's backlog (the pane's FILO flush reads a tool's unconsumed
   events from the DB instead of hoarding them); the default (None) is
   unchanged, and a direct-call FastAPI Query sentinel normalizes to
   None (regression guard for the C.1 route change).
"""
import sqlite3

import pytest

from pulse.velocity import event_velocity, PULSE_VELOCITY_WINDOW_S
from pulse.matrix import build_lanes
from dashboard import plugin_api as api
from storage import database
from storage import migration


# --- shared fakes (mirror test_pulse_lanes) ---------------------------------

class _FakeRegistry:
    def __init__(self, rows):
        self._rows = rows

    def list_tools(self):
        return self._rows


class _FakeProtos:
    def __init__(self, banks=None):
        self._banks = banks or {}
        self.min_labeled = 3
        self.goal_min_references = None
        self.good_min_samples = None
        self.user_friction_min_samples = None
        self.objective_friction_min_samples = None

    def bank(self, tool):
        return dict(self._banks.get(
            tool, {"good": [], "friction": [], "objective_friction": []}))

    def bank_meta(self, tool):
        return {}

    def state(self, tool):
        return "learning"

    def labeled_count(self, tool):
        b = self._banks.get(tool) or {}
        return len(b.get("good", [])) + len(b.get("friction", []))


def _row(tool, tid, completed=1, last=100.0):
    return {"tool_name": tool, "id": tid, "calls_started": completed,
            "calls_completed": completed, "errors": 0,
            "first_seen": 0.0, "last_seen": last}


def _caps(n):
    return [{"vector": [1.0, 0.0]} for _ in range(n)]


# --- env fixture (profile DB override, like test_pulse_activity_events) ------

@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "vel.db"
    database.set_override(db)
    api.set_db_path(db)
    import engine
    engine._engine = None
    conn = sqlite3.connect(str(db))
    migration.schema_bootstrap(conn, migration.EVENTS_DDL)
    yield db, conn
    conn.close()
    engine._engine = None
    database.set_override(None)
    api.set_db_path(None)


def _tool_start(conn, tool, ts, call_id=None):
    cur = conn.execute(
        "INSERT INTO events (ts, type, tool_name, tool_call_id)"
        " VALUES (?, 'tool_start', ?, ?)", (ts, tool, call_id))
    conn.commit()
    return cur.lastrowid


# =============================================================================
# C.1 — event_velocity()
# =============================================================================

def test_event_velocity_counts_in_window(env):
    _, conn = env
    now = 5000.0
    w = PULSE_VELOCITY_WINDOW_S
    # in-window (ts >= now - w): 3 for compute_use, 1 for terminal
    _tool_start(conn, "compute_use", now - 5)
    _tool_start(conn, "compute_use", now - 50)
    _tool_start(conn, "compute_use", now - 59)
    _tool_start(conn, "terminal", now - 1)
    # out-of-window: ts < now - w (should NOT count)
    _tool_start(conn, "compute_use", now - w - 10)
    _tool_start(conn, "terminal", now - w - 10)
    out = event_velocity(conn, now_ts=now)
    assert out["compute_use"]["count"] == 3
    assert out["terminal"]["count"] == 1
    assert out["compute_use"]["per_s"] == pytest.approx(3 / w, rel=1e-3)
    assert out["compute_use"]["window_s"] == PULSE_VELOCITY_WINDOW_S


def test_event_velocity_cools_as_window_advances(env):
    _, conn = env
    t0 = 1000.0
    _tool_start(conn, "terminal", t0)
    _tool_start(conn, "terminal", t0 + 10)
    # At now = t0 + 30 the two events are still inside a 60s window.
    assert event_velocity(conn, now_ts=t0 + 30)["terminal"]["count"] == 2
    # At now = t0 + 300 they have rolled out of the 60s window -> cooled.
    assert "terminal" not in event_velocity(conn, now_ts=t0 + 300)


def test_event_velocity_excludes_non_tool_start(env):
    _, conn = env
    now = 5000.0
    conn.execute("INSERT INTO events (ts, type, user_message)"
                 " VALUES (?, 'user_turn', 'x')", (now - 1,))
    conn.commit()
    _tool_start(conn, "read_file", now - 1)
    out = event_velocity(conn, now_ts=now)
    assert set(out) == {"read_file"}
    assert out["read_file"]["count"] == 1


def test_event_velocity_fault_returns_empty(monkeypatch):
    class _Boom:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("locked")
    out = event_velocity(_Boom(), now_ts=0.0)
    assert out == {}


# =============================================================================
# C.1 — build_lanes velocity ranking (the cooling-inversion fix)
# =============================================================================

def test_live_velocity_surging_tool_beats_idle_at_same_cumulative():
    # Both share a huge identical cumulative count AND a saturated 32-capsule
    # feed — the legacy capped momentum cannot tell them apart. Only the
    # in-window event-rate velocity reveals that `terminal` is surging now.
    reg = _FakeRegistry([_row("terminal", 1, completed=2574),
                         _row("idle_tool", 2, completed=2574)])
    protos = _FakeProtos({})
    caps = {"terminal": _caps(32), "idle_tool": _caps(32)}
    event_rates = {
        "terminal": {"count": 80, "per_s": round(80 / PULSE_VELOCITY_WINDOW_S, 4),
                     "window_s": PULSE_VELOCITY_WINDOW_S},
        "idle_tool": {"count": 0, "per_s": 0.0,
                      "window_s": PULSE_VELOCITY_WINDOW_S},
    }
    lanes = build_lanes(reg, protos, capsules_by_tool=caps,
                        event_rates=event_rates)
    live = lanes["live"]
    assert [e["tool_name"] for e in live] == ["terminal", "idle_tool"], \
        "velocity: the surging tool is rank 1 despite identical cumulative"
    assert live[0]["velocity"]["count"] == 80
    assert live[1]["velocity"]["count"] == 0
    assert [e["rank"] for e in live] == [1, 2]


def test_live_velocity_absent_falls_back_to_cumulative_recency():
    # No in-window events for either tool -> velocity all-zero -> the sort
    # degrades to cumulative/recency (deterministic), not velocity.
    reg = _FakeRegistry([_row("zeta", 1, completed=5, last=10.0),
                         _row("alpha", 2, completed=900, last=10.0)])
    event_rates = {"zeta": {"count": 0, "per_s": 0.0,
                            "window_s": PULSE_VELOCITY_WINDOW_S},
                   "alpha": {"count": 0, "per_s": 0.0,
                             "window_s": PULSE_VELOCITY_WINDOW_S}}
    lanes = build_lanes(reg, _FakeProtos({}), event_rates=event_rates)
    # alpha has the larger cumulative count -> rank 1 (tie on velocity=0).
    assert [e["tool_name"] for e in lanes["live"]] == ["alpha", "zeta"]


def test_live_velocity_field_is_additive():
    reg = _FakeRegistry([_row("hot", 1, completed=7),
                         _row("cold", 2, completed=1)])
    event_rates = {"hot": {"count": 12, "per_s": 0.2,
                           "window_s": PULSE_VELOCITY_WINDOW_S}}
    lanes = build_lanes(reg, _FakeProtos({}), event_rates=event_rates)
    hot = next(e for e in lanes["live"] if e["tool_name"] == "hot")
    cold = next(e for e in lanes["live"] if e["tool_name"] == "cold")
    # velocity present on every LIVE entry (absent tools default to 0)
    assert set(hot["velocity"]) == {"count", "per_s", "window_s"}
    assert cold["velocity"]["count"] == 0
    # legacy keys still intact alongside the additive one
    assert "momentum" in hot and "activity" in hot and "rank" in hot


def test_live_event_rates_none_is_legacy_byte_compatible():
    reg = _FakeRegistry([_row("terminal", 1, completed=2574),
                         _row("idle_tool", 2, completed=2574)])
    caps = {"terminal": _caps(32), "idle_tool": _caps(32)}
    legacy = build_lanes(reg, _FakeProtos({}), capsules_by_tool=caps)
    for e in legacy["live"]:
        assert "velocity" not in e, "event_rates=None must not add velocity"
    # Legacy capped-momentum tie -> alphabetical last-resort (the inversion
    # symptom): idle_tool sorts before terminal.
    assert [e["tool_name"] for e in legacy["live"]] == ["idle_tool", "terminal"]


# =============================================================================
# B — /pulse tool_name backlog filter
# =============================================================================

def test_activity_events_tool_name_filters_one_tool(env):
    _, conn = env
    base = api._pulse_activity_events(None)["next_cursor"]
    a1 = _tool_start(conn, "alpha", 2000.0)
    a2 = _tool_start(conn, "alpha", 2001.0)
    b1 = _tool_start(conn, "beta", 2002.0)
    only_alpha = api._pulse_activity_events(base, tool_name="alpha")
    assert [e["id"] for e in only_alpha["events"]] == [a1, a2]
    only_beta = api._pulse_activity_events(base, tool_name="beta")
    assert [e["id"] for e in only_beta["events"]] == [b1]
    # default (None) returns the global ASC page, unchanged.
    glob = api._pulse_activity_events(base)
    assert [e["id"] for e in glob["events"]] == [a1, a2, b1]


def test_activity_events_tool_name_empty_backlog(env):
    _, conn = env
    base = api._pulse_activity_events(None)["next_cursor"]
    _tool_start(conn, "gamma", 3000.0)
    out = api._pulse_activity_events(base, tool_name="never_used")
    assert out["events"] == [] and out["has_more"] is False


def test_pulse_route_direct_call_query_sentinel_normalizes_to_none(env):
    # Direct (non-ASGI) api.pulse() leaves the new tool_name param as the
    # unresolved FastAPI Query sentinel — it must normalize to None so the
    # global page is returned (regression guard for the C.1 route change).
    _, conn = env
    base = api._pulse_activity_events(None)["next_cursor"]
    i = _tool_start(conn, "terminal", 4000.0)
    import asyncio
    out = api.pulse(after_event_id=base)
    assert [e["id"] for e in out["activity_events"]["events"]] == [i]
    # And a real string filter still works through the same route helper.
    j = _tool_start(conn, "read_file", 4001.0)
    filtered = api._pulse_activity_events(base, tool_name="read_file")
    assert [e["id"] for e in filtered["events"]] == [j]
