"""Test-harness hooks (manager territory — test infra, not plugin code).

Session-end temp summary: after each run, report how much pytest temp the
suite wrote. The HARD gate is tools/temp_budget_check.py (run by CI and
available locally); this just keeps the number visible in dev runs so a
runaway fixture is noticed in the same terminal session that caused it.
"""
from __future__ import annotations

import pathlib


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
