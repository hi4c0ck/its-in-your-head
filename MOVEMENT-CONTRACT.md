# MOVEMENT-CONTRACT.md — template-owned portrait motion (Stage 3 of 5)

Owner: lane-d (`desktop/`), settled by the parent run.
Governing doc: the 5-stage "Portrait Motion System" upgrade design (owner-pasted,
2026-11-09). This file is the repo tracker: what is implemented, what the
contract says, and what the remaining stages must do.

## Status

| Stage | Scope | State |
|-------|-------|-------|
| 1 | Fix the movement contract + widen ranges + 48px visibility measurements | **DONE** |
| 2 | Alias compiler: engine emits normalized semantic axes; templates declare axis→feature aliases, response curves, sensitivity (no mandatory universal aliases) | **DONE** |
| 3 | Head-movement parts: template-declared parts, coordinated transforms, trajectories, allowed regions | **DONE** |
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
  48px), `node tests/test_desktop_alias_compiler.mjs` (alias graph,
  Stage 2), `node tests/test_desktop_head_parts.mjs` (coordinated
  parts, Stage 3), `node tests/test_desktop_motion_grammar.mjs` (grammar),
  `node tests/test_desktop_portrait_engine.mjs` (routes/state),
  `py -3.12 -m pytest tests/ -q` (perception lane).

## The alias compiler (Stage 2)

Templates may now author their own **axis → group → feature** graph:

- `registerPortraitTemplate({..., aliases})` where `aliases` is:
  ```js
  {
    groups: {
      focus:  { axes: { analytical_depth: 1.0 }, curve: 'tanh',
                gain: 3, sensitivity: 2.0 },
    },
    features: {
      brow: { rest: 0, focus: -8.0 },        // template's OWN coefficients
    },
  }
  ```
  - **A declared group/feature replaces the shared definition — no
    implicit merge.** Undeclared keys keep the shared formula. Templates
    may also invent new groups/features (union of keys).
  - **No mandatory universal aliases:** a template may declare nothing
    (byte-identical to legacy) or only a slice of the graph.
  - Curves: `linear` (identity), `tanh` (`tanh(g·u)/tanh(max(1,g))`),
    `step` (sign). `sensitivity` scales the weighted sum before the
    ±1 clamp; `gain` shapes the curve.
- `expressionFor(model, templateId)` — the template-owned expression:
  groups recomputed through its graph, features recomputed from
  `rest + Σ coefficient · source(group-or-axis)`. Missing input ⇒ no
  signal (rest value only). Deterministic; same model + template ⇒
  same expression.
- **Byte-identity guarantee:** the default alias set
  (`DEFAULT_ALIASES`) reproduces the legacy shared expression layer
  exactly — all three stock templates fall back to it, and
  `tests/test_desktop_alias_compiler.mjs` asserts `===` on all 9
  expressive channels for every stock template.
- Both glyph and studio builders now read `expressionFor(model,
  template)` instead of `model.expression`; `model.expression` stays
  available for legacy consumers.

## Head-movement parts (Stage 3)

Templates declare **coordinated parts** — head, jaw, shoulders, hair
mass — that move as transforms, not isolated vertex nudges:

```js
registerPortraitTemplate({
  ...,
  parts: {
    jaw: {
      tx: { mouthTension: 0.5 },        // feature-motion channel weights
      ty: { mouthLift: 0.6 },           // (movementFor output)
      region:  { x, y, w, h, clearance }, // safe box in template space,
                                          // stroke clearance respected
      envelope: { x: 0.6, y: 0.7, r: 7 },// fractions of the
                                          // clearance-reduced half-extents
      traverse: { period: 3.2, phase: 0.4, amplitude: 1.5,
                  alias: { warmth: 0.5 } }, // Stage 2 alias groups bias
                                          // the traversal cloud spread
    },
  },
})
```

- `partStateFor(model, templateId, t)` — pure, deterministic part
  states at local clock time `t` (the caller owns the clock; the engine
  owns none — a process restart returns to neutral). Part state =
  Σ channel weights × feature motion + traversal term
  `amp·(sin(θ) − sin(φ))` (exactly 0 at t = 0), clamped into
  region × envelope.
- **Coordination:** one channel drives several parts at their own
  weights (headTilt → head 1.0, shoulders 0.3, hair 0.4); parts
  respond to multiple channels. Different parts run different tempos.
- **Safety:** `partSafetyReport(templateId)` sweeps every axis
  extreme × the full traversal period and verifies all sampled states
  stay inside the declared region × envelope (the clamp guarantees
  this by construction; the audit is the regression check).
- **Migration safety:** a template that declares no parts gets an
  EMPTY part layer — renderers stay byte-identical to pre-Stage-3.
  `neutral-pencil` and `angular-pencil` now declare head/jaw/
  shoulders/hair_mass (angular gives the jaw more of the cloud);
  `soft-flow-study` stays legacy.
- Renderers CONSUME part states in Stage 4 — until then the part
  layer is machinery + declarations only, and no pixels move.
