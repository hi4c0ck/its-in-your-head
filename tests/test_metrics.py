"""OWNER tests: pipeline as slots land.

Green at hand-off: assert that the owner surface exists and raises
NotImplementedError, so the baseline proves the structure without
asserting on un-written behavior.
"""
import importlib
import math
import struct


def test_enqueue_accepts_events():
    import metrics
    importlib.reload(metrics)
    before = metrics._queue.qsize()
    metrics.enqueue({"type": "turn.start", "ts": 0.0})
    assert metrics._queue.qsize() == before + 1
    metrics._queue.get_nowait()


def test_sketch_shape_and_packing():
    import embedder
    importlib.reload(embedder)
    blob = embedder.sketch("hello world")
    assert len(blob) == embedder.DIM * 4, "packed DIM float32s expected"
    vals = struct.unpack(f"<{embedder.DIM}f", blob)
    assert all(math.isfinite(v) for v in vals)


def test_sketch_deterministic_and_casefolded():
    import embedder
    importlib.reload(embedder)
    assert embedder.sketch("same prompt") == embedder.sketch("same prompt")
    assert embedder.sketch("ABC") == embedder.sketch("abc")
    assert embedder.sketch("a bug in the parser") != embedder.sketch("a feature in the parser")


def test_sketch_l2_normalized():
    import embedder
    importlib.reload(embedder)
    import struct as _s
    vals = _s.unpack(f"<{embedder.DIM}f", embedder.sketch("normalize me"))
    norm = math.sqrt(sum(v * v for v in vals))
    assert abs(norm - 1.0) < 1e-4


def test_sketch_empty_is_zero_vector():
    import embedder
    importlib.reload(embedder)
    import struct as _s
    for text in ("", None):
        vals = _s.unpack(f"<{embedder.DIM}f", embedder.sketch(text))
        assert all(v == 0.0 for v in vals)


def test_worker_is_an_owner_slot():
    import metrics
    importlib.reload(metrics)
    try:
        metrics.start_worker()
        raise AssertionError("start_worker should be NotImplemented until owner codes it")
    except NotImplementedError:
        pass
