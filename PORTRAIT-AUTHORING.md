# PORTRAIT-AUTHORING — Template & Portrait Creation Guide

Owner/creator-facing reference for the portrait system: what a template IS, every
layer you may author, the exact shapes and formulas, and the gates that must be
green before a new template counts as created. Companion tracker:
`MOVEMENT-CONTRACT.md` (stage status); invariants: repo `ARCHITECTURE.md` +
the `its-in-your-head-maintenance` skill.

Stance: **the engine owns the model; templates own the motion.** Six
normalized semantic axes come from perception (no raw text ever reaches a
renderer). Everything *visual* — travel, alias graph, parts, the drawing
itself — is template-owned. A template may stay a consumer of the shared
vocabulary (legacy) or become a full author of its own motion language
(the five stages below).

## 0. Where the code lives

- `desktop/portrait-engine.mjs` — **the single source of truth.** All engine
  code is edited here, then spliced into the pane:
  `node tools/build-inline-engine.mjs` (idempotent; splices the
  `IYH-PORTRAIT-ENGINE-START/END` block into `desktop/plugin.js`, asserts
  byte-identity outside the markers).
- `desktop/plugin.js` — the pane (one ESM file, loads uncompiled by the
  renderer). Pane-side rendering glue (template picker, 48px compact glyph
  mount, pane clock) lives in its `IYH-PORTRAIT-UI` block.
- `tools/build_motion_tuner.py` → `tools/portrait_motion_tuner.html` — the
  standalone tuning/acceptance surface (regenerate after ANY engine change:
  the tuner embeds the engine block + its sha256; a stale tuner is a lying
  sample).
- Tests: `tests/test_desktop_*.mjs` (plain TAP scripts — run `node
  tests/<file>.mjs` directly, never `node --test`).

## 1. What a renderer receives

`buildPortraitSvg(data, options)` builds the model via
`buildPortraitModel(data)` and calls your `render(model, options)`:

| model field | meaning |
|---|---|
| `axes` | 6 normalized axes `{value: -1..+1, confidence, source}` — observed source only, frozen |
| `expression` | **legacy shared** control values (openness/focus/warmth/gaze/brow/eyeOpen/mouthTension/mouthLift/headTilt/contourDrift/…) — retained for legacy consumers; template code should prefer `expressionFor` |
| `live`, `motion`, `morphology` | transient gesture displacement + slow drawing treatment (additive layers, never overwrite each other) |
| `available`, `signalStrength`, `interpretedStrength` | evidence metadata |

`options` your renderer gets: `template`, `templateLabel`, `rendering`
(`'compact'` = the 48px dock glyph, 570×570 crop viewBox; `'studio'` = the
720×760 full drawing), `id`, `clock` (caller-owned local animation phase,
seconds — see §9), `graphite`, `accentPalette` (`signal`/`interpreted` —
accent layers only; graphite is the main material).

**Privacy contract:** `model` carries normalized evidence only — no raw
prompts, messages, profile fields, or demographics. A renderer that needs
anything else is a contract violation.

## 2. Registering a template

```js
IYH_PORTRAIT.registerPortraitTemplate({
  id: 'my-style',                 // 2–48 chars, [a-z0-9_-], leading [a-z0-9]
  label: 'My style',
  description: '…',               // shown in the template picker
  render: (model, options) => '…complete SVG string…',  // REQUIRED function
  movement: MY_MOVEMENT,          // optional — stage 1 (absent → shared legacy travel)
  aliases:  MY_ALIASES,           // optional — stage 2 (absent → DEFAULT_ALIASES)
  parts:    MY_PARTS,             // optional — stage 3 (absent → no part layer)
})
```

- `render` MUST return a complete `<svg…>…</svg>` string (verified by
  `buildPortraitSvg`, which throws on anything else).
- Returns an unregister callback. `{ replace: true }` re-registers over an
  existing id (this is how calibration variants are installed — §8).
