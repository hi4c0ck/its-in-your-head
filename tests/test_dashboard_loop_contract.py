"""Loop-contract regression test: dashboard read routes must stay sync def.

The router runs inside the plugin host child's SINGLE asyncio loop
(op_asgi). A blocking ``async def`` handler there serializes all of the
desktop's parallel endpoint polls onto that one loop and produces the
uniform 30s "Timed out connecting to Hermes backend" timeouts. Starlette
offloads ``def`` handlers to its thread pool; that offload is what keeps
the polls independent. This test pins the contract so a re-asynced read
route (or a blocking call reintroduced onto the loop in POST /settings)
fails the suite instead of regressing the hang.
"""
import inspect

from dashboard import plugin_api as api

READ_ROUTES = ("metrics", "heatmap", "recent", "pulse", "portrait",
               "drift", "context", "settings", "engine_status", "reset")


def test_read_routes_are_sync_handlers():
    for name in READ_ROUTES:
        fn = getattr(api, name)
        assert not inspect.iscoroutinefunction(fn), (
            f"route {name!r} is async def: its blocking handler work "
            f"serializes the desktop's parallel polls on the plugin host's "
            f"single asyncio loop")


def test_update_settings_stays_async_but_offloads_blocking_io():
    # POST /settings must stay async (it awaits request.json()), but its
    # blocking store reads/writes run on the thread pool.
    assert inspect.iscoroutinefunction(api.update_settings)
    src = inspect.getsource(api.update_settings)
    assert "asyncio.to_thread" in src, (
        "blocking _user_settings_write/_pane_settings must run via "
        "asyncio.to_thread, not on the host loop")


def test_series_scans_are_bounded():
    assert api.DRIFT_SERIES_CAP > 0
    assert api.PORTRAIT_SCAN_CAP > 0
