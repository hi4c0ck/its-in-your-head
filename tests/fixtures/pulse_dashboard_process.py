"""Separate-process fixture: the ISOLATED DASHBOARD half (PULSE bridge
test).

Simulates the plugin-host child: a fresh process with no in-process agent
engine (``sys.modules["engine"] = None`` makes the /pulse live-view probe
fail the way the child's import does), reading the profile DB at argv[1]
that the agent half wrote. Prints the /pulse JSON body on its last
stdout line.
"""
import json
import sys


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: pulse_dashboard_process.py <db-path>")
    # type: ignore[assignment]
    sys.modules["engine"] = None  # the child: no top-level engine module

    from storage import database
    database.set_override(sys.argv[1])

    # P1.5: honor the test-harness data root (conftest sets IYH_TEST_DATA_DIR;
    # a live Hermes install leaves it unset, so this is a no-op there).
    # perception is importable in this child-sim process (only engine is
    # nulled), so the /pulse fallback's data-root resolution stays CI-safe.
    import os
    import perception.anchors as _anchors
    _env_root = os.environ.get("IYH_TEST_DATA_DIR")
    if _env_root:
        _anchors.set_data_dir_override(_env_root)

    import dashboard.plugin_api as api
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    body = client.get("/pulse").json()
    print(json.dumps(body))


if __name__ == "__main__":
    main()
