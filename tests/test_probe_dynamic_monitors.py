"""L-QA: the dynamic-territory monitor probe (deterministic, Tier-0 sketch
backend). Drift-recall flags known out-of-territory probes, territory
stability stays bounded, and freshness reports live reassemblies."""
from perception.encoder import Tier0SketchBackend
from tools.probe_dynamic_monitors import (
    INJECTED_NOW, build_seeded_reference, drift_recall, territory_stability)

DIMS = {"directness", "context_richness", "exploration", "analytical_depth",
        "iteration", "scope"}


def test_drift_recall_flags_ood_and_in_domain():
    backend = Tier0SketchBackend()
    ref = build_seeded_reference(backend)
    r = drift_recall(backend, ref)
    # The held-out OOD set is fully present and most of it is flagged
    # out-of-territory at the 0.5 threshold.
    assert r["ood_total"] == 8
    assert r["ood_rate"] >= 0.5
    assert r["in_total"] > 0
    # In-domain probes mostly stay in territory (fewer false flags than
    # the OOD rate).
    assert r["in_false_rate"] < r["ood_rate"]


def test_territory_stability_is_bounded():
    backend = Tier0SketchBackend()
    ref = build_seeded_reference(backend)
    st = territory_stability(backend, ref)
    assert set(st["per_dim"]) == DIMS
    # Reassembling over in-domain probes moves each centroid a bounded amount
    # (not a runaway).
    assert 0.0 <= st["max_shift"] < 1.5
    assert st["max_shift"] == max(st["per_dim"].values())


def test_freshness_reports_live_reassemblies():
    backend = Tier0SketchBackend()
    ref = build_seeded_reference(backend)
    territory_stability(backend, ref)  # reassembles every dim (live > 0)
    fresh = ref.freshness(now=INJECTED_NOW)
    assert set(fresh) == DIMS
    for dim, f in fresh.items():
        assert f["live_count"] > 0
        assert f["seeded"] is False
        # The injected clock is far in the future -> positive (stale) ages.
        assert f["age"] is not None and f["age"] > 0
