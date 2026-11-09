// tests/test_desktop_head_parts.mjs — node test for the Stage 3
// head-movement parts (template-declared, coordinated transforms):
//   - legacy templates (no parts) get an EMPTY part layer
//   - rest invariance: at local clock t = 0 every part state is
//     exactly neutral (process restart ⇒ neutral)
//   - deterministic: same model + t → same state
//   - coordinated transforms: one channel drives several parts at
//     their own weights
//   - region × envelope containment (safety audit across axis
//     extremes and the full traversal period)
//   - alias-group spread bias (Stage 2 → Stage 3 wiring): the
//     template's own alias graph scales the traversal cloud
// Run: node tests/test_desktop_head_parts.mjs
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

const NEUTRAL = IYH.buildPortraitModel({ available: true, user: {}, confidence: {} })

// 1 — legacy template: no parts declared → EMPTY part layer
const legacy = IYH.partStateFor(NEUTRAL, 'soft-flow-study')
ok(Object.keys(legacy).length === 0, 'soft-flow-study declares no parts → empty part layer (legacy byte-identity)')

// 2 — rest invariance: t = 0 is exactly neutral for every declared part
for (const tid of ['neutral-pencil', 'angular-pencil']) {
  const at0 = IYH.partStateFor(NEUTRAL, tid, 0)
  const neutralParts = Object.entries(at0).every(([, st]) => st.dx === 0 && st.dy === 0 && st.rot === 0)
  ok(neutralParts, tid + ': t=0 part states are exactly neutral (' + Object.keys(at0).length + ' parts)')
}

// 3 — determinism
const a = IYH.partStateFor(NEUTRAL, 'neutral-pencil', 2.5)
const b = IYH.partStateFor(IYH.buildPortraitModel({ available: true, user: {}, confidence: {} }), 'neutral-pencil', 2.5)
ok(JSON.stringify(a) === JSON.stringify(b), 'part states are deterministic (same model + t → same state)')

// 4 — coordinated transforms: build a model with strong motion and
//    check each part against its declared channel weights.
const hot = IYH.buildPortraitModel({
  available: true,
  user: { directness: 1.0, context_richness: 0.2, exploration: 1.0, analytical_depth: 0.8, iteration: 0.3, scope: 1.0 },
  confidence: { directness: 0.9, context_richness: 0.9, exploration: 0.9, analytical_depth: 0.9, iteration: 0.9, scope: 0.9 },
  live_expression: { stage: 'prompt', intensity: 1.0, confidence: 1.0, openness: 1.0, focus: 1.0 },
})
for (const tid of ['neutral-pencil', 'angular-pencil']) {
  const st = IYH.partStateFor(hot, tid, 0) // t=0: traversal terms are 0 — channel sums only
  const mv = IYH.movementFor(hot, tid)
  const head = st.head
  ok(Math.abs(head.rot - Math.max(-7, Math.min(7, mv.headTilt))) < 1e-9,
    tid + ': head part rotates 1:1 with the headTilt channel (' + head.rot.toFixed(3) + '°)')
  const sh = st.shoulders
  ok(Math.abs(sh.rot - Math.max(-3, Math.min(3, mv.headTilt * (tid === 'neutral-pencil' ? 0.3 : 0.25)))) < 1e-9,
    tid + ': shoulders counter-tilt at a fraction of headTilt (' + sh.rot.toFixed(3) + '°)')
  const jaw = st.jaw
  const jawX = mv.mouthTension * (tid === 'neutral-pencil' ? 0.5 : 0.8)
  ok(Math.abs(jaw.dx - jawX) < 1e-9,
    tid + ': jaw x follows mouthTension × weight (' + jaw.dx.toFixed(3) + ' = ' + jawX.toFixed(3) + ')')
  // one channel (headTilt) drives three parts — coordination, not a
  // rigid object:
  ok(Math.abs(sh.rot) > 0.01 && Math.abs(st.hair_mass.rot) > 0.01,
    tid + ': headTilt simultaneously drives head + shoulders + hair (' + head.rot.toFixed(2) + '/' + sh.rot.toFixed(2) + '/' + st.hair_mass.rot.toFixed(2) + '°)')
}

// 5 — region × envelope containment: the safety audit across axis
//    extremes and the full traversal period must pass for every
//    template that declares parts.
for (const tid of ['neutral-pencil', 'angular-pencil', 'soft-flow-study']) {
  const rep = IYH.partSafetyReport(tid)
  ok(rep.pass, tid + ': part safety audit passes (' + rep.samples + ' samples, ' + rep.violations + ' violations)' + (rep.note ? ' — ' + rep.note : ''))
}

// 6 — alias-group spread bias (Stage 2 → 3): a hot warmth/focus model
//    widens the neutral jaw cloud relative to the neutral model.
const stHot = IYH.partStateFor(hot, 'neutral-pencil', 1.6) // mid-period
const stCalm = IYH.partStateFor(NEUTRAL, 'neutral-pencil', 1.6)
const jawTravel = Math.abs(stHot.jaw.dx) + Math.abs(stHot.jaw.dy)
const jawCalm = Math.abs(stCalm.jaw.dx) + Math.abs(stCalm.jaw.dy)
ok(jawTravel > jawCalm,
  'alias spread: warm/focused model widens the jaw cloud (' + jawTravel.toFixed(3) + ' > ' + jawCalm.toFixed(3) + ' @ t=1.6)')

// 7 — traversal smoothness: adjacent samples move smoothly (no
//    snapping — bounded per-step delta across a full period).
let prev = IYH.partStateFor(NEUTRAL, 'neutral-pencil', 0)
let maxStep = 0
for (let i = 1; i <= 24; i++) {
  const cur = IYH.partStateFor(NEUTRAL, 'neutral-pencil', i * 0.25)
  for (const pid of Object.keys(cur)) {
    const step = Math.abs(cur[pid].dx - prev[pid].dx) + Math.abs(cur[pid].dy - prev[pid].dy) + Math.abs(cur[pid].rot - prev[pid].rot)
    if (step > maxStep) maxStep = step
  }
  prev = cur
}
ok(maxStep < 1.5, 'traversal is smooth: max per-0.25s step = ' + maxStep.toFixed(3) + ' units (< 1.5, no snapping)')

console.log('\n' + n + ' checks passed — head-movement parts')
