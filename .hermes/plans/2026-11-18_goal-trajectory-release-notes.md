# v0.1.31 — global GOAL trajectory

## What changed
- Global GOAL now evaluates the whole operation trajectory (evolving
  objective with version history, bounded operation history, retrospective
  re-assessment) instead of per-tool similarity alone.
- Per-tool GOAL lanes remain v2-compatible diagnostic evidence.
- Schema v12 adds `ended_ts`, `result_assessment`, `historical_category`
  and current assessment/category separation for operations.
- Reset wipes `pulse_goal_history` and `pulse_operations` together with
  the in-memory objective, trajectory, pending calls and tool-turn state.
- Live API and DB fallback share `pulse.goal_trajectory.signal_from_state`.
- Pre/post events correlate by `tool_call_id`; operation labels are bounded
  sanitized signatures (no raw arguments or results).

## Honest limits
- `HYSTERESIS = 0.01` is score quantization / churn damping, not a
  full hysteresis model with separate enter/exit thresholds.
- Absent similarity stays `null` and is excluded from the global value;
  it is never rendered as zero progress.
- A successful tool call is not progress evidence by itself; only
  objective-aligned subsequent movement can confirm a detour.

## Gates
- `pytest -q` green.
- Three-story metrics board regenerated with `aligned`, `exploring`,
  `drifting`, `recovering` and `productive_detour` trajectories.
