// tests/test_desktop_portrait_engine.mjs — node test for the IYH_PORTRAIT
// contour engine + its desktop integration (replaces the retired radial-glyph
// suite). Extracts the IYH-PORTRAIT-ENGINE block from desktop/plugin.js and the
// IYH-PORTRAIT-UI block, runs them in a sandbox, and verifies:
//   - all three drawing templates render valid SVG (compact + studio)
//   - same state + template + rendering is byte-deterministic
//   - neutral input -> a restrained portrait (no accent extensions)
//   - increasing expressive parameters grow the portrait gradually
//   - missing / partial / invalid data never breaks rendering
//   - the compact render keeps its viewBox (scales to the 48px dock budget)
//   - both views render through the SAME engine + shared preference store, and
//     the template preference persists through a reload via ctx.storage.
// Run: node tests/test_desktop_portrait_engine.mjs
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'
import assert from 'node:assert/strict'

const dir = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(dir, '..', 'desktop', 'plugin.js'), 'utf8')

// ---- extract the two integration blocks straight from the production file ----
function block(start, end) {
  const s = src.indexOf(start)
  const e = src.indexOf(end)
  assert.ok(s >= 0 && e > s, start + ' found in plugin.js')
  return src.slice(s + start.length, e)
}
const engineBlock = block('// IYH-PORTRAIT-ENGINE-START', '// IYH-PORTRAIT-ENGINE-END')
const uiBlock = block('// IYH-PORTRAIT-UI-START', '// IYH-PORTRAIT-UI-END')

// ---- run the engine + UI blocks in a sandbox (React/DOM not needed) ----
const context = vm.createContext({
  // component bodies reference these; we never invoke the components, but
  // provide harmless stubs so the module-scope of the UI block is safe.
  useState: () => [null, () => {}],
  useEffect: () => {},
  jsx: () => null,
  jsxs: () => null,
  clamp01: (v) => (typeof v === 'number' && Number.isFinite(v) ? Math.min(1, Math.max(0, v)) : null),
  // module constant declared after the UI block in plugin.js; the pane clock
  // (iyhPortraitClockFor) closes over it at call time — mirror the value.
  PORTRAIT_POLL_MS: 850,
})
vm.runInContext(engineBlock + '\nglobalThis.__IYH_PORTRAIT__ = IYH_PORTRAIT;', context)
vm.runInContext(uiBlock + '\nglobalThis.__IYH_UI__ = { buildIyhPortraitMarkup, createIyhPortraitTemplatePreferences, IYH_PORTRAIT_TEMPLATE_PREF_KEY };', context)
const IYH_PORTRAIT = context.__IYH_PORTRAIT__
const UI = context.__IYH_UI__

let n = 0
const ok = (cond, label) => { assert.ok(cond, label); console.log('ok ' + (++n) + ' - ' + label) }
const dims = IYH_PORTRAIT.PORTRAIT_DIMENSIONS
const all = (v) => Object.fromEntries(dims.map((d) => [d, v]))
const features = (s) => (s.match(/<(?:path|line|circle|polygon)/g) || []).length +
  (s.match(/data-accent-region/g) || []).length

// (1) three templates, one default
const ids = IYH_PORTRAIT.listPortraitTemplates().map((t) => t.id)
ok(ids.length === 3, 'three drawing templates registered: ' + ids.join(','))
ok(IYH_PORTRAIT.DEFAULT_PORTRAIT_TEMPLATE === 'neutral-pencil', 'neutral-pencil is the default (restrained)')

const payload = {
  available: true,
  dimensions: all(0.5),
  user: { directness: 0.75, context_richness: 0.6, exploration: 0.4, analytical_depth: 0.55, iteration: 0.3, scope: 0.65 },
  agent: null,
  confidence: all(0.6),
}

// (2) all templates x both renderings emit well-formed SVG
for (const id of ids) {
  for (const r of ['compact', 'studio']) {
    const s = IYH_PORTRAIT.buildPortraitSvg(payload, { template: id, rendering: r })
    assert.ok(s.startsWith('<svg') && s.endsWith('</svg>'), id + '/' + r + ' well-formed')
    ok(true, 'template ' + id + ' renders valid SVG (' + r + ')')
  }
}

