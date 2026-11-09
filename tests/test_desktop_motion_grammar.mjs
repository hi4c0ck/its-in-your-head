// tests/test_desktop_motion_grammar.mjs — node test for the IYH portrait
// motion-grammar layer (Live Expression & Pattern Mutations plan §4/§6):
//   - a neutral rest renders zero motion deltas
//   - live expression deltas are visible, bounded, and high-gain
//   - morphology reshapes treatment (clamped, floor-gated, bounded)
//   - all three templates receive the SAME derived model and react
//   - identical inputs are byte-deterministic (no random jitter)
//   - PORTRAIT polls on its own ~0.85s cadence; PULSE/DRIFT stay slow
// Run: node tests/test_desktop_motion_grammar.mjs
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

const base = {
  available: true,
  dimensions: all(0.5),
  user: { directness: 0.75, context_richness: 0.6, exploration: 0.4,
          analytical_depth: 0.55, iteration: 0.3, scope: 0.65 },
  agent: null,
  confidence: all(0.6),
}
const live = (over = {}) => Object.freeze({
  stage: 'paired', intensity: 0.8, confidence: 0.8,
  openness: 0, focus: 0, resonance: 0.5, tension: 0.5,
  updated_at: 1, ...over,
})
const morpho = (over = {}) => Object.freeze({
  available: true, pair: ['context_richness', 'analytical_depth'],
  strength: 0.3, confidence: 0.6, evidence: 12,
  line_density: 0.3, contour_asymmetry: 0.1,
  echo_strength: 0.2, gesture_bias: 0.1, ...over,
})
const withLive = (over) => ({ ...base, live_expression: live(over) })
const withMorpho = (over) => ({ ...base, morphology: morpho(over) })

// (1) neutral rest: every motion delta is exactly its rest value
{
  const m = IYH.buildPortraitModel(base).motion
  for (const k of ['gazeDX', 'gazeDY', 'gazeTerritory', 'gazeCenter', 'headTilt', 'brow', 'eyeOpen',
                   'mouthLift', 'mouthTension', 'contourDrift', 'tensionBreak']) {
    assert.equal(m[k], 0, 'neutral ' + k)
  }
  assert.equal(m.continuity, 1, 'neutral continuity = 1')
  assert.equal(m.pressure, 1, 'neutral pressure = 1 (native stroke weights)')
  assert.equal(m.echoBoost, 0, 'neutral echo boost = 0')
  ok(true, 'neutral rest -> all motion deltas at their rest values')
}

// (2) live exploratory expression: visible, bounded deltas
{
  const m = IYH.buildPortraitModel(withLive({ stage: 'prompt', intensity: 0.7, confidence: 0.3, openness: 0.9 })).motion
  assert.ok(m.headTilt > 0, 'openness tilts the head')
  assert.ok(m.mouthLift > 0, 'openness lifts the mouth region')
  assert.ok(Number.isFinite(m.gazeDX) && Math.abs(m.gazeDX) <= 24, 'gaze saccade stays inside the eye region')
  ok(true, 'live exploratory expression moves head/mouth; gaze saccade is bounded')
}

// (2b) gaze territory model: saccades, not linear tracking
{
  const mag = (v) => Math.abs(v)
  const sweep = (over, n) => {
    const out = []
    for (let i = 0; i < n; i++) {
      out.push(IYH.buildPortraitModel(withLive({ ...over, updated_at: 100 + i })).motion.gazeDX)
    }
    return out
  }
  // Exploratory breadth WIDENS the territory; focus PINS it tight.
  const openVals = sweep({ stage: 'prompt', intensity: 1, confidence: 1, openness: 0.95 }, 200)
  const focusVals = sweep({ stage: 'prompt', intensity: 1, confidence: 1, focus: 0.95 }, 200)
  assert.ok(Math.max(...openVals.map(mag)) > Math.max(...focusVals.map(mag)),
    'exploratory territory is wider than focused territory')
  assert.ok(Math.max(...openVals.map(mag)) >= 15, 'exploratory saccades reach the 15px moment')
  const mean = openVals.reduce((s, v) => s + mag(v), 0) / 200
  assert.ok(mean < 12, 'average saccade stays modest (moments, not constant wide travel)')
  // A 0.1-level signal change re-rolls the landing (deterministic per signature).
  const a = IYH.buildPortraitModel(withLive({ stage: 'prompt', intensity: 0.7, confidence: 0.3, openness: 0.9, updated_at: 1 })).motion.gazeDX
  const b = IYH.buildPortraitModel(withLive({ stage: 'prompt', intensity: 0.7, confidence: 0.3, openness: 0.8, updated_at: 1 })).motion.gazeDX
  assert.notEqual(a, b, 'a 0.1 signal change re-rolls the saccade landing')
  ok(true, 'gaze: territory-wide deterministic saccades (moments of 15px, modest average)')
}

// (3) live focused expression: brow, aperture, mouth tension
{
  const m = IYH.buildPortraitModel(withLive({ focus: 0.9, openness: 0 })).motion
  assert.ok(m.brow > 0 && m.eyeOpen !== 0, 'focus sharpens brow + aperture')
  assert.ok(m.mouthTension > 0, 'focus adds controlled mouth tension')
  ok(true, 'focus drives brow/aperture/mouth-tension features')
}

