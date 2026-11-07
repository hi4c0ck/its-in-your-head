"""The metrics worker must retain the profile context that loaded the plugin.

Hermes resolves plugin_data_dir()/plugin_db() from the active profile's
ContextVar-backed HERMES_HOME scope. A bare threading.Thread drops that scope
and can silently send this profile's telemetry to the gateway's default home.
"""
import contextvars
import importlib
import threading


def test_start_worker_preserves_registering_profile_context(monkeypatch):
    import metrics

    # A fresh module gives the test an isolated worker/queue lifecycle.
    metrics = importlib.reload(metrics)
    profile_scope = contextvars.ContextVar("test_profile_scope", default="default")
    observed = []
    completed = threading.Event()

    def worker_probe():
        observed.append(profile_scope.get())
        completed.set()

    monkeypatch.setattr(metrics, "_worker_main", worker_probe)
    token = profile_scope.set("profiles/plugin-studio")
    try:
        metrics.start_worker()
    finally:
        profile_scope.reset(token)

    try:
        assert completed.wait(10), "profile-scoped worker did not start"
        assert observed == ["profiles/plugin-studio"]
    finally:
        metrics.stop_worker(timeout=10)
