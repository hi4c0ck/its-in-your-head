# MOVEMENT-CONTRACT.md — template-owned portrait motion (Stage 1 of 5)

Owner: lane-d (`desktop/`), settled by the parent run.
Governing doc: the 5-stage "Portrait Motion System" upgrade design (owner-pasted,
2026-11-09). This file is the repo tracker: what is implemented, what the
contract says, and what the remaining stages must do.

## Status

| Stage | Scope | State |
|-------|-------|-------|
| 1 | Fix the movement contract + widen ranges + 48px visibility measurements | **DONE** (this commit) |
| 2 | Alias compiler: engine emits normalized semantic axes; templates declare axis→feature aliases, response curves, sensitivity (no mandatory universal aliases) | pending |
| 3 | Head-movement parts: template-declared parts, coordinated transforms, trajectories, allowed regions | pending |
| 4 | Template renderer: each template renders its own visual language at studio AND compact sizes (compact currently shares the studio builder) | pending |
| 5 | Tuner upgrade: pixel-level checks (image diff between adjacent steps) in the browser, per-template calibration UI | pending |

## The contract (Stage 1 shape)

**Engine (template-independent):**
- `model.axes` — Layer 1. The six semantic axes normalized to −1..+1
  (`a_i = 2v_i − 1`), each with confidence as **metadata**
  (`{value, confidence, source}`), never multiplied into the value.
  Missing input ⇒ no signal (axis rests at 0, source `"absent"`).
- The shared motion layer still emits its signal deltas in the legacy
  travel bands (`MOTION_ENVELOPE` semantics) — kept so the legacy
  soft-flow template and existing consumers are unchanged.
- `MOTION_GAIN` = 4.0 with the normalized response
  `r = tanh(g·u)/tanh(g)` (|u|=1 maps to exactly ±1, so travel bands are
  used at their declared extent).

**Templates (sovereign authors):**
- `registerPortraitTemplate({..., movement})` — each template declares:
  - `travel`: how far each feature moves at |signal| = 1, in ITS OWN
    viewBox units (feature-relative: gaze = 35–60% of the safe eye
    interior, brow = 30–50% of the brow band, aperture = 20–30% of the
    range, mouth = 55–70% of its cloud, head = 5–7°, contour ≈ 30% of
    local clearance).
  - `regions`: the safe geometry each feature may occupy; **generated
    points never leave them** (clamped in `movementFor`).
  - `pxPerUnit` (48px compact scale = 48/570 ≈ 0.0842),
    `minVisibleAt48` / `minVisibleCoordinated48` (visibility commitment;
    0 = legacy, reported-only), `responsiveBand` (the 0.1-step regime
    where the curve still traverses ≥25% of a band; at gain 4.0 that is
    steps 0.0–0.1).
- `movementFor(model, templateId)` — remaps the shared signals into the
  template's travel bands and clamps inside its regions. Same output
  shape as `model.motion`, so builders keep their interface through the
  migration. Rest invariance: a resting poll moves nothing.

**Measurement (deterministic, no rasterization):**
- `visibilityReport(templateId)` — steps both live controls in 0.1
  increments at full amplitude (intensity 1.0, confidence 1.0), measures
  per-feature displacement in px at 48px, and gates:
  - every 0.1 step inside `responsiveBand`: primary feature ≥
    `minVisibleAt48` **or** coordinated travel ≥ `minVisibleCoordinated48`;
  - full-travel 0→1: coordinated ≥ `minVisibleCoordinated48`;
  - every pupil position (near + far, effective coordinates) inside the
    declared boxes. Saturated-tail steps are reported, not gated — the
    bands end by construction, inside the regions.

**Current declarations:**
- `neutral-pencil`, `angular-pencil` — `STUDIO_MOVEMENT`: widened for
  48px (commit: 0.5px primary / 1.0px coordinated per 0.1 step in the
  responsive band; measured 1.03–2.01px coordinated, full travel 4.23px).
- `soft-flow-study` — `SHARED_MOVEMENT` (legacy travel + wide regions):
  byte-identical look to pre-migration; verified 1:1 against
  `model.motion` in `tests/test_desktop_48px_visibility.mjs`.

## Pipeline

- `desktop/portrait-engine.mjs` is the **source of truth** for the engine
  block. `node tools/build-inline-engine.mjs` splices it into
  `desktop/plugin.js` between the `IYH-PORTRAIT-ENGINE-START/END` markers,
  verifying byte-identity outside the markers (the desktop loader stays
  single-file). Always edit the `.mjs`, then build, then regenerate the
  tuner: `py -3.12 tools/build_motion_tuner.py` (it embeds the same block
  and prints the engine sha; a stale HTML prints an old one).
- Tests: `node tests/test_desktop_48px_visibility.mjs` (contract +
  48px), `node tests/test_desktop_motion_grammar.mjs` (grammar),
  `node tests/test_desktop_portrait_engine.mjs` (routes/state),
  `py -3.12 -m pytest tests/ -q` (perception lane).
