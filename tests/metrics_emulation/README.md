# Metrics emulation suite — usage

Runs synthetic 50–100-turn sessions through the **real** its-in-your-head engine
and shows the result on a self-contained board. No scoring is reimplemented,
no real user text is used, no server is needed.

## What is real vs synthetic

| | |
|---|---|
| **Real** | `engine.Engine().on_events()` batch feed (same path the metrics worker uses), `portrait_view()`, `drift_view()`, `pulse_view()`, the `/pulse` upset block, and the production portrait renderer (`IYH-PORTRAIT-ENGINE` block from `desktop/plugin.js`, rendered by `IYH_PORTRAIT.buildPortraitSvg`). |
| **Synthetic** | the event stream: short core prompts, tool names, ok/error status, req/resp metadata. Prompt strings are the repo's OWN anchor phrases (`perception/resemblance.py:71-82`) and feedback labels (`pulse/feedback.py FEEDBACK_RULES`). |

The engine moves portrait values smoothly; the board shows the real per-turn
values and real drift — it does not invent larger swings.

### Engine fixes (2026-11-17)

1. **ENGINE FIX — portrait followed volatility poorly.** The driver is
   `r_d` (the strong-contrast threshold): `T = 0.5 + R/(2r_d)` and
   `Q = |R|/r_d`, so `alpha = ALPHA_MAX·Q`. Measured |R| is only 0.005–0.09,
   but `STRONG_R_DEFAULT` was 0.5 — unreachable. Targets stayed in 0.47–0.59
   and the step was ~0.002/turn. `r_d` is now **0.15** (~2x the observed
   scale): realistic |R|=0.06 gives Q~0.4, T~0.70, a ~0.05/step. Neutral
   evidence (R=0) still gives Q=0 and **no movement** — the "absence is not
   evidence of absence" invariant is preserved, and the per-step cap
   (DELTA_MAX=0.18) still binds. Measured 80-turn portrait span:
   volatile 0.142 → **0.573**, friction-recovery 0.278 → 0.611,
   friction-to-good 0.263 → 0.558; max adjacent step exactly 0.18 (the cap).
   Gate: `test_volatile_weak_contrast_moves_portrait`. The six-dimension layer
   stays — it is the stable identity layer; volatility now reaches it.
2. **GOOD/FRICTION were flat because feedback association was missing.**
   `core/collector.py::_on_pre_llm_call` calls `_apply_explicit_feedback()` →
   `pulse.feedback.apply_feedback()` on every user turn. The harness now calls
   those same real functions in the same order (after enqueue, before the
   worker feed), so explicit "thanks" / "still broken" cores label the real
   prototype banks. With a realistic 40 s/turn cadence ending at "now", the
   1 h accumulation half-life yields graded lane values (0.5–0.9), not 0/1.
   Gate: `test_explicit_feedback_drives_good_and_friction_lanes`.
3. **ENGINE FIX — gapped GOOD/FRICTION values.** `_lane_accumulation` used a
   HARD `similarity >= threshold` include/exclude plus a MEAN over the matching
   capsules, so one capsule crossing the gate moved the lane by a whole step.
   Now a continuous channel gate × recency weight, accumulated as
   `1 - exp(-weighted / half_life)` ("no jumps"). 3/6/12 calls → 0.31/0.53/0.78.
4. **ENGINE FIX — contra-verse GOOD/FRICTION.** Independent saturating sums let
   one tool read 0.9+ in BOTH lanes (a capsule can match both banks). The two
   lanes now share ONE evidence budget: `value = activation(total) × share`,
   with `activation = 1 - exp(-(good_w + fric_w) / half_life)`. With one channel
   empty this reduces exactly to the documented accumulation; with both, their
   sum never exceeds the activation (measured max pair sum 0.996) and shifting
   evidence moves them in opposite directions. When evidence is genuinely
   balanced they sit near 0.5/0.5 — that is the real state, not saturation.
   Objective friction stays a separate capped hint, never summed.
   Gate: `test_good_and_friction_share_one_budget_contra_verse`.

## Speedrun mode (forced engine poll times)

`Engine.pulse_view(now=...)` is an optional data clock (production path keeps
wall time). The harness uses it to force real engine polls at chosen virtual
times after each turn:

```bash
$PY -m tests.metrics_emulation.cli --all --turns 80 --speedrun
$PY -m tests.metrics_emulation.cli --story friction-recovery --speedrun \
    --poll-offsets 0,0.6,0.85,2,1800,3600
```

`0,0.6,0.85,2` mirror the pane's poll/reducer/travel/arrival timing; larger
offsets (1800/3600) expose half-life decay deterministically. Each turn's
`polls` array in the JSON records `goal/good/friction/upset` at every forced
poll. Gate: `test_speedrun_forces_engine_polls_at_chosen_times`.

## Run

```bash
PY=<venv>/Scripts/python.exe

# fast gates (12 turns each)
$PY -m pytest tests/test_metrics_emulation.py -q

# full 80-turn runs (approx 6-35 s each; 3 stories ~1 min total)
$PY -m tests.metrics_emulation.cli --all --turns 80 --seed 8117

# one story, custom length/seed
$PY -m tests.metrics_emulation.cli --story friction-recovery --turns 60 --seed 42

# regenerate the board after new runs
$PY -m tests.metrics_emulation.build_board

# remove generated run files (board "delete" only hides them locally)
$PY -m tests.metrics_emulation.cli --purge
```

Open `tests/metrics_emulation/board.html` directly in a browser (no server).

## Board

- Left rail: run select, bookmark (★), hide ("delete"), tiny stats — turns `◷`,
  portrait span `↗`, max drift `≈`, friction peak `!`. Bookmark/hide are stored
  in the page's localStorage; **delete does not remove the JSON file** — use
  `--purge` for that.
- Main: story header with the DRAWING template, `Ink` flag and `animated` flag
  (one template per run, chosen by seed from the three real templates), phase
  band with corner dots, and wrapped corner cards — crucial turns only, no full
  step log.
- Portrait stage: the real engine renders the selected turn's portrait with the
  run's template. `Ink on` passes light graphite; `animated on` advances the
  production 850 ms poll-step clock as you play/step. The clock is data-driven,
  not wall-time, exactly like the pane.
- Metrics: six axis values with confidence markers, the four PULSE lane tops,
  a directness drift sparkline, and F′ / tier / mood from the real upset block.

## Stories

1. `friction-recovery` — steady work (~1/3), then user friction and a model loop
   with no result, then tooling adjustment, clear instructions, and a short
   thanks; ends complete.
2. `friction-to-good` — starts with solid friction and weak GOAL data, then the
   user gives good instructions and the work smooths out; watch the lane values
   drift toward good.
3. `volatile-amplification` — default start, then alternating terse/opposite
   prompt styles across the session to show how the engine reacts to volatility.

## Files

- `harness.py` — story → events → real engine → snapshots → JSON
- `stories.py` — declarative arcs using repo-owned phrase tables
- `schema.py` — export schema + verbatim view extraction
- `cli.py` — run/purge CLI
- `build_board.py` → `board.html` — generated self-contained board
- `runs/` — generated JSON (gitignored); `board.html` embeds the current runs
