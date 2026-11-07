# Tune confidence constants from the live snapshot (deterministic, stdlib-only).
"""Deterministic analysis of pattern_affinity_history vs confidence math.

Reads a profile DB snapshot READ-ONLY (sqlite3 ``mode=ro`` URI; never
writes), prints:
  - top-pattern dominance counts
  - top_margin / top_amplification distribution stats
  - confidence vs (evidence, age-at-ref) crosstab
  - amplification-sign vs implied-agreement check (sign-aware decay test)
  - margin-gate fit (K from the observed median margin) + alpha fit
  - replay: legacy stored confidence vs NEW calibrated formula evaluated
    at the last row ts (age = ref_ts - row_ts), with mean/std spread.

Implied agreement reconstruction: stored confidence was computed by the
engine at insertion time with age~0 (all last-evidence stamps set to the
same turn ts), so agreement = stored_conf / (alpha_legacy * log(1+ev)).
This recovers the engine's cumulative top-fraction per row.

Usage: python tools/tune_confidence.py [--db PATH]
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from perception.patterns import (  # noqa: E402
    CONFIDENCE_ALPHA,
    CONFIDENCE_LAMBDA,
    confidence_from_evidence,
    margin_gate,
)

DEFAULT_DB = ROOT / "data" / "live-snapshot.db"


def _stats(vals):
    vals = sorted(vals)
    return {
        "n": len(vals),
        "min": vals[0] if vals else None,
        "p25": vals[len(vals) // 4] if vals else None,
        "median": statistics.median(vals) if vals else None,
        "max": vals[-1] if vals else None,
        "mean": statistics.fmean(vals) if vals else None,
        "std": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
    }


def _corr(xs, ys):
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return num / den if den else 0.0


def fmt(stats, digits=4):
    return (
        f"n={stats['n']} min={stats['min']:.{digits}f} "
        f"median={stats['median']:.{digits}f} max={stats['max']:.{digits}f} "
        f"mean={stats['mean']:.{digits}f} std={stats['std']:.{digits}f}"
    )


def main(db_path: Path) -> int:
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        rows = list(con.execute(
            "SELECT id, ts, top_pattern, top_prompt_affinity, "
            "top_agent_affinity, top_margin, top_amplification, evidence, "
            "confidence FROM pattern_affinity_history ORDER BY ts, id"
        ))
        bank = list(con.execute(
            "SELECT pattern_name, evidence FROM pattern_bank ORDER BY pattern_name"
        ))
    finally:
        con.close()
    if not rows:
        print("no pattern_affinity_history rows")
        return 1

    ref_ts = max(r[1] for r in rows)
    print(f"db: {db_path} (read-only)")
    print(f"rows: {len(rows)} span_hours={(ref_ts - min(r[1] for r in rows)) / 3600:.2f} "
          f"ref_ts={ref_ts:.1f}")
    print(f"pattern_bank: {bank}")

    # --- top-pattern dominance -------------------------------------------
    counts = Counter(r[2] for r in rows)
    print("\ntop_pattern counts:")
    for name, c in counts.most_common():
        print(f"  {name}: {c}/{len(rows)} ({c / len(rows):.1%})")

    # --- margin / amplification distributions ----------------------------
    margins = [r[5] for r in rows if r[5] is not None]
    amps = [r[6] for r in rows if r[6] is not None]
    confs = [r[8] for r in rows if r[8] is not None]
    print("\ntop_margin:      " + fmt(_stats(margins)))
    print("top_amplification: " + fmt(_stats(amps)) +
          f" positive={sum(a > 0 for a in amps)}/{len(amps)}")
    print("stored_confidence: " + fmt(_stats(confs)))
    print(f"  distinct stored confidences: {sorted(set(round(c, 6) for c in confs))}")

    # --- implied agreement (age~0 at insertion) --------------------------
    implied = []
    for r in rows:
        base = CONFIDENCE_ALPHA * math.log(1.0 + r[7]) if r[7] else 0.0
        implied.append((r[8] / base) if base and r[8] is not None else 0.0)

    # --- confidence vs (evidence, age) crosstab --------------------------
    print("\nconfidence vs (evidence, age_hours_at_ref):")
    print("  ev  age_h  conf    margin  amp     implied_agr  top")
    for r, agr in zip(rows, implied):
        print(f"  {r[7]:<3d} {(ref_ts - r[1]) / 3600:<6.2f} {r[8]:.4f}  "
              f"{r[5]:.4f}  {r[6]:+.3f}  {agr:.3f}        {r[2]}")
    by_ev: dict[int, list[float]] = {}
    for r in rows:
        by_ev.setdefault(r[7], []).append(r[8])
    print("  mean stored conf by evidence: " +
          ", ".join(f"ev={k}:{statistics.fmean(v):.4f}" for k, v in sorted(by_ev.items())))

    # --- amplification sign check ----------------------------------------
    neg_agr = [a for r, a in zip(rows, implied) if r[6] is not None and r[6] < 0]
    pos_agr = [a for r, a in zip(rows, implied) if r[6] is not None and r[6] > 0]
    print("\namplification sign vs implied agreement:")
    print(f"  amp<0: n={len(neg_agr)} mean_agr={statistics.fmean(neg_agr):.3f} "
          f"median_agr={statistics.median(neg_agr):.3f}")
    print(f"  amp>0: n={len(pos_agr)} mean_agr={statistics.fmean(pos_agr):.3f} "
          f"median_agr={statistics.median(pos_agr):.3f}")
    print(f"  corr(amp, implied_agr) = {_corr(amps, [a for r, a in zip(rows, implied) if r[6] is not None]):.3f}")
    print(f"  corr(margin, stored_conf) = {_corr(margins, [r[8] for r in rows if r[5] is not None]):.3f} "
          "(legacy formula has no margin term)")

    # --- margin-gate fit ---------------------------------------------------
    med_margin = statistics.median(margins)
    k_fit = med_margin / math.log(2.0)
    print("\nmargin-gate fit: gate(m) = 1 - exp(-m/K), K = median/ln2")
    print(f"  median_margin={med_margin:.5f} -> K={k_fit:.5f} "
          f"(pinned CONFIDENCE_MARGIN_K=0.0332)")
    for m in (0.002, med_margin, 0.067, 0.20):
        print(f"  gate({m:.4f}) = {margin_gate(m):.4f}")
    alpha_target = 0.45 / (math.log(3.0) * 0.5 * 0.5)
    print(f"\nalpha fit: fresh ev=2, agr=0.5, median margin -> 0.45 target: "
          f"alpha = 0.45/(ln3*0.5*0.5) = {alpha_target:.3f} "
          f"(pinned CONFIDENCE_ALPHA_CAL=1.639)")
    print(f"  lambda unchanged: half-life "
          f"{math.log(2) / CONFIDENCE_LAMBDA / 86400:.1f} days")

    # --- replay: legacy vs calibrated at ref_ts --------------------------
    print("\nreplay at ref_ts (age = ref_ts - row_ts):")
    print("  id  ev  age_h  legacy  calibrated  margin")
    new_confs = []
    for r, agr in zip(rows, implied):
        age = ref_ts - r[1]
        new = confidence_from_evidence(r[7], agr, age, margin=r[5],
                                       amplification=r[6])
        new_confs.append(new)
        print(f"  {r[0]:<3d} {r[7]:<3d} {age / 3600:<6.2f} {r[8]:.4f}  {new:.4f}      {r[5]:.4f}")
    legacy_stats, new_stats = _stats(confs), _stats(new_confs)
    print(f"\nlegacy stored:     {fmt(legacy_stats)}")
    print(f"calibrated replay: {fmt(new_stats)}")
    print(f"spread ratio (new std / legacy std): "
          f"{new_stats['std'] / legacy_stats['std']:.2f}x"
          if legacy_stats["std"] else "spread ratio: n/a")
    fresh = confidence_from_evidence(2, 0.5, 0, margin=med_margin)
    print(f"\nfresh ev=2 agr=0.5 median-margin calibrated conf: {fresh:.4f} "
          f"(legacy flat ev=2 value: 0.2291)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DEFAULT_DB),
                    help="profile DB snapshot (opened read-only)")
    args = ap.parse_args()
    raise SystemExit(main(Path(args.db)))
