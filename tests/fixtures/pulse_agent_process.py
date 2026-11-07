"""Separate-process fixture: the AGENT half (PULSE bridge test).

Feeds the engine a synthetic tool/user sequence and lets it persist the
bounded PULSE state to the profile DB at argv[1] — the way the metrics
worker does (off the hot path). The dashboard half
(pulse_dashboard_process.py) then reads the same DB in its own fresh
process, with no in-process engine.
"""
import sys
import time


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: pulse_agent_process.py <db-path>")
    from storage import database
    database.set_override(sys.argv[1])

    import engine

    eng = engine.get_engine()
    now = time.time()
    eng.on_events([
        {"type": "tool_start", "tool_name": "terminal", "ts": now},
        {"type": "tool_start", "tool_name": "read_file", "ts": now},
    ])
    eng.on_events([
        # The error end is objective friction (never user friction).
        {"type": "tool_end", "tool_name": "terminal", "status": "error",
         "duration_ms": 40, "ts": now + 0.1},
        {"type": "tool_end", "tool_name": "read_file", "status": "ok",
         "duration_ms": 10, "ts": now + 0.2},
    ])
    eng.on_events([
        {"type": "user_turn", "ts": now + 0.3,
         "user_message": "please fix the flaky pulse bridge test"},
    ])
    # Hot-path feedback (the collector's API): labels the most recent
    # in-memory capsule and marks the bank dirty — no file I/O here; the
    # next worker pass persists it.
    from pulse import feedback
    feedback.apply_feedback(eng.protos, eng.capsule_state(), "great job")
    eng.on_events([])  # worker pass: dirty banks + goal vector persist
    print("agent done")


if __name__ == "__main__":
    main()
