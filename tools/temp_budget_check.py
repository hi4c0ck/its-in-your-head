"""Temp-budget gate: fail a run if pytest basetemp grew past the budget.

The 39 GB %TEMP% incident (2026-11-06) was multi-GB stand-in model files
written into per-test tmp dirs by a downloader-fake test. This gate makes
that class of regression red immediately, locally and in CI:

    python tools/temp_budget_check.py            # default .pytest-basetemp, 100 MB
    python tools/temp_budget_check.py <basetemp> --max-mb 200

Rules:
- total bytes under basetemp must be < --max-mb
- NO single file under basetemp may exceed --max-file-mb (default 50 MB):
  suite temps are fixtures, not model staging. Staged models live under
  the plugin-data dir, never under pytest temps.

Exit 0 = within budget; 1 = over budget (lists the offenders).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _walk(p: Path):
    total = 0
    big = []
    for f in p.rglob("*"):
        if f.is_file():
            try:
                sz = f.stat().st_size
            except OSError:
                continue
            total += sz
            if sz > 50 * 1024 * 1024:
                big.append((sz, f))
    return total, big


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("basetemp", nargs="?", default=".pytest-basetemp")
    ap.add_argument("--max-mb", type=int, default=100)
    args = ap.parse_args(argv)

    base = Path(args.basetemp)
    if not base.exists():
        print(f"temp-budget OK: {base} does not exist (0 MB)")
        return 0

    total, big = _walk(base)
    mb = total / (1024 * 1024)
    ok = mb < args.max_mb and not big

    for sz, f in sorted(big, reverse=True)[:10]:
        print(f"  OVER-SIZED FILE: {sz / (1024*1024):.0f} MB  {f}")
    if ok:
        print(f"temp-budget OK: {base} = {mb:.1f} MB (< {args.max_mb} MB, no >50 MB files)")
        return 0
    print(f"temp-budget FAIL: {base} = {mb:.1f} MB (budget {args.max_mb} MB, "
          f"single-file cap 50 MB, {len(big)} oversized)")
    print("  -> a test fixture is writing model-scale bytes into pytest temps; "
          "shrink fakes to byte-sized stand-ins (see phase2 issue #4)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
