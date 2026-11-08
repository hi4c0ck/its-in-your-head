#!/usr/bin/env python3
"""L-QA dynamic-territory monitors (replaces the retired OOD-Δ floor).

Deterministic dev-time probe, no LLM, no randomness. Builds a
RollingReference seeded from the curated bank (R4), then runs three dynamic
monitors that read "how fresh / how wide is my territory," not a frozen Δ:

- **Drift recall** — does ``ood_drift_for`` flag a held-out
  known-out-of-territory probe set, and leave in-domain probes in territory?
  (replaces the OOD-Δ separation floor; a monitor, not a hard gate).
- **Territory stability** — how much each dim's reference centroid moves
  across a bounded reassembly window (catches pathological reassembly).
- **Reference freshness** — ``freshest_ts`` age per dim at an injected
  deterministic ``now`` (stale territory is a signal, not an error).

Run from the repo root:  python tools/probe_dynamic_monitors.py
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from perception import patterns as pat  # noqa: E402
from perception.anchors import ANCHORS, DIMENSIONS  # noqa: E402
from perception.encoder import Tier0SketchBackend, TIER0_MODEL_VERSION  # noqa: E402
from perception.reference import RollingReference  # noqa: E402
from tools.probe_pattern_quality import OUT_OF_DOMAIN, FRESH_IN_DOMAIN  # noqa: E402

# A deterministic injected clock for the freshness monitor (stale territory
# is a signal, not an error — so a fixed now keeps the probe re-runnable).
INJECTED_NOW = 1_000_000.0
# OOD flag threshold for the drift-recall monitor (drift >= 0.5 = "out").
OOD_FLAG = 0.5


def _l2(v) -> float:
    return sum(x * x for x in v) ** 0.5


def _centroid(vectors) -> list[float]:
    if not vectors:
        return []
    n = len(vectors)
    return [sum(row[i] for row in vectors) / n
            for i in range(len(vectors[0]))]


def _shift(before, after) -> float:
    """L2 distance between two centroid vectors (0 = no movement)."""
    if not before or not after or len(before) != len(after):
        return 0.0
    return sum((a - b) ** 2 for a, b in zip(before, after)) ** 0.5


def build_seeded_reference(backend) -> RollingReference:
    """R4 cold start: seed each dim's rolling window from the bank's positive
    VECTORS (encoded via ``get_anchor_vectors`` — ANCHORS holds the raw
    phrases, not vectors)."""
    from perception.anchors import get_anchor_vectors
    av = get_anchor_vectors(backend)
    seed = {d: list(av.get(d, {}).get("positive") or [])
            for d in DIMENSIONS}
    return RollingReference(seed_bank=seed)


def drift_recall(backend, ref) -> dict:
    """Does ood_drift_for flag known out-of-territory probes high while
    leaving in-domain probes in territory? (replaces the OOD-Δ floor)."""
    enc = backend.encode
    ood_vecs = enc(list(OUT_OF_DOMAIN))
    in_vecs = enc([t for d in DIMENSIONS for t in FRESH_IN_DOMAIN[d]])
    ood_flagged = sum(1 for v in ood_vecs
                       if ref.ood_drift_for(v) >= OOD_FLAG)
    in_flagged = sum(1 for v in in_vecs
                     if ref.ood_drift_for(v) >= OOD_FLAG)
    ood_rate = ood_flagged / len(ood_vecs) if ood_vecs else 0.0
    in_false = in_flagged / len(in_vecs) if in_vecs else 0.0
    return {"ood_flagged": ood_flagged, "ood_total": len(ood_vecs),
            "ood_rate": ood_rate, "in_false": in_flagged,
            "in_total": len(in_vecs), "in_false_rate": in_false}


def territory_stability(backend, ref) -> dict:
    """Per-dim reference-centroid movement across a bounded reassembly window.
    Small shift = stable; a large shift = pathological reassembly / runaway."""
    enc = backend.encode
    shifts: dict[str, float] = {}
    for dim in DIMENSIONS:
        before = _centroid(_centroids_of(ref, dim))
        # Reassemble the dim over its in-domain probe sequence (deterministic).
        seq = list(ANCHORS[dim]["positive"]) + list(FRESH_IN_DOMAIN[dim])
        for ts, vec in zip(range(1, len(seq) + 1), enc(seq)):
            ref.reassemble(dim, vec, ts=float(ts))
        after = _centroid(_centroids_of(ref, dim))
        shifts[dim] = round(_shift(before, after), 6)
    return {"per_dim": shifts,
            "max_shift": max(shifts.values(), default=0.0)}


def _centroids_of(ref, dim):
    return ref.windows[dim].vectors()


def freshness_report(ref) -> dict:
    """Per-dim freshest_ts age at the injected now (a signal, not an error)."""
    return ref.freshness(now=INJECTED_NOW)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(REPO_ROOT / "probe-report-dynamic.md"))
    args = ap.parse_args(argv)

    backend = Tier0SketchBackend()
    assert backend.model_version == TIER0_MODEL_VERSION
    ref = build_seeded_reference(backend)

    recall = drift_recall(backend, ref)
    # stability reassembles the reference in place; capture before/after.
    stability = territory_stability(backend, ref)
    fresh = freshness_report(ref)

    lines: list[str] = []
    lines.append("# L-QA dynamic-territory monitor report")
    lines.append("")
    lines.append(f"- backend: tier-0 ({backend.model_version})")
    lines.append(f"- reference: seeded from bank ({', '.join(DIMENSIONS)})")
    lines.append("- monitors: drift-recall, territory-stability, "
                 "reference-freshness (all informational — no hard gate)")
    lines.append("")
    lines.append("## Drift recall (replaces the retired OOD-Δ floor)")
    lines.append("")
    lines.append(f"- out-of-territory flagged: {recall['ood_flagged']}/"
                 f"{recall['ood_total']} ({recall['ood_rate']:.3f}) "
                 f"at OOD threshold {OOD_FLAG}")
    lines.append(f"- in-domain false flags: {recall['in_false']}/"
                 f"{recall['in_total']} ({recall['in_false_rate']:.3f})")
    lines.append("")
    lines.append("## Territory stability (per-dim centroid movement)")
    lines.append("")
    lines.append("| dim | centroid shift |")
    lines.append("|---|---|")
    for dim in DIMENSIONS:
        lines.append(f"| {dim} | {stability['per_dim'][dim]:.6f} |")
    stable = stability["max_shift"] < 0.25
    stable_label = "stable" if stable else "watch: possible runaway reassembly"
    lines.append(f"\n- max shift: {stability['max_shift']:.6f} ({stable_label})")
    lines.append("")
    lines.append("## Reference freshness (injected now, age per dim)")
    lines.append("")
    lines.append("| dim | seeded | live_count | age |")
    lines.append("|---|---|---|---|")
    for dim in DIMENSIONS:
        f = fresh[dim]
        age = f["age"]
        age_s = "-" if age is None else f"{age:.1f}"
        lines.append(f"| {dim} | {f['seeded']} | {f['live_count']} | {age_s} |")
    lines.append("")

    body = "\n".join(lines)
    out = Path(args.out)
    out.write_text(body, encoding="utf-8")
    print(body)
    print(f"\n[monitor] report: {out}")
    print(f"[monitor] sha256(report body): "
          f"{hashlib.sha256(body.encode('utf-8')).hexdigest()}")


if __name__ == "__main__":
    main()