- All three optional declarations are normalized + frozen at registration:
  `movement` → `normalizeMovement(decl, SHARED_MOVEMENT)`, `aliases` →
  `normalizeAliases(decl, DEFAULT_ALIASES)`, `parts` →
  `normalizeParts(decl)` (absent = `EMPTY_PARTS`).
- Keep custom definitions in the pane's single ESM file (or its build
  output); stock registrations live at the bottom of `portrait-engine.mjs`
  (`neutral-pencil`, `angular-pencil`, `soft-flow-study`).

## 3. Movement declaration (Stage 1) — travel & safe regions

A `movement` object:

```js
{
  pxPerUnit: 0.0842,        // 48px compact = 48 / 570 crop
  minVisibleAt48: 0.5,     // px floor per 0.1 step inside the responsive band
  minVisibleCoordinated48: 1.0, // px floor for full coordinated travel
  responsiveBand: 0.1,     // |Δ| below which the saturator traverses linearly
  travel: {                 // |signal|=1 displacement, template viewBox units
    gazeX: 40, gazeY: 12, brow: 16, aperture: 0.4,
    mouthY: 24, mouthX: 24, head: 7, contour: 14, pressure: 0.4,
  },
  regions: {                // hard safe boxes (feature-relative)
    gaze:   { dxMin: -28, dxMax: 31, dyMin: -9, dyMax: 8 },
    brow:   { min: -16, max: 18 },
    aperture:{ min: -0.4, max: 0.44 },
    mouth:  { xMin: -24, xMax: 24, yMin: -20, yMax: 18 },
    head:   { min: -7, max: 7 },
    contour:{ min: -14, max: 14 },
    pupil:  { min: 267, max: 345, yMin: 352, yMax: 383 },   // post-transform
    farPupil:{ min: 88, max: 340, yMin: 352, yMax: 383 },  // effective coords
  },
}
```

`movementFor(model, templateId)` remaps the shared signal deltas into these
bands (high-gain saturator: `MOTION_GAIN` 4.0, `tanh(4·u)/tanh(4)`), clamps
every coordinate into its region, and returns a `model.motion`-shaped
object — legacy consumers keep working. `SHARED_MOVEMENT` reproduces the
pre-migration look exactly; `STUDIO_MOVEMENT` is the 48px-sized band set
(~4.2px coordinated full travel at the dock).

**Visibility is measured, not eyeballed:** `IYH.visibilityReport(templateId)`
deterministically sweeps 0.1 steps × intensity/confidence extremes.
Discipline rules (from the 48px-visibility suite):
1. Measure at FULL amplitude (intensity 1.0, confidence 1.0) — low-amplitude
   polls moving less is designed reliability, not a failure.
2. Gate per-step acceptance ONLY inside the template's `responsiveBand` —
   beyond it the saturator parks features at band ends by design; higher
   steps are reported, not gated.
3. The full-travel check compares 0→1 (never 1.0→1.0), plus the
   region-safety assert (both pupils inside their declared boxes, far eye in
   post-plane-transform effective coordinates `88 + 0.74·(x + Δ·0.58)`).

Pins: `tests/test_desktop_48px_visibility.mjs` (26 checks).

## 4. Alias compiler (Stage 2) — template-owned axis→feature graph

Groups combine the six raw axes into named signals; features are built from
groups **and/or raw axes**:

```js
{
  groups: {
    openness: { axes: { directness: 0.42, exploration: 0.38, scope: 0.20 },
                curve: 'linear', gain: 1, sensitivity: 1 },
  },
  features: {
    brow: { rest: 0, openness: 8.0, focus: -6.0 },   // value = rest + Σ src·w
    gazeX:{ rest: -28.0, exploration: 1.0 },          // raw axes allowed
  },
}
```

- `u = clamp(Σ wᵢ·devᵢ · sensitivity, −1, +1)`; `r = curve(u, gain)`.
  Curves: `linear` (legacy), `tanh` (normalized response, `gain` default 1),
  `step` (sign only — territory selection).
- Feature value = `rest + Σ (group-or-axis) × coefficient` in deviation
  units. A key that is a declared group uses the group value; otherwise the
  raw axis deviation (groups win; raw axes fall back).
