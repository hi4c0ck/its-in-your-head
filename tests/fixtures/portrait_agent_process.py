"""Separate-process fixture: the AGENT half (PORTRAIT bridge test).

Feeds the engine synthetic user turns; each turn persists an observed
portrait observation to portrait_history (the F2 durable path,
synchronous in engine._append_history). The dashboard half
(portrait_dashboard_process.py) then reads the same DB in its own
fresh process, with no in-process engine.
"""
import sys
import time


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: portrait_agent_process.py <db-path>")
    from storage import database
    database.set_override(sys.argv[1])

    import engine

    eng = engine.get_engine()
    now = time.time()
    eng.on_events([
        {"type": "turn", "ts": now,
         "user_message": "please tighten the portrait fallback tests"},
        {"type": "turn", "ts": now + 0.5,
         "user_message": "now make the drift sparklines honest"},
    ])
    print("agent done")


if __name__ == "__main__":
    main()