// (3) determinism: same state + template + rendering -> byte-identical
const a = IYH_PORTRAIT.buildPortraitSvg(payload, { template: 'neutral-pencil', rendering: 'studio' })
const b = IYH_PORTRAIT.buildPortraitSvg(payload, { template: 'neutral-pencil', rendering: 'studio' })
ok(a === b, 'same state + template + rendering is byte-identical')
const shuffled = {
  available: true,
  dimensions: Object.fromEntries(Object.entries(payload.dimensions).reverse()),
  user: Object.fromEntries(Object.entries(payload.user).reverse()),
  agent: null,
  confidence: Object.fromEntries(Object.entries(payload.confidence).reverse()),
}
ok(IYH_PORTRAIT.buildPortraitSvg(shuffled, { template: 'neutral-pencil', rendering: 'studio' }) === a,
  'input key-order is irrelevant (canonical dim order)')

// (4) neutral input -> restrained portrait (no accent extension layer)
const neutral = { available: true, dimensions: all(0.5), user: all(0.5), agent: null, confidence: all(0.5) }
const neutralSvg = IYH_PORTRAIT.buildPortraitSvg(neutral, { template: 'neutral-pencil', rendering: 'studio' })
ok(!/data-accent-region="extensions"/.test(neutralSvg), 'neutral input is restrained (no accent extensions)')

// (5) expressiveness grows gradually: quiet -> loud adds features
const quiet = { available: true, dimensions: all(0.5), user: all(0.15), agent: null, confidence: all(0.2) }
const loud = { available: true, dimensions: all(0.5), user: all(0.9), agent: all(0.8), confidence: all(0.9) }
ok(features(IYH_PORTRAIT.buildPortraitSvg(loud, { rendering: 'studio' })) >
   features(IYH_PORTRAIT.buildPortraitSvg(quiet, { rendering: 'studio' })),
  'louder evidence -> more portrait features (gradual evolution)')
// single-dimension sweep: the drawing changes step by step, never malformed
const sweep = [0, 0.25, 0.5, 0.75, 1].map((v) => {
  const u = all(0.5); u.analytical_depth = v
  return IYH_PORTRAIT.buildPortraitSvg({ available: true, dimensions: all(0.5), user: u, agent: null, confidence: all(0.6) },
    { template: 'neutral-pencil', rendering: 'studio' })
})
for (const s of sweep) assert.ok(s.startsWith('<svg') && s.endsWith('</svg>'), 'sweep step well-formed')
ok(new Set(sweep).size >= 3, 'analytical_depth sweep visibly changes the portrait (>=3 distinct shapes)')

// (6) missing / partial / invalid data never breaks rendering
for (const bad of [ {}, null, { available: true, dimensions: {} }, { user: { directness: 'garbage' } }, { available: false } ]) {
  const s = IYH_PORTRAIT.buildPortraitSvg(bad || {}, { template: 'angular-pencil', rendering: 'compact' })
  assert.ok(s.startsWith('<svg') && s.endsWith('</svg>'), 'invalid input still svg (' + JSON.stringify(bad) + ')')
}
ok(true, 'missing/partial/invalid data does not break rendering')

// (7) compact render keeps its viewBox so it scales into the 48px dock
const compact = IYH_PORTRAIT.buildPortraitSvg(payload, { template: 'neutral-pencil', rendering: 'compact' })
assert.ok(/viewBox=/.test(compact), 'compact svg carries a viewBox')
assert.ok(/preserveAspectRatio=/.test(compact) || /xMidYMid/.test(compact), 'compact svg scales without distortion')
ok(true, 'compact render preserves aspect ratio for the 48px dock budget')

// (8) the UI layer: both views build from the same engine (markup parity)
const compactMarkup = UI.buildIyhPortraitMarkup(payload, 'neutral-pencil', 'compact', 'iyh-dock-portrait')
const studioMarkup = UI.buildIyhPortraitMarkup(payload, 'neutral-pencil', 'studio', 'iyh-workspace-portrait')
ok(compactMarkup.startsWith('<svg width="100%" height="100%"'), 'compact markup fills its div (ratio kept)')
ok(studioMarkup.length > compactMarkup.length, 'studio markup carries more detail than compact')
ok(compactMarkup !== studioMarkup, 'compact + studio are distinct renderings of the same state')

