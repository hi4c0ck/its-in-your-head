# PULSE accumulate model — value builds from calls, not one prompt's similarity

## Problem
`good_value` / `friction_value` = `_floor(strongest_similarity(goal, bank))` (matrix.py:515, 530-533):
ONE prompt's max cosine vs the labeled prototype bank. When the bank is empty or the
goal is dissimilar → `None` → **0.00**. Calls never raise it (only explicit labels do).
User model: *"a few calls above the floor should reach a valuable score."*

Contrast: **LIVE** already does this — `surge_values(recent, cumulative)` (momentum.py:37)
gives a [0,1] value (0.5 = baseline) that rises/falls with recent-vs-cumulative share.
Pure, deterministic, no clock.

## Design (follows the diagnosis attachment's principles)
1. **Value = decaying accumulation on event-slices** (reuse `surge_values`):
   - **LIVE** (existing): all calls.
   - **GOOD** (new): slice to *successful/positive* calls → a few successes above the
     floor → rising value (was 0.00).
   - **FRICTION** (new): slice to *errors/timeouts* (objective) + labeled friction (user)
     → a few errors → rising value (was 0.00).
   - The recent window rolls / decays → value reflects RECENT accumulation, not total.
2. **Confidence = support/state, separated from value** (attachment: "a score should not
   vanish into neutral merely because its evidence is still provisional"). A provisional
   lane shows its real accumulated `value` + `state: learning/provisional` — NOT 0.00.
3. **Similarity preserved as a sub-field** (`evidence.similarity`), not the display value.
   Semantic quality stays available on the detail surface.
4. **Lane thresholds recalibrated** (goal .80 / good .60 / friction .40) on the new
   accumulate scale — 0.5 baseline shifts where "valuable" sits. Empirical, per-lane.
5. **EMA movement preserved** (bounded decay, no jumps — attachment principle #1).

## Data-model questions (confirm before coding — they fix the value formula)
- **GOOD event** = "successful call" (pure activity) or "call similar to good prototypes"
  (needs the labeled bank)? 
- **FRICTION event** = error/timeout delta from `errors`, or the objective-friction bank?
  Are recent capsules tagged error, or do we slice by the `errors` count delta?
- **Decay**: turn-based (per PULSE update) vs call-count window (PULSE is pure, no clock).

## Implementation steps
1. `momentum.py`: generalize `surge_values` → accept arbitrary recent/cumulative slices
   (success / error) or add `lane_surge(recent, cum)`.
2. `matrix.py:515,530`: `good_value`/`friction_value` = `lane_surge(...)` on the slices;
   keep similarity as `evidence.similarity`; keep `state`/`support`; recalibrate
   `DEFAULT_THRESHOLDS`.
3. `desktop/plugin.js`: pane shows the accumulate value (dots/tooltip) + confidence
   (state); similarity sub-field on the detail surface.
4. `tests/test_pulse_matrix.py` / `test_pulse_momentum.py`: pure asserts — value rises
   with calls, decays with window, per-lane thresholds; the empirical set (in-domain /
   near-competitor / out-of-domain).

## Decisions to confirm (the knobs that determine the value formula)
1. **GOOD = successful-call surge, FRICTION = error/timeout surge?** (event semantics)
2. **Decay window / half-life** (calls or PULSE-updates).
3. **Thresholds**: recalibrate now empirically, or start from 0.80/0.60/0.40?
4. **Scope**: PULSE only, or also the attachment's **P0** (engine.py interpreted-track
   value/confidence mix-up) + **P1** (patterns.py winner-share agreement) — those are
   the separate PERCEPTION system.

## P2 — grounded accumulate design (capsule `status` is the signal)
- Capsule schema (activity_encoder.py:29-33): each recent capsule has `status`
  (`"ok"` default, activity_encoder.py:127) + `ts`. `capsules_by_tool` is a
  bounded recent deque → old evidence rolls out = natural decay, NO clock.
- **GOOD accumulate** = recent-window evidence of `status == "ok"` calls.
- **FRICTION accumulate** = recent-window evidence of `status != "ok"` calls.
- **value (0-baseline, "starts at 0.0000"):**
  `acc(lane) = 1 - exp(-n_effective / half_life)`  → 0 when no recent evidence,
  rises with a few calls, decays as capsules roll out of the window. Pure +
  stateless (mirrors `surge_values`); `half_life` = the knob.
  (A few calls: n=3,hf=8 → 0.31; n=6 → 0.53; n=12 → 0.78.)
- **confidence** = the documented evidence-bank state/support
  (learning/provisional/ready) — SEPARATE from value; a provisional lane shows
  its real accumulated value, not 0.00 (attachment principle).
- **similarity** kept as `evidence.similarity` sub-field (not the display value).
- **thresholds** recalibrated on the 0-baseline accumulate scale (goal .80 /
  good .60 / friction .40 are similarity-scale; shift for the accumulate scale).

### Open knobs (confirm before coding the value formula)
1. Driver: pure `status` slice (good=ok, friction=error) vs channel-gated
   (only calls similar to that tool's labeled good/friction bank count)?
2. `half_life` default (calls) — and whether GOOD should also require
   `status=="ok"` + a labeled-good example to count (quality gate).

## P1 (structural, empirical-calibration-dependent)
- DONE (structural, safe): `score_turn` prompt-only mode now ranks by prompt
  affinity, not bank order (patterns.py:613). No calibrated constants touched.
- FLAGGED (needs the attachment's held-out calibration set, do NOT pin new
  constants without it = no cheats): replace winner-share `agreement`
  (engine.py:532: top_wins/total_turns) with pattern-specific reliability +
  the absolute-affinity ambiguity gate. The L11 constants
  (patterns.py:110-117) were fit to the OLD agreement, so re-fitting is
  required before pinning.

## Order: P0 (done) -> P2 accumulate (the named deliverable) -> P1 structural
## -> P2 desktop level/state audit + threshold recalibration.

