// tests/test_desktop_48px_visibility.mjs — node test for the template-owned,
// feature-relative movement contract (Portrait Motion System, Stage 1):
//   - templates declare travel bands + safe regions; movementFor remaps
//     the shared signals into them and clamps geometry inside the regions
//   - rest invariance: a resting poll produces zero deltas for every template
//   - 48px visibility: a 0.1 control step in the responsive band clears
//     the template's minVisible thresholds at the compact 48px scale
//   - full-travel check and pupil region safety across the whole sweep
//   - legacy template (soft-flow) keeps its pre-migration look: its
//     movement declaration is the shared travel, so its deltas equal
//     model.motion's exactly (migration did not change it)
//   - byte-determinism of the compact renderings
// Run: node tests/test_desktop_48px_visibility.mjs
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

const dims = IYH.PORTRAIT_DIMENSIONS
const all = (v) => Object.fromEntries(dims.map((d) => [d, v]))
const base = (openness, focus) => ({
  available: true,
  user: all(0.5),
  confidence: all(1),
  live_expression: {
    stage: 'paired', openness, focus,
    intensity: 1.0, confidence: 1.0, resonance: 0.5, tension: 0.5,
    updated_at: 7,
  },
})

const TEMPLATES = IYH.listPortraitTemplates().map((t) => t.id)
ok(TEMPLATES.length === 3, 'three templates registered: ' + TEMPLATES.join(', '))

// 1 — rest invariance: zero deltas for every template.
for (const tid of TEMPLATES) {
  const m = IYH.movementFor(IYH.buildPortraitModel({}), tid)
  const zero = ['gazeDX', 'gazeDY', 'brow', 'eyeOpen', 'mouthLift', 'mouthTension', 'headTilt', 'contourDrift']
    .every((k) => m[k] === 0)
  ok(zero && m.pressure === 1 && m.continuity === 1, tid + ': rest invariance (zero deltas, neutral pressure)')
}

// 2 — every registered template carries a movement declaration.
for (const tid of TEMPLATES) {
  const r = IYH.visibilityReport(tid)
  ok(typeof r.responsiveBand === 'number' && Number.isFinite(r.pxPerUnit), tid + ': movement declaration present (' + r.responsiveBand + ' band, ' + r.pxPerUnit.toFixed(4) + ' px/unit)')
}

// 3 — 48px visibility: the responsive-band steps + full travel pass on
//    the templates that commit to it (minVisibleAt48 > 0).
for (const tid of TEMPLATES) {
  const r = IYH.visibilityReport(tid)
  if (r.minVisibleAt48 === 0) {
    ok(r.fullTravel.coordinatedPx >= 0, tid + ': legacy template — no 48px commitment (full travel ' + r.fullTravel.coordinatedPx.toFixed(2) + 'px, reported only)')
    continue
  }
  ok(r.pass, tid + ': 48px visibility contract passes (gated steps: ' + r.steps.filter((s) => s.gated).map((s) => s.coordinatedPx.toFixed(2) + 'px').join(' / ') + ', full travel ' + r.fullTravel.coordinatedPx.toFixed(2) + 'px)')
}

// 4 — region safety: every pupil position in the sweep stays inside the
//    declared boxes (the generated geometry never leaves its region).
for (const tid of TEMPLATES) {
  const r = IYH.visibilityReport(tid)
  ok(r.steps.every((s) => s.safe), tid + ': all ' + r.steps.length + ' sweep positions stay inside the pupil regions')
}

// 5 — legacy migration safety: soft-flow's declared movement is the
//    shared travel, so movementFor === model.motion's shared numbers.
const legacy = IYH.buildPortraitModel(base(0.6, 0.4))
const shared = IYH.movementFor(legacy, 'soft-flow-study')
const direct = legacy.motion
for (const k of ['gazeDX', 'gazeDY', 'brow', 'eyeOpen', 'mouthLift', 'mouthTension', 'headTilt', 'contourDrift', 'pressure']) {
  ok(Math.abs(shared[k] - direct[k]) < 1e-12, 'soft-flow: movementFor.' + k + ' === model.motion.' + k + ' (' + shared[k].toFixed(3) + ')')
}

// 6 — the studio templates actually widen: at the same controls their
//    deltas exceed the shared travel's deltas (the widening contract).
const studio = IYH.movementFor(legacy, 'neutral-pencil')
ok(studio.brow > direct.brow && studio.mouthLift > direct.mouthLift, 'neutral-pencil widens travel vs shared (brow ' + studio.brow.toFixed(1) + ' vs ' + direct.brow.toFixed(1) + ', mouthLift ' + studio.mouthLift.toFixed(1) + ' vs ' + direct.mouthLift.toFixed(1) + ')')

// 7 — determinism: identical inputs, identical compact SVGs.
const a = IYH.buildPortraitSvg(base(0.7, 0.3), { template: 'neutral-pencil', rendering: 'compact' })
const b = IYH.buildPortraitSvg(base(0.7, 0.3), { template: 'neutral-pencil', rendering: 'compact' })
ok(a === b, 'compact rendering is byte-deterministic (' + a.length + ' bytes)')

// 8 — the normalized axes layer: a_i = 2v − 1, confidence as metadata.
const axes = IYH.buildPortraitModel({ available: true, user: { directness: 0.42 }, confidence: { directness: 0.73 } }).axes
ok(Math.abs(axes.directness.value - (-0.16)) < 1e-9 && axes.directness.confidence > 0 && axes.directness.source === 'observed', 'axes: directness 0.42 → ' + axes.directness.value.toFixed(2) + ' with metadata confidence ' + axes.directness.confidence.toFixed(2))
ok(axes.scope.source === 'absent' && axes.scope.value === 0, 'axes: absent input rests at 0 with source "absent" (no fabricated signal)')

console.log('\n' + n + ' checks passed — 48px visibility + movement contract')