// (4) resonance vs tension: continuity + pressure
{
  const calm = IYH.buildPortraitModel(withLive({ resonance: 0.95, tension: 0.05 })).motion
  const tense = IYH.buildPortraitModel(withLive({ resonance: 0.05, tension: 0.95 })).motion
  assert.ok(calm.continuity > tense.continuity, 'high resonance keeps secondary contours continuous')
  assert.ok(tense.pressure > 1, 'high tension raises stroke pressure (broken-stroke side)')
  ok(true, 'resonance/tension modulate continuity and pressure in opposite directions')
}

// (5) morphology: floor-gated, clamped, bounded treatment deltas
{
  const off = IYH.buildPortraitModel(withMorpho({ strength: 0.01 })).morphology
  assert.equal(off.available, false, 'strength below the 0.02 floor is inert')
  const on = IYH.buildPortraitModel(withMorpho()).morphology
  assert.ok(on.available && on.pair.length === 2, 'strength above the floor is active')
  const clamped = IYH.buildPortraitModel(withMorpho({ line_density: 5.0, gesture_bias: -7 })).motion
  assert.ok(Number.isFinite(clamped.lineDensity) && clamped.lineDensity <= 1, 'out-of-range morphology is clamped')
  ok(true, 'morphology is floor-gated and clamped (never unbounded)')
}

// (6) all three templates react to the SAME model (plan §6)
{
  const ids = IYH.listPortraitTemplates().map((t) => t.id)
  assert.equal(ids.length, 3, 'three templates')
  const payload = withLive({ openness: 0.8, focus: 0.2 })
  const neutralSvgs = {}
  const liveSvgs = {}
  for (const id of ids) {
    neutralSvgs[id] = IYH.buildPortraitSvg(base, { template: id, rendering: 'compact' })
    liveSvgs[id] = IYH.buildPortraitSvg(payload, { template: id, rendering: 'compact' })
    assert.ok(liveSvgs[id].startsWith('<svg') && liveSvgs[id].endsWith('</svg>'), id + ' live svg well-formed')
    assert.notEqual(liveSvgs[id], neutralSvgs[id], id + ' visibly reacts to the live expression')
  }
  ok(true, 'all three templates visibly react to the same live expression')
}

// (7) determinism: identical inputs -> byte-identical output, no jitter
{
  const payload = withLive({ openness: 0.7, focus: 0.4, resonance: 0.8, tension: 0.2, intensity: 0.75, confidence: 0.7 })
  const withM = { ...payload, morphology: morpho() }
  for (const id of IYH.listPortraitTemplates().map((t) => t.id)) {
    for (const r of ['compact', 'studio']) {
      const a = IYH.buildPortraitSvg(withM, { template: id, rendering: r })
      const b = IYH.buildPortraitSvg(withM, { template: id, rendering: r })
      assert.equal(a, b, id + '/' + r + ' byte-deterministic')
    }
  }
  ok(true, 'identical state + morphology is byte-identical across all templates/renderings')
}

// (8) extreme live input stays finite and bounded (no blowup)
{
  const extreme = withLive({ openness: 1, focus: 1, resonance: 0, tension: 1, intensity: 1, confidence: 1 })
  const m = IYH.buildPortraitModel({ ...extreme, morphology: morpho({ line_density: 1, echo_strength: 1, gesture_bias: 1, contour_asymmetry: 1 }) }).motion
  for (const [k, v] of Object.entries(m)) {
    if (typeof v !== 'number') continue  // active (bool) / stage (string)
    assert.ok(Number.isFinite(v), 'extreme input stays finite: ' + k + '=' + v)
  }
  assert.ok(Math.abs(m.gazeDX) <= 24, 'extreme saccade stays inside the eye region')
  ok(true, 'extreme live + full morphology input stays finite and bounded')
}

// (9) buildPortraitModel exposes the derived layers for templates/tests
{
  const model = IYH.buildPortraitModel(withLive({ openness: 0.5 }))
  assert.ok(model.motion && model.live, 'model carries motion + live layers')
  assert.equal(model.rawLive.stage, 'paired', 'raw server payload is exposed')
  ok(true, 'buildPortraitModel exposes live/motion/raw layers')
}

// (10) portrait-only poll cadence: PORTRAIT ~0.85s, PULSE/DRIFT stay slow
{
  const m = src.match(/const PORTRAIT_POLL_MS = (\d+)/)
  assert.ok(m, 'PORTRAIT_POLL_MS is defined')
  assert.ok(Number(m[1]) >= 750 && Number(m[1]) <= 1000, 'portrait cadence in the 750-1000ms window')
  assert.ok(src.match(/refetchInterval: PORTRAIT_POLL_MS/g).length >= 2, 'both portrait observers use it')
  assert.ok(src.includes('const POLL_MS = 5000'), 'PULSE/DRIFT keep the slow POLL_MS')
  ok(true, 'PORTRAIT polls on its own ' + m[1] + 'ms cadence (PULSE/DRIFT unchanged)')
}

console.log('\n1..10 all motion-grammar checks passed (' + n + ' ok)')
