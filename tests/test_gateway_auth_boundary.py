"""v0.1.28 P1.4 — API auth boundary.

Status: NOT VERIFIED. The dashboard router (``dashboard/plugin_api.py``)
carries NO auth dependency of its own; isolation comes from the Hermes
gateway's namespace-scoped mount. This repo's TestClient suite reaches every
route unauthenticated (correct in-host, proves nothing about the gateway),
so the boundary is ASSERTED, not proven.

Two layers of tests:

1. Provable in-repo (always run): the router has no auth dependency, the
   destructive routes exist, and a bare TestClient reaches them — i.e. we
   confirm that ANY caller the gateway lets through to this namespace is
   trusted, which is exactly why the gateway middleware is the boundary.

2. Real-gateway integration (gated, run only when the Hermes gateway is
   available): an anonymous request, a cross-namespace request, a bad-origin
   POST, and a destructive /reset are all REJECTED by the gateway. This is
   the proof the plan asks for; until it is green the boundary stays
   "not verified" and is tracked by the module docstring + this gate.
"""
import importlib

import pytest


def _client(tmp_path):
    import dashboard.plugin_api as api
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    importlib.reload(api)
    api.set_db_path(tmp_path / "data.db")
    import sqlite3
    with sqlite3.connect(str(tmp_path / "data.db")) as db:
        db.executescript(api._DDL)
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app), api


# ---------------------------------------------------------------------------
# Layer 1 — what the in-repo suite CAN prove (always runs, stays green).
# ---------------------------------------------------------------------------

def test_router_has_no_auth_dependency(tmp_path):
    """The boundary is host-provided: the router itself has no auth
    dependency. A bare TestClient therefore reaches every route — confirming
    that isolation is the gateway's job, not the router's."""
    client, api = _client(tmp_path)
    assert not getattr(api.router, "dependencies", None), \
        "router must not add its own auth dependency — isolation is the " \
        "gateway's namespace mount; an in-router dependency would paper over " \
        "the host boundary"
    # A bare TestClient reaches a read route unauthenticated (the "asserted,
    # not proven" state the boundary depends on).
    r = client.get("/metrics")
    assert r.status_code == 200, \
        "in-repo TestClient reaches the routes unauthenticated, as expected — " \
        "the gateway is the actual auth boundary (see layer 2)"


def test_destructive_routes_exist_unauthenticated(tmp_path):
    """POST /reset + POST /settings are present and reachable from a bare
    TestClient. Their safety rests ENTIRELY on the gateway's namespace
    isolation — the destructive-reset check in layer 2 is what proves it."""
    client, api = _client(tmp_path)
    for path in ("/reset", "/settings"):
        r = client.post(path)
        # Reachable (not 404). The gateway is what must reject a hostile
        # caller, not a 403 from this router.
        assert r.status_code != 404, f"{path} must exist"


@pytest.mark.skip(
    reason="P1.4 real-gateway integration gate: requires the Hermes gateway; "
           "the in-repo TestClient cannot prove the host namespace boundary. "
           "Run via the gateway harness (see test_gateway_integration_probe).")
def test_gateway_rejects_anonymous_and_cross_namespace(tmp_path):
    """The proof the plan wants: the gateway REJECTS an anonymous request, a
    cross-namespace request, a bad-origin POST, and a hostile /reset. This
    only runs against a live Hermes gateway, so it is gated — until it is
    green the auth boundary stays 'not verified'."""
    raise AssertionError("real-gateway probe not wired — see module docstring")


# ---------------------------------------------------------------------------
# Layer 2 gate — run the real-gateway probe only when the gateway is present.
# ---------------------------------------------------------------------------

def _gateway_available() -> bool:
    """Detect the Hermes gateway (the thing that actually enforces the
    namespace auth boundary). Absent in this repo's standalone TestClient
    env; present in the desktop/gateway process where a real integration
    harness can mount the router and probe it."""
    import importlib.util
    return (importlib.util.find_spec("hermes_gateway") is not None
            or __import__("os").environ.get("IYH_GATEWAY_INTEGRATION") == "1")


@pytest.mark.skipif(not _gateway_available(),
                    reason="Hermes gateway not importable — P1.4 gateway "
                           "boundary probe needs a live gateway")
def test_gateway_integration_probe(tmp_path):
    """Wired against a real Hermes gateway: mount ``api.router`` under
    /api/plugins/its-in-your-head/ in a namespace-scoped app, then assert an
    anonymous request is rejected, a cross-namespace traversal is rejected,
    a bad-origin POST is rejected (CSRF), and an authorized destructive
    /reset is allowed while a hostile one is not. Until this harness exists
    the module stays skipped and the boundary is documented 'not verified.'"""
    pytest.fail("gateway integration harness not implemented yet "
                "(P1.4 follow-up — build the harness, keep the docstring "
                "'not verified' until this is green)")
