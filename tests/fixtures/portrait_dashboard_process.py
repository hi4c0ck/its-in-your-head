"""Separate-process fixture: the ISOLATED DASHBOARD half (PORTRAIT
bridge test).

Simulates the plugin-host child: a fresh process with no in-process
agent engine (``sys.modules["engine"] = None`` makes the /portrait
live-view probe fail the way the child's import does), reading the
profile DB at argv[1] that the agent half wrote. Prints the /portrait
JSON body on its last stdout line.
"""
import json
import sys


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: portrait_dashboard_process.py <db-path>")
    # type: ignore[assignment]
    sys.modules["engine"] = None  # the child: no top-level engine module
    # The production child has the plugin root OFF its sys.path, so the
    # top-level agent packages are unimportable and _engine_first's probe
    # fails the same way here.
    # type: ignore[assignment]
    sys.modules["perception"] = None
    # type: ignore[assignment]
    sys.modules["pulse"] = None

    from storage import database
    database.set_override(sys.argv[1])

    import dashboard.plugin_api as api
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(api.router)
    body = TestClient(app).get("/portrait").json()
    print(json.dumps(body))


if __name__ == "__main__":
    main()
