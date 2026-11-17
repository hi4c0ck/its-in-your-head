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
