"""Cold-start burst regressions for the standalone storage alias loader.

The plugin-host child's op_asgi registers each concurrent plugin_api module
instance in sys.modules BEFORE its own exec, so a post-restart burst of
parallel pane polls can exec this file on several instances at once. The
loader must therefore:

1. never hand out a half-initialized package (the 04:40 AttributeError
   502s: "module ... has no attribute 'database'"), and
2. re-create cleanly when the top package entry is deleted while stale
   submodule entries survive (the import fast path skips the parent-attr
   binding for already-cached submodules).

Both are pinned below against the real loader, in the child's standalone
load context (no plugin root on sys.path, hyphenated module name).
"""
import importlib.util
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALIAS = "hermes_plugin_its_in_your_head_storage"
STANDALONE = "hermes_dashboard_plugin_its-in-your-head"


def _standalone_module():
    """Load dashboard/plugin_api.py the way the plugin-host child does."""
    api_path = ROOT / "dashboard" / "plugin_api.py"
    saved_modules = {k: sys.modules.pop(k) for k in list(sys.modules)
                     if k in ("storage",) or k.startswith("storage.")}
    saved_path = sys.path[:]
    sys.path[:] = [p for p in sys.path if p and Path(p).resolve() != ROOT]
    try:
        spec = importlib.util.spec_from_file_location(STANDALONE, api_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load standalone dashboard api {api_path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod, (saved_modules, saved_path)
    except BaseException:
        sys.path[:] = saved_path
        sys.modules.update(saved_modules)
        raise


def _restore(saved):
    saved_modules, saved_path = saved
    sys.modules.pop(STANDALONE, None)
    sys.modules.pop(ALIAS, None)
    for k in [k for k in list(sys.modules)
              if k == "storage" or k.startswith("storage.")]:
        sys.modules.pop(k, None)
    sys.path[:] = saved_path
    sys.modules.update(saved_modules)


def _complete(module):
    """The 04:40 burst died exactly at these accesses on a half-initialized
    module — they are the contract, checked immediately after each call."""
    assert module.database is not None
    assert module.migration is not None
    assert module.EVENTS_DDL


def _burst(mod, n=16):
    results, errors = [], []

    def probe():
        try:
            m = mod._load_plugin_storage()
            _complete(m)
            results.append(m)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=probe) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def test_concurrent_fresh_first_load_single_instance():
    """The child's fresh-process cold start: no alias entries at all, 16
    parallel loader calls (one per racing op_asgi thread) must converge on
    a single, complete instance."""
    mod, saved = _standalone_module()
    try:
        for k in [k for k in list(sys.modules)
                  if k == ALIAS or k.startswith(ALIAS + ".")]:
            del sys.modules[k]
        results, errors = _burst(mod)
        assert not errors, f"fresh cold-start burst failed: {errors[:3]}"
        assert len({id(r) for r in results}) == 1, \
            "burst converged on multiple storage instances"
        assert results[0] is sys.modules[ALIAS]
        _complete(results[0])
    finally:
        _restore(saved)


def test_recreate_after_top_delete_with_stale_submodules():
    """The stale-submodule trap: top package deleted, submodules still
    cached. Without the loader's stale sweep, the re-created package's
    __init__ hits the import fast path and never binds `.database` onto
    the new object."""
    mod, saved = _standalone_module()
    try:
        assert ALIAS + ".database" in sys.modules
        del sys.modules[ALIAS]
        results, errors = _burst(mod)
        assert not errors, f"re-create burst failed: {errors[:3]}"
        assert len({id(r) for r in results}) == 1
        assert results[0] is sys.modules[ALIAS]
        _complete(results[0])
    finally:
        _restore(saved)


def test_cross_instance_cold_start_burst():
    """op_asgi registers each racing plugin_api instance in sys.modules
    BEFORE its exec, so a post-restart burst can be executing this file on
    several instances at once. A per-instance lock cannot serialize them:
    instance A's creator pre-registers the alias mid-exec while instance B
    adopts that half-initialized module. The cross-instance lock shared
    through sys.modules must serialize every instance."""
    mod_a, saved = _standalone_module()
    mod_b, _ = _standalone_module()
    try:
        for k in [k for k in list(sys.modules)
                  if k == ALIAS or k.startswith(ALIAS + ".")]:
            del sys.modules[k]
        results, errors = [], []

        def probe(instance):
            try:
                m = instance._load_plugin_storage()
                _complete(m)
                results.append(m)
            except Exception as e:
                errors.append(e)

        threads = ([threading.Thread(target=probe, args=(mod_a,))
                    for _ in range(8)]
                   + [threading.Thread(target=probe, args=(mod_b,))
                      for _ in range(8)])
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"cross-instance burst failed: {errors[:3]}"
        assert len({id(r) for r in results}) == 1, \
            "racing instances converged on multiple storage instances"
        assert results[0] is sys.modules[ALIAS]
        _complete(results[0])
    finally:
        _restore(saved)
