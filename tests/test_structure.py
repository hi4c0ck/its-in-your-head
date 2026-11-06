"""Infra baseline: the package stays structurally valid while the owner codes.

Run with the hermes install venv:  <venv python> -m pytest -q
(pytest.ini pins the importlib import mode this package shape needs.)
"""
import importlib
import importlib.util
import inspect
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_plugin():
    p = ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location("its_in_your_head_plugin", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _declared_hooks():
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    m = re.search(r"provides_hooks:\s*\n((?:\s+-\s.*\n)+)", text)
    assert m, "plugin.yaml: provides_hooks block not found"
    return [line.strip().lstrip("-").strip().strip("'\"")
            for line in m.group(1).splitlines() if line.strip()]


def test_registration_matches_manifest():
    """provides_hooks must exactly match what register() registers
    (catalog admission + doctor drift check enforce this)."""
    mod = _load_plugin()
    registered = []

    class FakeCtx:
        def register_hook(self, name, cb):
            registered.append(name)

        def register_tool(self, **kw):
            raise AssertionError("tools are not part of the skeleton")

    mod.register(FakeCtx())
    assert set(registered) == set(_declared_hooks())


def test_hook_callbacks_accept_kwargs():
    mod = _load_plugin()
    for name in ("_on_pre_llm_call", "_on_post_api_request", "_on_api_request_error"):
        sig = inspect.signature(getattr(mod, name))
        assert any(p.kind is inspect.Parameter.VAR_KEYWORD
                   for p in sig.parameters.values()), f"{name} must accept **kwargs"


def test_backend_route_shapes():
    """Stub routes return the documented response shapes — the green baseline
    for the desktop half before the owner implements the real queries."""
    import dashboard.plugin_api as api
    importlib.reload(api)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    r = client.get("/metrics")
    assert r.status_code == 200
    assert set(r.json()) == {"events", "total_tokens", "last_event"}
    assert client.get("/heatmap?window=24h").json()["hours"] == 24
    assert client.get("/recent").json() == []
