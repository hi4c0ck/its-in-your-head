"""Test-harness hooks (manager territory — test infra, not plugin code).

Session-end temp summary: after each run, report how much pytest temp the
suite wrote. The HARD gate is tools/temp_budget_check.py (run by CI and
available locally); this just keeps the number visible in dev runs so a
runaway fixture is noticed in the same terminal session that caused it.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest


def pytest_sessionfinish(session, exitstatus):
    factory = session.config.pluginmanager.get_plugin("tmpdir_factory")
    base = factory.getbasetemp() if factory is not None else None
    if base is None:
        return
    base = pathlib.Path(base)
    if not base.exists():
        return
    total = 0
    big = 0
    for f in base.rglob("*"):
        if f.is_file():
            try:
                sz = f.stat().st_size
            except OSError:
                continue
            total += sz
            if sz > 50 * 1024 * 1024:
                big += 1
    mb = total / (1024 * 1024)
    warn = mb >= 100 or big
    print(f"\n[pytest-temp] suite temp = {mb:.1f} MB "
          f"({big} file(s) > 50 MB)")
    if warn:
        print("[pytest-temp] OVER BUDGET — a test fixture is writing "
              "model-scale bytes into tmp_path. Shrink fakes to "
              "byte-sized stand-ins, then: "
              f"python tools/temp_budget_check.py {base}")


@pytest.fixture(scope="session")
def _iyh_data_root(tmp_path_factory):
    """The perception data root the test process pins.

    CI runs without the Hermes storage resolver (no ``plugins`` /
    ``hermes_constants`` modules), so the strict ``resolve_data_dir()``
    default path raises ``RuntimeError`` there and takes the whole
    portrait / drift / persistence / data-root test group down. This fixture
    gives the test process a real, writable data root instead. Production
    keeps the strict resolver; only the test process is pinned (and an
    ``IYH_TEST_DATA_DIR`` env var lets separate-process fixtures reuse the
    same root)."""
    override = os.environ.get("IYH_TEST_DATA_DIR")
    root = pathlib.Path(override).expanduser() if override else \
        tmp_path_factory.mktemp("iyh-data-root")
    root.mkdir(parents=True, exist_ok=True)
    os.environ["IYH_TEST_DATA_DIR"] = str(root)
    return root


@pytest.fixture(autouse=True)
def _iyh_pin_data_root(_iyh_data_root, monkeypatch):
    """Pin ``resolve_data_dir(None)`` to the session data root — but ONLY when
    the real Hermes resolver is unavailable (CI, no ``plugins`` /
    ``hermes_constants`` modules). Under a live Hermes install the real
    resolver works and tests must keep using it (a blanket pin would reroute
    engine caches to a temp dir). Explicit data dirs always win, so a pin that
    does apply still only substitutes the DEFAULT path."""
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        import perception.anchors as _anchors
    except Exception:
        return  # perception not importable in this context — nothing to pin
    real = _anchors.resolve_data_dir
    # Probe the real resolver (no I/O — it only computes a Path). If it
    # succeeds, the Hermes storage resolver is available: do NOT pin (tests
    # keep using the real data dir). Only when it is unavailable (CI) pin
    # the default path to the session data root.
    try:
        real(None)
    except Exception:
        def _patched(explicit=None):
            return real(explicit) if explicit else _iyh_data_root
        monkeypatch.setattr(_anchors, "resolve_data_dir", _patched)
        # patterns.py binds the name locally (``from .anchors import
        # resolve_data_dir``) — patch that binding too, or its default path
        # would still raise.
        try:
            import perception.patterns as _patterns
            monkeypatch.setattr(_patterns, "resolve_data_dir", _patched)
        except Exception:
            pass
    yield _iyh_data_root
