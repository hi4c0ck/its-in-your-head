"""OWNER test placeholders — replace as the pipeline gets implemented.

Green at hand-off: assert that the owner surface exists and raises
NotImplementedError, so the baseline proves the structure without
asserting on un-written behavior.
"""
import importlib


def test_enqueue_accepts_events():
    import metrics
    importlib.reload(metrics)
    before = metrics._queue.qsize()
    metrics.enqueue({"type": "turn.start", "ts": 0.0})
    assert metrics._queue.qsize() == before + 1
    metrics._queue.get_nowait()


def test_sketch_is_an_owner_slot():
    import embedder
    importlib.reload(embedder)
    try:
        embedder.sketch("hi")
        raise AssertionError("sketch should be NotImplemented until owner codes it")
    except NotImplementedError:
        pass


def test_worker_is_an_owner_slot():
    import metrics
    importlib.reload(metrics)
    try:
        metrics.start_worker()
        raise AssertionError("start_worker should be NotImplemented until owner codes it")
    except NotImplementedError:
        pass
