// tests/test_desktop_template_renderers.mjs — node test for the Stage 4
// template renderers: the studio and compact builders CONSUME the
// template's part states (partTransforms) —
//   - legacy (no-part) template output stays inert (no part transforms)
//   - neutral model at t=0 carries no part transform (rest invariance)
//   - hot model: jaw / shoulders / hair groups carry their transforms
//   - the clock sweep is smooth, periodic and deterministic
//   - each template authors its own part language (angular diverges)
// Run: node tests/test_desktop_template_renderers.mjs
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'
import assert from 'node:assert/strict'

const dir = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(dir, '..', 'desktop', 'plugin.js'), 'utf8')

function block(start, end) {
  const s = src.indexOf(start)
  const e = src.indexOf(end)
  assert.ok(s >= 0 && e > s, start + ' found in plugin.js')
  return src.slice(s + start.length, e)
}
const context = vm.createContext({
  useState: () => [null, () => {}],
  useEffect: () => {},
  jsx: () => null,
  jsxs: () => null,
  clamp01: (v) => (typeof v === 'number' && Number.isFinite(v) ? Math.min(1, Math.max(0, v)) : null),
})
vm.runInContext(block('// IYH-PORTRAIT-ENGINE-START', '// IYH-PORTRAIT-ENGINE-END') +
  '\nglobalThis.__IYH_PORTRAIT__ = IYH_PORTRAIT;', context)
const IYH = context.__IYH_PORTRAIT__

let n = 0
const ok = (cond, label) => { assert.ok(cond, label); console.log('ok ' + (++n) + ' - ' + label) }

const HOT = {
  kind: 'its-in-your-head-portrait-live-controls',
  sentence: 'steady',
  state: 'focused',
  patternAffinity: {
    openness: 0.75, analytical_depth: 0.75, complexity: 0.2,
    playfulness: 0.1, warmth: 0.5, energy: 0.9,
  },
}
const NEUTRAL = { kind: 'its-in-your-head-portrait', sentence: 'quiet', state: 'idle', patternAffinity: {} }

const glyph = (template, data, clock = 0) => IYH.buildPortraitSvg(data, { template, rendering: 'compact', clock, seed: 7 })
const studio = (template, data, clock = 0) => IYH.buildPortraitSvg(data, { template, clock, seed: 7 })

// 1. Legacy template (soft-flow-study, no parts): the builder is
//    untouched by Stage 4 — no part wrappers, no part transforms.
ok(!studio('soft-flow-study', NEUTRAL, 2.5).includes('data-part='),
  'legacy template: renderer inert (no part wrappers / transforms)')

// 2. Stock compact renderer: neutral model at t=0 — rest invariance.
//    (Pre-existing non-part transforms, e.g. the head's static
//    translate(7 0) or the far-eye plane, are NOT part transforms —
//    the check targets the part groups specifically.)
const rest = glyph('neutral-pencil', NEUTRAL, 0)
ok(rest.includes('data-part="jaw"'), 'stock compact: jaw part owned by the renderer (wrapper present)')
ok(!/data-part="jaw" transform=/.test(rest), 'stock compact: no jaw transform at rest')
ok(!/<g id="iyh-neutral-pencil-hair"[^>]* transform=/.test(rest), 'stock compact: no hair transform at rest')
ok(!/<g id="iyh-neutral-pencil-shoulder-gesture"[^>]* transform=/.test(rest), 'stock compact: no shoulder transform at rest')
ok(!/<g id="iyh-neutral-pencil-head" transform="[^"]*rotate\([^)]*\) (translate|rotate)/.test(rest), 'stock compact: no head part transform at rest')

// 3. Hot model: this profile's mouth/tilt channels are zero, so at t=0
//    every part is exactly at rest — the motion arrives via the
//    template's own traversal terms (warmth bias 0.5 → jaw drift).
const hot0 = glyph('neutral-pencil', HOT, 0)
ok(!/data-part="jaw" transform=/.test(hot0), 'stock compact: hot model, channel-neutral profile → parts at rest at t=0')
const hot2 = glyph('neutral-pencil', HOT, 2)
ok(/data-part="jaw" transform="translate\(/.test(hot2), 'stock compact: traversal carries the jaw by t=2 (warmth bias)')
ok(/id="iyh-neutral-pencil-hair"[^>]* transform="translate\(/.test(hot2), 'stock compact: hair mass drifts by t=2 (analytical bias)')
ok(/id="iyh-neutral-pencil-shoulder-gesture"[^>]* transform=/.test(glyph('neutral-pencil', HOT, 3.5)), 'stock compact: shoulders counter-tilt by t=3.5')

// 4. Clock sweep: smooth and deterministic (per-part period closure
//    is asserted in the head-parts suite; the full SVG composes
//    several different periods, so it does NOT close at one period).
const a = glyph('neutral-pencil', HOT, 0)
const b = glyph('neutral-pencil', HOT, 1.7)
const c = glyph('neutral-pencil', HOT, 1.7)
const b2 = glyph('neutral-pencil', HOT, 1.9)
ok(a !== b, 'clock sweep: part states move with the clock')
ok(b === c, 'clock sweep: deterministic at a given clock')
ok(b !== b2, 'clock sweep: consecutive clocks differ (no stutter / quantization)')

// 5. Studio renderer: same part contract as the compact renderer.
const sRest = studio('neutral-pencil', NEUTRAL, 0)
ok(!/data-part="jaw" transform=/.test(sRest), 'stock studio: jaw inert at rest')
const sHot = studio('neutral-pencil', HOT, 0.9)
ok(/data-part="jaw" transform="translate\(/.test(sHot), 'stock studio: hot model → jaw translation')
ok(sHot.includes('data-part="jaw"'), 'stock studio: jaw part owned by the renderer')

// 6. Angular template: its own part weights/periods → its own motion.
ok(glyph('angular-pencil', HOT, 1.1) !== glyph('neutral-pencil', HOT, 1.1),
  'angular vs neutral: templates author their own part language')
const aRest = glyph('angular-pencil', NEUTRAL, 0)
ok(!/data-part="jaw" transform=/.test(aRest) && !/<g id="iyh-angular-pencil-hair"[^>]* transform=/.test(aRest),
  'angular: rest invariance at t=0 (neutral model)')

// 7. No-part custom template: partTransforms is the empty map — the
//    renderer (whatever custom code the template ships) gets nothing.
IYH.registerPortraitTemplate({
  id: 'stage4-no-parts',
  name: 'No Parts',
  description: 'a stage-4 template that owns no parts',
  render: (model, options) => '<svg data-test="static"></svg>',
})
const ptEmpty = IYH.partTransforms(IYH.buildPortraitModel(NEUTRAL), 'stage4-no-parts', 1.5)
ok(Object.keys(ptEmpty).length === 0, 'no-part template: empty part-transform map')

// 8. Part safety report is clean for both stock templates (regions ×
//    envelopes contain every extreme, including the traversal).
for (const tid of ['neutral-pencil', 'angular-pencil']) {
  const rep = IYH.partSafetyReport(tid)
  ok(rep.pass === true && rep.violations === 0, tid + ': partSafetyReport clean (' + rep.samples + ' samples, 0 violations)')
}

console.log('1..8')
console.log('# template renderer stage: all checks passed')