// (9) the template preference persists through a reload via ctx.storage
const persisted = new Map()
const writes = []
const fakeCtx = { storage: {
  get: (k, d) => (persisted.has(k) ? persisted.get(k) : d),
  set: (k, v) => { writes.push([k, v]); persisted.set(k, v) },
  remove: (k) => persisted.delete(k),
} }
const prefs = UI.createIyhPortraitTemplatePreferences(fakeCtx)
ok(prefs.get() === IYH_PORTRAIT.DEFAULT_PORTRAIT_TEMPLATE, 'cold start -> default template')
ok(prefs.set('soft-flow-study') === true && prefs.get() === 'soft-flow-study', 'valid template accepted')
ok(writes.length === 1 && writes[0][0] === UI.IYH_PORTRAIT_TEMPLATE_PREF_KEY, 'persisted through ctx.storage.set (plugin-scoped)')
ok(prefs.set('does-not-exist') === false, 'unknown template id rejected')
const prefs2 = UI.createIyhPortraitTemplatePreferences(fakeCtx) // simulate a reload
ok(prefs2.get() === 'soft-flow-study', 'preference survives a reload (re-read from ctx.storage)')

// (10) source wiring: both views render the engine; PULSE/DRIFT routes intact
ok(src.includes('jsx(IyhPortraitCompact, { data, preferences, size: 48, fill: true })') && src.includes("flex: '0 0 132px', width: 132"), 'full-height PORTRAIT: fill: true is now BOTH the tall sidebar and the wide bottom-dock; the flank + readout are compacted to a thin centered side column so the face covers most of the place (the fixed 48px strip square is retired)')
ok(src.includes('jsx(IyhPortraitStudio, { data, preferences })'), 'expanded PORTRAIT renders the engine studio view')
ok(src.includes('createIyhPortraitTemplatePreferences(ctx)'), 'one shared preference store created in register(ctx)')
ok(src.includes("rest('/pulse'") && src.includes("rest('/drift')") && src.includes("rest('/portrait')"),
  'PULSE / DRIFT / PORTRAIT routes all intact')

// (11) the per-set reference-seed structure ("expand the algo"): the portrait
// face differs by seed, is deterministic, and no-seed == today's baseline.
const noSeed = { available: true, seed: false, faceWidth: 0, jaw: 0, brow: 0 }
const seedA = { seed: true, available: true, faceWidth: 0.43, jaw: 0.29, brow: 0.28,
                headWidth: -0.1, eyeAspect: 0.2, hairMass: 0.25, shoulder: -0.15 }
const seedB = { seed: true, available: true, faceWidth: -0.4, jaw: -0.25, brow: 0.1,
                headWidth: 0.2, eyeAspect: -0.3, hairMass: -0.2, shoulder: 0.2 }
for (const t of ['neutral-pencil', 'angular-pencil', 'soft-flow-study']) {
  for (const r of ['compact', 'studio']) {
    const base_ = IYH_PORTRAIT.buildPortraitSvg({ ...payload, morphology: noSeed }, { template: t, rendering: r })
    const a = IYH_PORTRAIT.buildPortraitSvg({ ...payload, morphology: seedA }, { template: t, rendering: r })
    const b = IYH_PORTRAIT.buildPortraitSvg({ ...payload, morphology: seedB }, { template: t, rendering: r })
    const a2 = IYH_PORTRAIT.buildPortraitSvg({ ...payload, morphology: seedA }, { template: t, rendering: r })
    assert.ok(a !== base_, t + '/' + r + ': seed changes the face from the neutral baseline')
    assert.ok(a !== b, t + '/' + r + ': two seeds render different faces (per-set)')
    assert.ok(a === a2, t + '/' + r + ': the seed is deterministic')
  }
}
ok(true, 'the reference-seed structure differentiates every set, deterministically, across all templates')

console.log('PASS portrait_engine: ' + n + ' assertions')
