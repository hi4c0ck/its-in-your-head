"""C.1: per-tool event-rate velocity for the LIVE lane.

The LIVE momentum is computed from PERSISTED event throughput (free,
native): a rolling-window count of ``tool_start`` events per tool. A
massive ungraded batch (e.g. many ``compute_use`` calls) spikes that
tool's velocity -> it rises in LIVE; when the batch stops, the events
roll out of the window -> it cools. No new mechanism and no schema
change: it reads the existing ``events`` table (``idx_events_ts`` /
``idx_events_tool``) and is fault-tolerant (any DB fault -> ``{}``, so
the caller falls back to the legacy capped-capsule sort; never 500s).
"""

from __future__ import annotations

import time

# Rolling window (seconds) for the LIVE velocity count. Tunable; an
# additive settings key can expose it later if the user wants to dial
# the "fast" horizon. 60s keeps the signal "recent but not a blink."
PULSE_VELOCITY_WINDOW_S = 60


def event_velocity(conn, now_ts: float | None = None,
                   window_s: float = PULSE_VELOCITY_WINDOW_S) -> dict:
    """Per-tool event-rate velocity.

    Counts ``tool_start`` events in the half-open window
    ``[now - window_s, now]`` and returns::

        {tool_name: {"count": n, "per_s": n / window_s, "window_s": w}}

    Tools with no in-window events are simply absent (the caller treats
    a missing tool as count 0). ``conn`` is a sqlite3.Connection on the
    profile DB. Pure read; any fault -> ``{}`` (velocity neutral, the
    caller then uses the legacy sort key). ``now_ts`` is overridable so
    tests can advance the window deterministically.
    """
    out: dict = {}
    try:
        window = max(0.0, float(window_s))
        now = float(now_ts if now_ts is not None else time.time())
        since = now - window
        rows = conn.execute(
            "SELECT tool_name, COUNT(*) AS n FROM events"
            " WHERE type = 'tool_start' AND ts >= ?"
            " AND tool_name IS NOT NULL AND tool_name != ''"
            " GROUP BY tool_name",
            (since,)).fetchall()
    except Exception:
        return out
    for tool, n in rows:
        if tool is None:
            continue
        n = int(n or 0)
        out[str(tool)] = {
            "count": n,
            "per_s": round(n / window, 4) if window else 0.0,
            "window_s": int(window) if window == int(window) else window,
        }
    return out
