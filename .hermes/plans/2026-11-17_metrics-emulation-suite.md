# Metrics Emulation Suite Implementation Plan

> **For Hermes:** implement task-by-task; each task has a failing check, a minimal
> implementation, a green check, and a commit.

**Goal:** an in-repo harness that emulates 50–100-turn sessions through the REAL
its-in-your-head engine (no reimplemented scoring, no real user text), exports
per-turn metric/portrait snapshots as JSON, and a standalone board that shows a
compact corner-by-corner view with portraits rendered by the REAL portrait engine.

**Architecture:** Python CLI drives `metrics.set_db_path()` + `engine.Engine().on_events()`
with synthetic `turn` / `tool_start` / `tool_end` / `reasoning_seg` events (short
core prompts, tool names, req/resp metadata only). It captures `portrait_view()`,
`drift_view()`, `pulse_view()` and `upset` verbatim from the engine after each
turn. A build script emits a self-contained `board.html` that inlines the
production `IYH-PORTRAIT-ENGINE` block from `desktop/plugin.js` (same recipe as
`tools/build_motion_tuner.py`) and reads the exported JSON. No server, no build
step, no plugin changes.

**Tech Stack:** Python 3.14 stdlib + pytest (repo test venv), Node for the
`.mjs` source gates, plain HTML/CSS/ES5-style JS in the generated board.

---

## Decisions (owner input + measured)

| Decision | Value | Why |
|---|---|---|
| Board delivery | standalone generated `board.html` + Python CLI | owner left open; no server/build, works from `file://` |
| Results | one JSON per run under `tests/metrics_emulation/runs/` (gitignored) | owner: "JSON is ok" |
| Turns | **80 default**, configurable 40–100 | measured 20 turns = 1.54 s with full views → 80 ≈ 6 s/story, 3 stories ≈ 18 s; stochastic confidence without mass data |
| "one portrait template per test" | one `DRAWING` template id per test (from `IYH_PORTRAIT.listPortraitTemplates()`), with `ink` flag + `animated` follow-ups | owner correction: DRAWING extends template, has Ink param and animated follow-ups |
| Per-test start | deterministic seed selects one DRAWING template; same seed replays exactly | one test = one template; reproducible |
| Real-flow boundary | ALL numeric values come from `Engine` views; board only renders them with the real portrait engine | owner: "must not dream or unrequested inventions" |

## Real-flow anchors (verified this session)

- `core/events.py`: `turn`/`user_turn` prompt event requires `user_message`;
  `tool_start`/`tool_end` require `tool_name`; `reasoning_seg` requires `turn_id`.
- `metrics.py:186 enqueue`, `metrics.py:231 _row_from_event`, `metrics.py:295
  _persist_batch`, `metrics.py:346 _worker_main` (persist → engine feed → tick,
  raw scrub last). Harness calls `Engine.on_events(batch)` directly = the worker
  feed path (test convention `tests/test_engine_wiring.py:33-63`).
- `engine.py:336 on_events` routes: `tool_start` → `pulse.handle_pre_tool_call`,
  `tool_end` → `_on_tool_end` (capsule + prototypes), `turn` → `_on_user_turn`
  (upset, goal vector, portrait observed, drift history append).
- `engine.py:1373 pulse_view`, `:1489 portrait_view`, `:1531 drift_view` —
  authoritative snapshots; `upset` block comes from `pulse_view()["upset"]`.
- `dashboard/plugin_api.py` reads the same engine/DB state; no separate scorer.
- `desktop/portrait-engine.mjs` + `IYH-PORTRAIT-ENGINE-START/END` block in
  `desktop/plugin.js` — real renderer; `buildPortraitSvg(data, options)` with
  `options.template`, pane clock = one 850 ms step per fresh poll.
- Template ids: `neutral-pencil`, `angular-pencil`, `soft-flow-study`.
- Benchmark: `20 turns = 1.54 s` including all three views (tier-0 backend).

## Directory layout

```
tests/metrics_emulation/
├── __init__.py            # package marker (stdlib only)
├── cli.py                 # `python -m tests.metrics_emulation.cli ...`
├── harness.py             # story → events → real engine → snapshots → JSON
├── stories.py             # the 3 owner-specified arcs + phase metadata
├── schema.py              # JSON schema id, compact snapshot extraction
├── build_board.py         # generates board.html (inlines real engine block + sha)
├── board.html             # GENERATED, committed (self-contained)
├── README.md              # run/verify instructions
└── runs/                  # gitignored JSON outputs
tests/test_metrics_emulation.py   # pytest gates for harness + real-flow invariants
```