- **Replace, never merge:** your declared nodes REPLACE the shared formula
  for that group/feature; undeclared keys keep the shared one. You may
  INVENT new groups/features (the output is the union).
- `DEFAULT_ALIASES` (exported) reproduces the legacy formulas byte-exactly —
  the pinned contract. `expressionFor(model, templateId)` returns the
  template-owned expression (stale shared values are overwritten, so they
  can't leak).

Pins: `tests/test_desktop_alias_compiler.mjs` (10 checks).

## 5. Head-movement parts (Stage 3) — coordinated transforms

A template declares NAMED PARTS that move as coordinated transforms (not
vertex nudges):

```js
{
  head: {
    rot: { headTilt: 1.0 },                       // channel-weight maps
    region: { x: 260, y: 250, w: 200, h: 200, clearance: 8 },
    envelope: { x: 0, y: 0, r: 7 },               // r = max rotation (deg)
    pivot: [360, 350],
  },
  jaw: {
    tx: { mouthTension: 0.5 }, ty: { mouthLift: 0.6 },
    region: { x: 300, y: 430, w: 120, h: 70, clearance: 6 },
    envelope: { x: 0.6, y: 0.7 },
    pivot: [360, 465],
    traverse: { period: 3.2, phase: 0.4, amplitude: 1.5,
                alias: { warmth: 0.5 } },          // alias-group spread bias
  },
  // … shoulders, hair_mass: each with its OWN period/phase — coordinated,
  // not synchronized.
}
```

Field semantics (as normalized):
- `tx`/`ty`/`rot`: weight maps over `movementFor` channels
  (`gazeDX, gazeDY, brow, eyeOpen, mouthLift, mouthTension, headTilt,
  contourDrift, pressure`).
- `region` + `clearance` + `envelope.{x,y}`: safe box in the template's own
  coordinate space; the envelope are FRACTIONS of the clearance-reduced
  half-extents (the hard clamp boundary). `r` bounds rotation.
- `pivot`: rotation center — explicit `[cx, cy]`, else the region center.
- `traverse.{period, phase, amplitude}` + `traverse.alias` (group→weight
  spread bias): the local idle rhythm.

Exact state at local clock time `t` (seconds, caller-supplied; the engine
owns no clock — restart returns to neutral):

```
dx = clamp( Σ w·m[ch]  +  A·( sin(2π·t/period + φ) − sin(φ) ),  halfW )
dy = clamp( Σ w·m[ch]  +  A·( sin(2π·t/period + φ + π/2) − sin(φ + π/2) ),  halfH )
rot = clamp( Σ w·m[ch],  r )
A = max(0, amplitude · (1 + Σ wᵢ·aliasGroupᵢ))     // spread bias, clamped ≥ 0
```

- The traversal term is `amp·(sin θ − sin φ)` — **exactly 0 at t=0 for any
  phase offset** (rest invariance). A bare `sin(θ+φ)` breaks it — never
  use it.
- y passes a π/2 lead (x/y a quarter period apart — Lissajous).
- `partStateFor(model, templateId, t)` → frozen `{ [partId]: {dx, dy, rot} }`
  (no parts declared → `{}` → legacy byte-identity).
- **Safety:** `partSafetyReport(templateId)` sweeps every axis at ±1 × the
  full traversal quarter-periods; fields are `pass` / `violations` /
  `samples` / `sample` (NOT `clean`). The clamp makes pass by construction;
  the audit is the regression check that the weights were declared sanely.

Pins: `tests/test_desktop_head_parts.mjs` (17 checks).

## 6. Renderers (Stage 4) — consuming part transforms

`partTransforms(model, templateId, t)` is the renderer-facing view of
`partStateFor`: per-part SVG `transform` attribute strings
(`translate(dx dy) rotate(r cx cy)` about the declared pivot). **A neutral
part yields the EMPTY STRING** — the renderer omits the attribute → rest
output is byte-identical to pre-part code.

Renderer conventions:
- Compute `const pt = IYH.partTransforms(model, options.template,
  Number(options.clock) || 0)` and apply `pt[partId]` to each part group.
- Group ids: `iyh-<templateId>-<part>` (NOT `iyyh-*`). The compact glyph and
  the studio builder share the contract: head group COMPOSES the head part
  AFTER the expression rotation; hair mass / hair front / jaw / shoulders /
  clothing carry their respective parts.
- The soft-flow legacy builder stays untouched (no part wrappers) — its
  group strings LOOK like the studio's; verify which function owns a match
  before patching.
- `clock` is caller-owned: 0 = channel motion only (the traversal is frozen
  at rest); advancing it animates the traversal.

Pins: `tests/test_desktop_template_renderers.mjs` (21 checks: legacy inert,
rest invariance, traversal-driven motion, clock sweep smoothness/determinism,
template divergence, no-part empty map, safety reports).

## 7. Tuning: pixel acceptance & calibration (Stage 5)

- **Pixel acceptance** (the tuner, `tools/portrait_motion_tuner.html`):
  rasterizes the 48px glyph + 512px studio offscreen and diffs adjacent
  deterministic clock steps `t = 0, 0.8, 1.6, 2.4` (production poll
  cadence). Verdict: **PASS needs ≥4 changed px @48 and ≥32 @512** per step;
  a zero profile whose mouth/tilt channels are zero moves ONLY via
  traversal terms — assert part motion at `t > 0`, never `t = 0`, for
  channel-neutral profiles.
- **Calibration = register a VARIANT, never mutate the stock template:**
  `IYH.portraitTemplateDefinition(templateId)` returns a deep structural
  clone `{id, label, description, render (ref), movement, aliases, parts}`;
  apply multipliers (travel ×k / part amplitude ×k) and
  `registerPortraitTemplate(definition, { replace: true })`. Variant ids
  must be `[a-z0-9_-]` (`safeTemplateId` rejects `:`). The stock template
  stays byte-frozen; the pane clock (§9) animates whichever is registered.
- Regenerate the tuner after every engine change (it pins the engine-block
  sha).

Pins: `tests/test_desktop_calibration.mjs` (13 checks: deep-clone isolation,
variant multipliers, deterministic re-registration, stock untouched, zeroed
parts → neutral, discoverability).

## 8. What the pane feeds the engine (live apply, commit `de557c8`)

The live 48px dock + studio panes call `buildPortraitSvg` with
`options.clock = iyhPortraitClockFor(rendering, data)` — a **poll-step
clock** owned by the pane:

- first data sighting of a rendering → phase **0.0** (rest invariance at
  pane open; zero persisted live state — every pane open/restart returns to
  neutral),
- each fresh `/portrait` poll result (data-identity guarded) → **+0.85 s**
  (one `PORTRAIT_POLL_MS` step; React re-renders on other state never
  advance it),
- compact and studio track independent clocks; fully deterministic (same
  poll sequence → same clock sequence → same frames).

Lane-d owns this glue (the `IYH-PORTCLOCK` block in `desktop/plugin.js`);
pins: `tests/test_desktop_portrait_clock.mjs` (9 checks).

## 9. Gates for a NEW template (all must be green)

1. Engine suite: every `node tests/test_desktop_*.mjs` (glob it — the set
   grows; currently 16 suites) — add your template's own checks: well-formed
   SVG for both renderings, rest invariance (`t=0`/`clock=0` → neutral
   output byte-identical to the no-part legacy), traversal motion at `t>0`,
   determinism (same input → same bytes), `partSafetyReport(...).pass ===
   true`, and a `visibilityReport` gate inside your `responsiveBand`.
2. **Whole-file in-order load test:** the renderer loads `plugin.js` as a
   Blob-URL ESM executed strictly in source order (the import rewriter
   allows ONLY `@hermes/plugin-sdk` + `react`). Copy the pane file to a temp
   `.mjs`, replace the three import lines with a local stub module
   (exporting `useState, useRef, useEffect, host, useQuery, queryClient,
   Select*, jsx, jsxs`), run `node` — clean exit = the renderer's import
   settles. (Block-extraction tests alone can't see a load-time TDZ.)
3. C7 ownership: the new test file is `tests/test_desktop_*.mjs` (lane-d
   glob); any new root doc/tool registers into the `parent` set in
   `tools/check_contracts.py` in the SAME commit (`python
   tools/check_contracts.py` must print CLEAN).
4. Rebuild + regenerate: `node tools/build-inline-engine.mjs` → `py -3.12
   tools/build_motion_tuner.py` (block sha printed; verify the tuner's
   sha matches the build-inline slice — they differ by design, both must be
   fresh).
5. Full python suite: `py -3.12 -m pytest -q` (~434) — JS-only changes
   still run it; last green count is the one to trust.
6. Sync the pane: copy repo `desktop/plugin.js` →
   `%LOCALAPPDATA%\hermes\desktop-plugins\its-in-your-head\plugin.js`
   (plain file copy, `desktop/` level dropped) and `git pull` the profile
   clone (`profiles\plugin-studio\plugins\its-in-your-head`). The watcher
   fires on CONTENT change (not mtime touch); load successes log NOTHING —
   only failures print. If the pane unmounted at an error boundary, close
   and reopen it (or Ctrl+K → Reload desktop plugins).

## 10. Invariants checklist (owner-locked; re-apply, don't re-derive)

- **Determinism:** no `Math.random`; same input → same bytes. The gaze
  saccade model is FNV-1a-keyed (quantized signature), heavy-tailed landings
  `pow(u, 2.2)` — big moments come from target re-rolls, not wide constant
  travel.
- **Zero persisted live state:** transient state lives in engine/pane
  memory only; restart returns to neutral (the pane clock resets at pane
  open).
- **Rest invariance:** t=0 (clock 0) → parts exactly neutral; traversal
  terms MUST be `amp·(sin θ − sin φ)`.
- **Template sovereignty:** each template authors its own visual language
  (movement/aliases/parts/renderer) — not a shared-vocabulary consumer.
- **48px visibility:** movements must be visible at the real 48px dock,
  verified by `visibilityReport` + tuner pixel acceptance — not the studio
  pane.
- **No new perception machinery:** controls derive ONLY from existing
  pattern-affinity numbers (the six axes + confidence metadata).
- **Stage-by-stage migration safety:** every stage kept previous
  interfaces; any single stage alone still works (a no-parts template
  renders byte-identically to the legacy path).

## 11. Minimal complete template skeleton

```js
const MY_PARTS = Object.freeze({
  head: { rot: { headTilt: 1.0 },
          region: { x: 260, y: 250, w: 200, h: 200, clearance: 8 },
          envelope: { x: 0, y: 0, r: 6 }, pivot: [360, 350] },
  jaw:  { tx: { mouthTension: 0.5 },
          region: { x: 300, y: 430, w: 120, h: 70, clearance: 6 },
          envelope: { x: 0.5, y: 0.6 }, pivot: [360, 465],
          traverse: { period: 3.6, phase: 1.1, amplitude: 1.2,
                      alias: { warmth: 0.4 } } },
});

IYH_PORTRAIT.registerPortraitTemplate({
  id: 'my-style', label: 'My style',
  description: 'Short picker copy.',
  render: (model, options) => {
    const e = IYH_PORTRAIT.expressionFor(model, options.template);
    const pt = IYH_PORTRAIT.partTransforms(model, options.template,
                                            Number(options.clock) || 0);
    // …your construction: groups with id="iyh-my-style-<part>",
    // transform = pt.<part> (omit when empty), strokes in graphite,
    // accent only on extension/interpretation layers.
    return `<svg …>…</svg>`;
  },
  movement: IYH_PORTRAIT.STUDIO_MOVEMENT,   // or your own bands + regions
  aliases:  undefined,                        // DEFAULT (legacy formulas)
  parts:    MY_PARTS,
});
```

Then run the §9 gates, commit stage-by-stage, push, sync the pane copy.
Done: your template is a first-class author in the portrait system —
visible at 48px, deterministic, rest-invariant, safety-audited, pixel-
accepted, live in the dock.
