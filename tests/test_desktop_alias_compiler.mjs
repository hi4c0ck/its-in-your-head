// tests/test_desktop_alias_compiler.mjs — node test for the Stage 2
// alias compiler (template-owned axis→feature graph):
//   - the default alias set reproduces the legacy shared expression
//     byte-exactly (migration safety)
//   - a template may alias any subset of axes, with its own weights,
//     curve and sensitivity — no universal aliases are mandatory
//   - groups feed features; features may also read raw axes directly
//   - missing input means no signal (rest value), never fabricated
//   - deterministic: same model + template → same expression
// Run: node tests/test_desktop_alias_compiler.mjs
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
const data = {
  available: true,
  user: {
    directness: 0.8, context_richness: 0.35, exploration: 0.9,
    analytical_depth: 0.6, iteration: 0.2, scope: 0.7,
  },
  confidence: { directness: 0.9, context_richness: 0.4, exploration: 0.8,
    analytical_depth: 0.7, iteration: 0.5, scope: 0.6 },
}
const model = IYH.buildPortraitModel(data)

// 1 — default alias set is byte-identical to the legacy shared expression
//     for every expressive channel (neutral + angular + soft-flow all
//     fall back to the defaults, so the migration changed nothing).
const feats = ['brow', 'eyeOpen', 'gaze', 'gazeX', 'gazeY', 'mouthTension', 'mouthLift', 'headTilt', 'contourDrift']
for (const tid of IYH.listPortraitTemplates().map((t) => t.id)) {
  const e = IYH.expressionFor(model, tid)
  const same = feats.every((k) => e[k] === model.expression[k])
  ok(same, tid + ': default aliases byte-identical to model.expression (' + feats.length + ' channels)')
}

// 2 — a template can own its alias graph: re-weighted groups, its own
//     curve, and sensitivity. Expected values are derived from the model
//     itself (data-driven), not assumed to differ from the shared formula.
const unreg = IYH.registerPortraitTemplate({
  id: 'alias-test-tmpl',
  label: 'Alias test',
  description: 'test',
  render: () => '<svg></svg>',
  aliases: {
    groups: {
      focus: { axes: { analytical_depth: 1.0 }, curve: 'tanh', gain: 3, sensitivity: 2.0 },
      warmth: { axes: { exploration: 0.8, context_richness: 0.4 }, curve: 'linear', sensitivity: 1 },
    },
    features: {
      brow: { rest: 0, focus: -8.0 },          // stronger brow pull from focus
      mouthLift: { rest: 0, warmth: 12.0 },    // warmth drives the mouth harder
    },
  },
})
const e2 = IYH.expressionFor(model, 'alias-test-tmpl')
const shared = model.expression
// exact expectation for the custom focus group:
const devA = model.observed.analytical_depth.deviation || 0
const expectedFocus = Math.tanh(3 * Math.max(-1, Math.min(1, devA * 2.0))) / Math.tanh(3)
ok(Math.abs(e2.focus - expectedFocus) < 1e-12,
  'custom group: focus = tanh(3·dev_a·2)/tanh(3) = ' + e2.focus.toFixed(6) + ' (exact)')
ok(Math.abs(e2.brow - -8.0 * e2.focus) < 1e-12,
  'custom feature: brow = rest(0) + focus×(-8) = ' + e2.brow.toFixed(4) + ' (template\'s own coefficient)')
const devE = model.observed.exploration.deviation || 0
const devC = model.observed.context_richness.deviation || 0
const expectedWarmth = Math.max(-1, Math.min(1, devE * 0.8 + devC * 0.4))
ok(Math.abs(e2.mouthLift - expectedWarmth * 12.0) < 1e-12,
  'custom feature: mouthLift = warmth×12 = ' + e2.mouthLift.toFixed(4) + ' (template\'s own coefficient)')
// undeclared channels keep the shared formula (no universal alias) —
// checked on raw-axis features only (group-driven ones legitimately move
// when the template re-weights the groups):
ok(e2.headTilt === shared.headTilt && e2.gazeX === shared.gazeX,
  'undeclared raw-axis channels keep the shared formula (headTilt ' + e2.headTilt.toFixed(3) + ', gazeX ' + e2.gazeX.toFixed(1) + ')')
unreg()

// 3 — no mandatory universal aliases: a template that declares an EMPTY
//    feature drops all alias influence on that feature (rest only).
const unreg2 = IYH.registerPortraitTemplate({
  id: 'alias-empty-tmpl',
  label: 'Alias empty',
  description: 'test',
  render: () => '<svg></svg>',
  aliases: { features: { brow: { rest: 0 } } },
})
const e3 = IYH.expressionFor(model, 'alias-empty-tmpl')
ok(e3.brow === 0 && e3.mouthLift === shared.mouthLift,
  'empty feature declaration: brow rests at 0 while un-declared mouthLift stays shared')
unreg2()

// 4 — missing input means no signal: absent dimensions contribute 0.
const absent = IYH.buildPortraitModel({ available: true, user: { directness: 0.9 } })
const e4 = IYH.expressionFor(absent, 'neutral-pencil')
ok(e4.gazeX === -28.0 && e4.mouthLift === 0 && e4.headTilt === 0,
  'absent axes contribute no signal (gazeX rests at -28, mouthLift 0, headTilt 0)')

// 5 — determinism.
const a = IYH.expressionFor(IYH.buildPortraitModel(data), 'neutral-pencil')
const b = IYH.expressionFor(IYH.buildPortraitModel(data), 'neutral-pencil')
ok(JSON.stringify(a) === JSON.stringify(b), 'alias compiler is deterministic (same model + template → same expression)')

console.log('\n' + n + ' checks passed — alias compiler')
