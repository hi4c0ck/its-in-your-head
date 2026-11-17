"""CLI: run emulation stories and write JSON results.

Examples:
  python -m tests.metrics_emulation.cli --all --turns 80
  python -m tests.metrics_emulation.cli --story friction-recovery --turns 60 --seed 42
  python -m tests.metrics_emulation.cli --purge
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from . import harness, stories

DEFAULT_OUT = Path(__file__).resolve().parent / "runs"


def main(argv=None):
    p = argparse.ArgumentParser(description="Run IYH metrics emulation stories")
    p.add_argument("--story", choices=stories.STORY_ORDER,
                   help="one story id (default: friction-recovery)")
    p.add_argument("--all", action="store_true", help="run all three stories")
    p.add_argument("--turns", type=int, default=80, help="turns per story (max 100 by default)")
    p.add_argument("--max-turns", type=int, default=100,
                   help="raise the per-run turn cap for long-session checks")
    p.add_argument("--seed", type=int, default=8117, help="deterministic seed")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory")
    p.add_argument("--purge", action="store_true", help="delete generated run JSON files")
    p.add_argument("--speedrun", action="store_true",
                   help="force engine polls at the given virtual offsets")
    p.add_argument("--poll-offsets", default="0,0.6,0.85,2",
                   help="comma-separated seconds after each turn for speedrun polls")
    a = p.parse_args(argv)

    if a.purge:
        removed = 0
        if a.out.exists():
            for pattern in ("friction-recovery-*.json", "friction-to-good-*.json",
                            "volatile-amplification-*.json"):
                for f in a.out.glob(pattern):
                    f.unlink()
                    removed += 1
        print("purged %d run file(s) from %s" % (removed, a.out))
        return 0

    offsets = tuple(float(x) for x in a.poll_offsets.split(",") if x.strip()) \
        if a.speedrun else ()
    ids = stories.STORY_ORDER if a.all else [a.story or stories.STORY_ORDER[0]]
    for sid in ids:
        t0 = time.perf_counter()
        run = harness.run_story(sid, turns=a.turns, seed=a.seed,
                                poll_offsets=offsets, max_turns=a.max_turns)
        path = harness.write_run(run, a.out)
        s = run["summary"]
        print("%-24s %3d turns  %2d corners  %s  span=%.3f  drift=%.3f  Fpeak=%.4f  %.1fs -> %s"
              % (sid, len(run["turns"]), len(run["corners"]),
                 "ok" if run["turns"][-1]["portrait"]["available"] else "EMPTY",
                 max([v or 0 for v in s["portrait_span"].values()]),
                 max([v or 0 for v in s["max_drift"].values()]),
                 s["friction_peak"] or 0.0,
                 time.perf_counter() - t0, path.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
