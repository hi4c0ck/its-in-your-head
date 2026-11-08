"""Pin the isolated-child settings fallback to the full C4 key set.

The dashboard child runs with the plugin root off its ``sys.path`` so it cannot
import ``core.settings``; ``_settings_defaults()`` then falls back to an inline
literal. That literal used to drift from ``core.settings.DEFAULTS`` — it was
missing ``perception.encoder`` (and the other later-added keys), which made the
encoder switcher's ``POST /settings`` 400 with "unknown settings keys" in the
child. This test forces the fallback path (both import forms blocked) and
asserts it mirrors ``DEFAULTS`` byte-for-byte, so the next key added to
DEFAULTS can't silently re-break the switcher in the isolated child.
"""
import sys

import pytest

PLUGIN_ROOT = "D:/work/its-in-your-head"
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)


def _block_core_settings(monkeypatch):
    """Force BOTH `from core.settings import ...` and the relative form to
    raise, exactly like the isolated child, so _settings_defaults() takes the
    last-resort branch."""
    for name in [m for m in list(sys.modules)
                 if m == "core" or m.startswith("core.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "core", None)
    monkeypatch.setitem(sys.modules, "core.settings", None)


def test_child_settings_fallback_mirrors_defaults(monkeypatch):
    from core.settings import DEFAULTS
    # Import the route module fresh so its try-block re-runs under the block.
    for m in [k for k in sys.modules if k.startswith("dashboard")]:
        monkeypatch.delitem(sys.modules, m, raising=False)
    _block_core_settings(monkeypatch)
    import dashboard.plugin_api as pa

    lastresort = pa._settings_defaults()
    # The exact bug this prevents: a new C4 key missing from the inline literal.
    assert "perception.encoder" in lastresort
    assert set(lastresort) == set(DEFAULTS), (
        f"child fallback drifted from DEFAULTS: "
        f"missing={sorted(set(DEFAULTS) - set(lastresort))} "
        f"extra={sorted(set(lastresort) - set(DEFAULTS))}")
    assert all(lastresort[k] == DEFAULTS[k] for k in DEFAULTS)


def test_child_settings_fallback_is_stable():
    """Reproducing the block twice yields the identical literal (no
    hidden state leak between the fallback invocations)."""
    from core.settings import DEFAULTS
    for m in [k for k in sys.modules if k.startswith("dashboard")]:
        del sys.modules[m]
    saved = {k: sys.modules.get(k) for k in ("core", "core.settings")}
    try:
        sys.modules["core"] = None
        sys.modules["core.settings"] = None
        import dashboard.plugin_api as pa

        a = pa._settings_defaults()
        b = pa._settings_defaults()
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    assert a == b
    assert set(a) == set(DEFAULTS)
