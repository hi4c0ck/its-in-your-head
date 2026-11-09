"""Separate-process fixture: PULSE restart restore (Stage 1 lifecycle).

A fresh process with a fresh engine against the profile DB at argv[1]
(the DB was written by pulse_agent_process.py). Forces hydration via
pulse_view and prints the /pulse-shaped JSON body on the last stdout
line. No new tool calls or feedback are fed — any rows/evaluations
come purely from restored persisted evidence.
"""
import json
import sys


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: pulse_restart_process.py <db-path>")
    from storage import database
    database.set_override(sys.argv[1])

    import engine

    eng = engine.get_engine()
    view = eng.pulse_view()
    print(json.dumps(view))


if __name__ == "__main__":
    main()
