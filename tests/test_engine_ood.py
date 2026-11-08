"""L-OOD wiring: the engine exposes ood_drift + reference_freshness on the
/portrait perception key and seeds + reassembles the rolling reference from
the bank on live observed turns."""
import math

from engine import Engine as _Engine


def _norm(v):
    s = math.sqrt(sum(x * x for x in v))
    return [x / s for x in v]


def test_portrait_view_exposes_ood_and_freshness_slots():
    eng = _Engine()
    p = eng.portrait_view()["perception"]
    assert "ood_drift" in p and "reference_freshness" in p
    # No observed turn yet: neutral drift, reference not seeded.
    assert p["ood_drift"] == 0.0
    assert p["reference_freshness"] is None


def test_ensure_reference_seeds_from_anchor_vectors():
    eng = _Engine()
    seed = {"directness": {"positive": [_norm([1, 0, 0])], "negative": []}}
    ref = eng._ensure_reference(backend=None, anchor_vectors=seed)
    assert ref is eng.reference
    assert ref.windows["directness"].seeded is True
    assert ref.windows["directness"].live_count == 0


def test_reassembly_and_ood_reflect_live_usage():
    eng = _Engine()
    seed = {"directness": {"positive": [_norm([1, 0, 0])], "negative": []}}
    ref = eng._ensure_reference(backend=None, anchor_vectors=seed)
    aligned = _norm([0.9, 0.1, 0])
    # A vector aligned with the seeded directness window drifts low.
    assert ref.ood_drift_for(aligned) < 0.05
    ref.reassemble("directness", aligned, ts=1.0)
    # An out-of-territory vector (orthogonal to the seeded dim) drifts high.
    off = _norm([0, 0, 1])
    assert ref.ood_drift_for(off) > 0.5
    # Freshness now shows a live entry in the directness window.
    assert ref.freshness(now=2.0)["directness"]["live_count"] == 1
