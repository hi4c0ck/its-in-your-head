"""PULSE tool-row momentum (Tier-0 positive feedback).

Deterministic, pure (no I/O, no clock): a rich-get-richer surge signal
that answers the user's "terminal dominant -> now lots of compute_use,
it should jump forward" case in Sketch mode, where the semantic
cosines (goal/good/friction) are near 0.

Momentum is a REAL usage signal, not a dead cosine: it compares how much
of the RECENT activity a tool owns (recent capsule share) to how much of
the long-run activity it owned (cumulative call share). A tool that is
"hotter" than its long-run baseline is *surging* and its momentum is
amplified so it pops to the front of the matrix. A tool that has cooled
below its baseline is *cooling*.

The dashboard's isolated child cannot import the agent-side modules
(plugin root is off its sys.path), so the route mirrors this math
inline (see ``dashboard/plugin_api.py`` /pulse). Keep the two in sync.
"""
from __future__ import annotations

# Tunable knobs (constants for now; could become C4 settings later).
MOMENTUM_GAIN = 0.6        # < 1 -> expanding curve: mild surges stay visible
MOMENTUM_BAND = 0.02       # |surge| below this reads as "steady"
MOMENTUM_DECIMALS = 4      # round display values to 4 places (stable pins)


def _shares(counts: dict, total: int) -> dict:
    """count / total per tool (0.0 when the total is 0)."""
    if not total:
        return {k: 0.0 for k in counts}
    return {k: counts.get(k, 0) / total for k in counts}


def surge_values(recent_counts: dict, cumulative_counts: dict) -> dict[str, dict]:
    """Per-tool momentum from recent + cumulative activity maps.

    ``recent_counts`` / ``cumulative_counts`` map tool -> int activity
    (recent capsule feed vs cumulative call count). Either or both may be
    empty/partial; a tool missing from a map contributes 0 there.

    Returns tool -> {"momentum": signed rich-get-richer term in [-1, 1],
                     "surge": recent_share - cum_share,
                     "value": display value in [0, 1] (0.5 = steady),
                     "state": "surging" | "steady" | "cooling"}.

    Pure + deterministic: same two maps -> byte-identical output.
    """
    tools = sorted(set(recent_counts) | set(cumulative_counts))
    recent_total = sum(max(0, v) for v in recent_counts.values())
    cum_total = sum(max(0, v) for v in cumulative_counts.values())

    recent_share = _shares(recent_counts, recent_total)
    cum_share = _shares(cumulative_counts, cum_total)
    surge = {t: recent_share.get(t, 0.0) - cum_share.get(t, 0.0) for t in tools}

    # Normalize by the strongest |surge| so the top surger always reads
    # 1.0 (responsive, non-dead) and the rest spread by relative rank.
    peak = max((abs(surge[t]) for t in tools), default=0.0) or 1.0

    out: dict[str, dict] = {}
    for t in tools:
        s = surge[t]
        frac = abs(s) / peak
        if s == 0.0:
            momentum = 0.0
        else:
            momentum = (1.0 if s > 0.0 else -1.0) * (frac ** MOMENTUM_GAIN)
        value = 0.5 + 0.5 * momentum
        state = "surging" if s >= MOMENTUM_BAND else \
            "cooling" if s <= -MOMENTUM_BAND else "steady"
        d = MOMENTUM_DECIMALS
        out[t] = {
            "momentum": round(momentum, d),
            "surge": round(s, d),
            "value": round(min(1.0, max(0.0, value)), d),
            "state": state,
        }
    return out


def empty_momentum() -> dict:
    """The neutral cell shape for a tool with no activity data."""
    return {"momentum": 0.0, "surge": 0.0, "value": 0.5, "state": "steady"}
