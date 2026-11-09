// tests/test_desktop_calibration.mjs — node test for the Stage 5
// calibration accessor + variant registration:
//   - portraitTemplateDefinition returns a deep structural clone
//     (mutations never leak into the stock template)
//   - a calibrated variant registered from the definition moves the
//     parts/travel by the multiplier, deterministically
//   - the stock template is untouched after variant registration
// Run: node tests/test_desktop_calibration.mjs
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
  structuredClone,
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
const model = IYH.buildPortraitModel(HOT)
const T = 2.0 // a non-zero clock so the traversal terms are live

// 1. The accessor returns a structural clone of the stock data.
const def = IYH.portraitTemplateDefinition('neutral-pencil')
ok(def.id === 'neutral-pencil' && def.label === IYH.listPortraitTemplates()
  .find((t) => t.id === 'neutral-pencil').label, 'accessor: id + label match the stock template')
ok(!!def.render && def.render.name.length > 0, 'accessor: the renderer reference is the template author')
ok(def.movement && def.movement.travel && typeof def.movement.travel.gazeX === 'number',
  'accessor: movement data is present (travel bands)')
ok(def.parts && Object.keys(def.parts).length === 4, 'accessor: all four stock parts present')
ok(def.aliases && def.aliases.features, 'accessor: alias graph data is present')

// 2. Mutations to the clone never leak into the stock template.
def.movement.travel.gazeX = -999
def.parts.jaw.traverse.amplitude = -999
const def2 = IYH.portraitTemplateDefinition('neutral-pencil')
ok(def2.movement.travel.gazeX !== -999, 'clone: travel mutation does not leak into the stock template')
ok(def2.parts.jaw.traverse.amplitude !== -999, 'clone: part mutation does not leak into the stock template')

// 3. A calibrated variant (travel ×2, parts ×0) registers and
//    changes exactly what it should: double feature travel, zero
//    part motion. Deterministic, stock untouched.
IYH.registerPortraitTemplate({
  id: 'cal-test',
  label: 'cal-test',
  description: 'calibrated test variant',
  render: def2.render,
  movement: { ...def2.movement, travel: { ...def2.movement.travel, gazeX: def2.movement.travel.gazeX * 2 } },
  aliases: def2.aliases,
  parts: Object.fromEntries(Object.entries(def2.parts).map(([pid, p]) => [pid, {
    ...p,
    tx: Object.fromEntries(Object.entries(p.tx).map(([k, w]) => [k, 0])),
    ty: Object.fromEntries(Object.entries(p.ty).map(([k, w]) => [k, 0])),
    rot: Object.fromEntries(Object.entries(p.rot).map(([k, w]) => [k, 0])),
    traverse: { ...p.traverse, amplitude: 0 },
  }])),
}, { replace: true })

const mStock = IYH.movementFor(model, 'neutral-pencil')
const mCal = IYH.movementFor(model, 'cal-test')
ok(Math.abs(mCal.gazeDX - 2 * mStock.gazeDX) < 1e-9 || (mCal.gazeDX === 0 && mStock.gazeDX === 0),
  'variant: doubled gaze travel (within region clamps)')
const pStock = IYH.partStateFor(model, 'neutral-pencil', T)
const pCal = IYH.partStateFor(model, 'cal-test', T)
ok(Object.values(pCal).every((s) => s.dx === 0 && s.dy === 0 && s.rot === 0),
  'variant: zeroed parts → all part states exactly neutral at any clock')
ok(Object.values(pStock).some((s) => s.dx !== 0 || s.dy !== 0 || s.rot !== 0),
  'stock: parts still move (the stock template is untouched by the variant)')

// 4. The variant is registered (discoverable) and re-registration
//    with replace is deterministic (same id, same behaviour).
ok(IYH.listPortraitTemplates().some((t) => t.id === 'cal-test'), 'variant: discoverable in the template list')
IYH.registerPortraitTemplate({
  id: 'cal-test', label: 'cal-test', description: 'again',
  render: def2.render,
  movement: { ...def2.movement, travel: { ...def2.movement.travel, gazeX: def2.movement.travel.gazeX * 2 } },
  aliases: def2.aliases,
  parts: Object.fromEntries(Object.entries(def2.parts).map(([pid, p]) => [pid, {
    ...p,
    tx: Object.fromEntries(Object.entries(p.tx).map(([k, w]) => [k, 0])),
    ty: Object.fromEntries(Object.entries(p.ty).map(([k, w]) => [k, 0])),
    rot: Object.fromEntries(Object.entries(p.rot).map(([k, w]) => [k, 0])),
    traverse: { ...p.traverse, amplitude: 0 },
  }])),
}, { replace: true })
const pAgain = IYH.partStateFor(model, 'cal-test', T)
ok(Object.values(pAgain).every((s) => s.dx === 0 && s.dy === 0 && s.rot === 0),
  'variant: re-registration (replace) is deterministic')

// 5. The stock template is byte-stable through all of the above.
const stockAgain = IYH.partStateFor(model, 'neutral-pencil', T)
ok(JSON.stringify(stockAgain) === JSON.stringify(pStock),
  'stock: part states unchanged after variant registration + re-registration')

console.log('1..5')
console.log('# calibration stage: all checks passed')