## JSON shape (`iyyh-metrics-emulation/v1`)

```json
{
  "schema": "iyyh-metrics-emulation/v1",
  "run": {"story": "friction-recovery", "seed": 8117, "turns": 80,
          "drawing": {"template": "neutral-pencil", "ink": false, "animated": true}},
  "provenance": {"repo_commit": "...", "python": "3.14.9",
                 "portrait_engine_sha": "<sha12>", "engine_module": "engine.py"},
  "phases": [{"id":"default","from":0,"to":26,"label":"steady work"}, ...],
  "corners": [{"turn": 0, "phase": "default", "label": "start"}, ...],
  "turns": [{"i":0,"phase":"default","event_summary":{"tools":[...],"user":"core:fix"},
             "portrait":{...real portrait_view compact...},
             "drift":{"directness":[0.51,...], ...},
             "pulse":{"lanes":{"live":[...],"goal":[...],"good":[...],"friction":[...]},
                      "rows":N},
             "upset":{"F":..., "tier":..., "mood":...}}],
  "summary": {"portrait_span": {...}, "max_drift": {...}, "friction_peak": ...}
}
```

Compact extraction keeps every NUMBER exactly as produced by the engine; it
drops only bulky keys not needed by the board (full perception pattern text,
raw event dicts). No calculated value is re-derived in the harness.

## Stories (all three are mandatory; deterministic from seed)

1. **`friction-recovery`** — ~1/3 default work (short core prompts, `read_file`/
   `patch` ok), then user friction + model loop with no result (`still failing`,
   `again`, error tool_end, repeated same tool), then tooling adjustment and
   good instructions, final `thanks, that works` + `session_end`. Corner labels:
   steady → stuck → adjusted → complete.
2. **`friction-to-good`** — starts with solid friction + weak GOAL data (harsh
   short prompts, error tool_end), then increasingly clear instructions and
   smooth ok tool calls; portrait must move toward the good/aligned expression
   and the bubble drift must be visible in the saved lane values. Corner labels:
   poor goal → friction peak → guidance → smooth.
3. **`volatile-amplification`** — default start, then volatile prompting
   (alternating terse approval / rejection / topic switches) across the whole
   session; repeated large portrait drift. Corner labels: baseline → swings →
   late swing.

Each story is a declarative phase list; the harness expands phases into events.
All user text is short synthetic core phrases (no real prompts, no personal
data). Tool names are real tool ids from the plugin's own vocabulary.

## Board (generated, self-contained)

- Left rail (~240 px): one row per run: `select`, bookmark ★ toggle, delete ×,
  tiny stats (turns, drift max, friction peak, confidence). Bookmark/delete are
  board-local (localStorage); a run file is never deleted by the page — CLI has
  explicit `--purge` for disk removal. Stated in the README.
- Main: 
  - header: story label, DRAWING template id, Ink state, animated state, seed.
  - **corner strip**: shrunk phase bands + corner cards (turn index, label,
    mini portrait) — no full step log.
  - portrait stage: `buildPortraitSvg(turn.portrait, {template, rendering:'studio',
    id, clock})` from the inlined REAL engine, with the test's Ink param
    (invert-source equivalent) and `animated` follow-ups replayed at the
    production 850 ms poll-step clock; STEP / PLAY / slider controls.
  - metrics row: six axis values + confidence, F'/tier/mood, per-lane top
    entries, drift sparkline — all read from JSON, nothing recomputed.
- Theme vars with fallbacks; no hardcoded plugin colors; works light/dark.

## Tasks

### Task 1 — scaffold + real-flow smoke test
Create `tests/metrics_emulation/__init__.py`, `schema.py`, `harness.py` with
`run_story()` that feeds the real `Engine` and returns snapshots. Test
`tests/test_metrics_emulation.py::test_real_engine_flow` asserts portrait
`available`, 6 dims, drift length == turns, pulse rows non-empty, and that no
scoring helper is reimplemented (source check: harness imports `engine`, not
`pulse.matrix` directly). Commit.

### Task 2 — the three stories
Create `stories.py` with the declarative phase lists. Tests assert each story's
phase coverage, final corner, and that all three produce valid runs. Commit.

### Task 3 — CLI + JSON export
Create `cli.py`: `--story friction-recovery|friction-to-good|volatile-amplification|all`,
`--turns 80`, `--seed`, `--out tests/metrics_emulation/runs`, `--purge`.
Test determinism: same seed+story → byte-identical JSON (except provenance
commit field, which is fixed by flag for the test). Commit.

