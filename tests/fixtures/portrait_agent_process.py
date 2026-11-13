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

    # P1.5: honor the test-harness data root (conftest sets IYH_TEST_DATA_DIR;
    # a live Hermes install leaves it unset, so this is a no-op there). The
    # engine resolves the perception data root lazily; pin it so the suite's
    # separate-process half is deterministic + CI-safe.
    import os
    import perception.anchors as _anchors
    _env_root = os.environ.get("IYH_TEST_DATA_DIR")
    if _env_root:
        _anchors.set_data_dir_override(_env_root)

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
