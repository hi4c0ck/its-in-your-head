"""Stand-in ONNX worker: the same long-lived process shape as the real
worker so the stale-registry reaper's image-path check + the detached
watchdog have a real target. No inference — the model is not required for
the lifecycle under test. Run with the onnx venv python when available so
its image carries the `its-in-your-head-onnx` marker the reaper matches on."""
import time

time.sleep(3000)