### Task 4 — board generator
Create `build_board.py` → `board.html`, inlining the engine block with sha,
embedding a demo run (or fetch via file input). Test: generated HTML contains
`IYH-PORTRAIT-ENGINE` marker content + sha + no external fetches. Commit.

### Task 5 — board UI
Implement rail management, corner strip, portrait stage, metrics row, play/step.
Verify in browser with DOM assertions + SVG render count + screenshot. Commit.

### Task 6 — docs + full gates
`tests/metrics_emulation/README.md`; run all 3 stories, full pytest, contracts,
temp budget, `.mjs` suites, ESM check. Record actual numbers in the plan's
verification section. Commit.

## Verification (2026-11-17 — real results)

- `pytest tests/test_metrics_emulation.py -q` — **6 passed, 1 warning in 17.15s** (added the GOOD/FRICTION feedback-path regression)
- `python -m tests.metrics_emulation.cli --all --turns 80 --seed 8117` (after feedback + cadence fix):
  - `friction-recovery`: 80 turns, 9 corners, portrait span **0.611**, Fpeak 0.3148, 48.8 s
  - `friction-to-good`: 80 turns, 7 corners, span **0.558**, Fpeak 0.3245, 57.5 s
  - `volatile-amplification`: 80 turns, 7 corners, span **0.573**, Fpeak 0.3148, 46.1 s
  - GOOD/FRICTION lanes now graded (9–25 distinct values per lane) instead of 0 / 0.3.
- `build_board.py` → `board.html` 2,656,775 bytes; board browser: 3 runs, 9 corners,
  corners wrapped (no horizontal scroll), 1 real SVG with 50 paths, 6 axes, 4 lanes,
  zero JS errors; run switch / step / seek / star / hide all verified.
- Full suite: **659 passed, 8 skipped, 1 warning in 84.63s** (includes the engine
  no-jumps + contra-verse + volatility regressions and speedrun gate)
- `tools/check_contracts.py`: `G5: CLEAN` · temp budget `1.8 MB` · plugin ESM check OK
- `test_desktop_bubble.mjs` 142, `test_desktop_pulse.mjs` 113,
  `test_pulse_tick_reducer.mjs` 71, calibration/encoder/petstate suites all pass.

### Engine fix verified 2026-11-17 (the actual product changes)
- `perception/portrait.py::STRONG_R_DEFAULT` 0.5 → **0.15**: measured |R| is
  0.005–0.09, so the old threshold was unreachable (T 0.47–0.59, step ~0.002).
  Now realistic |R|=0.06 gives Q~0.4 / T~0.70 / ~0.05 step; R=0 still gives
  Q=0 and NO movement; DELTA_MAX=0.18 still caps each step. Volatile story
  span 0.142 → 0.573. Gate: `test_volatile_weak_contrast_moves_portrait`.
- `pulse/matrix.py::_channel_gate` + `_lane_weights` + `_lane_accumulation`:
  hard-gated MEAN → continuous gate × recency weight with saturating sum.
  Good/friction per-tool values move in bounded steps (max adjacent step 0.034).
- `pulse/matrix.py::build_lanes`: GOOD and USER-FRICTION now split one shared
  evidence budget (`activation × share`). Per-tool pair sums ≤ 0.996 in all
  three 80-turn runs (never both 0.9+); evidence shifts move them oppositely.
  Objective friction remains a separate capped hint.
- `engine.py::pulse_view(now=None)` optional data clock threaded to velocity and
  `build_lanes`, so `--speedrun` forces real engine polls at chosen times.
- New gates: `test_good_friction_values_are_continuous_no_jumps` (similarity +
  age sweeps), `test_good_and_friction_share_one_budget_contra_verse`,
  `test_speedrun_forces_engine_polls_at_chosen_times`,
  `test_volatile_weak_contrast_moves_portrait`.
- Full suite **659 passed, 8 skipped, 1 warning in 84.63s**.

Observed honest result: with the recalibrated r_d the engine now follows the
alternating prompts with a per-step cap of 0.18 (mean step 0.027–0.046); the
volatile story shows real, visible drift, not fabricated jumps; this is the real
engine's capacity, which is what the suite is meant to reveal.

## Out of scope

- No changes to engine/scoring code, no plugin code changes, no real user text,
  no ONNX worker, no server, no wiki changes.
- No claim that a story "proves" product quality beyond what the saved real
  engine values show.
