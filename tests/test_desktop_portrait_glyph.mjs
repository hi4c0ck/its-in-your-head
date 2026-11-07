// tests/test_desktop_portrait_glyph.mjs — node test for the PORTRAIT glyph.
// Extracts the pure block between PORTRAIT-GLYPH markers in desktop/plugin.js
// and asserts determinism, gradual evolution, understated state, track
// separation, dimension roles, no <text>, and theme census.
// Run: node tests/test_desktop_portrait_glyph.mjs
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const dir = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(dir, '..', 'desktop', 'plugin.js'), 'utf8')
const start = src.indexOf('// PORTRAIT-GLYPH-START')
const end = src.indexOf('// PORTRAIT-GLYPH-END')
assert.ok(start >= 0 && end > start, 'PORTRAIT-GLYPH markers found in plugin.js')
const block = src.slice(start, end)
assert.ok(!/Math\.random|Date\.now|new Date|document\.|window\.|from 'react'|from "react"/.test(block), 'glyph block is pure (no random/dom/react)')
const { buildGlyph, glyphStep, GLYPH_DIMS } = new Function(block + '\n;return { buildGlyph, glyphStep, GLYPH_DIMS };')()

let n = 0
const ok = (cond, label) => { assert.ok(cond, label); console.log('ok ' + (++n) + ' - ' + label) }
const count = (s, re) => (s.match(re) || []).length
const layer = (s, name) => {
  const m = s.match(new RegExp('<g data-layer="' + name + '"[^>]*>(.*?)</g>', 's'))
  return m ? m[1] : ''
}
const nulls = () => Object.fromEntries(GLYPH_DIMS.map(d => [d, null]))
const full = (v) => Object.fromEntries(GLYPH_DIMS.map(d => [d, v]))
const conf = (v) => Object.fromEntries(GLYPH_DIMS.map(d => [d, v]))
const base = (over = {}) => Object.assign({ available: true, dimensions: full(0.5), user: full(0.6), agent: nulls(), confidence: conf(0.9), evidenceCount: 20 }, over)

// (a) determinism
const inp = base({ agent: full(0.4) })
ok(buildGlyph(inp) === buildGlyph(inp), 'a: same input byte-identical across calls')
const shuffled = { evidenceCount: 20, confidence: Object.fromEntries(Object.entries(conf(0.9)).reverse()), agent: Object.fromEntries(Object.entries(full(0.4)).reverse()), user: Object.fromEntries(Object.entries(full(0.6)).reverse()), dimensions: Object.fromEntries(Object.entries(full(0.5)).reverse()), available: true }
ok(buildGlyph(inp) === buildGlyph(shuffled), 'a: key-order shuffled input byte-identical')

// (b) gradual evolution: at most one step per 0.125; 0.05 moves <=1 step
for (const v of [0, 0.05, 0.1, 0.2, 0.33, 0.5, 0.62, 0.75, 0.9, 1]) {
  ok(Math.abs(glyphStep(v + 0.05) - glyphStep(v)) <= 1, 'b: step move <=1 for ' + v + ' +0.05')
}
const counts = [0, 1, 4, 8, 16, 32].map(ev => count(buildGlyph(base({ evidenceCount: ev })), /<circle|<line|<polygon|<path/g))
for (let i = 1; i < counts.length; i++) ok(counts[i] >= counts[i - 1], 'b: element count non-decreasing as evidence grows (' + counts.join(',') + ')')
ok(counts[0] < counts[counts.length - 1], 'b: evidence growth increases feature count')
const sweep = [0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1]
  .map(v => { const u = full(0.5); u.scope = v; return count(buildGlyph(base({ user: u })), /<circle/g) })
for (let i = 1; i < sweep.length; i++) ok(sweep[i] >= sweep[i - 1], 'b: scope sweep monotonic (' + sweep.join(',') + ')')

// (c) understated
const under = buildGlyph({ available: true, dimensions: full(0.5), user: nulls(), agent: nulls(), confidence: conf(0.1), evidenceCount: 0 })
const fullGlyph = buildGlyph(base())
ok(count(under, /<circle|<line|<polygon|<path/g) < count(fullGlyph, /<circle|<line|<polygon|<path/g), 'c: understated has fewer elements')
ok(under.includes('opacity="0.25"'), 'c: understated opacity 0.25 (faint)')
ok(!under.includes('<polygon') && !under.includes('<path'), 'c: understated is NOT full glyph (no inner shape)')
const lowConf = buildGlyph(base({ confidence: conf(0.05), evidenceCount: 0 }))
const opLow = Number(lowConf.match(/data-layer="observed" opacity="([\d.]+)"/)[1])
ok(opLow <= 0.4, 'c: low confidence/evidence -> faint opacity <=0.4 (got ' + opLow + ')')

// (d) track separation
ok(!buildGlyph(base({ agent: nulls() })).includes('data-layer="interpreted"'), 'd: agent all-null -> no interpreted layer')
const dual = buildGlyph(base({ agent: full(0.4) }))
const dual2 = buildGlyph(base({ agent: full(0.9) }))
ok(layer(dual, 'observed') === layer(dual2, 'observed'), 'd: agent change -> ONLY interpreted layer changes')
ok(layer(dual, 'interpreted') !== layer(dual2, 'interpreted'), 'd: agent change alters interpreted layer')
const dual3 = buildGlyph(base({ agent: full(0.4), user: full(0.9) }))
ok(layer(dual, 'interpreted') === layer(dual3, 'interpreted'), 'd: user change -> ONLY observed layer changes')
ok(layer(dual, 'observed') !== layer(dual3, 'observed'), 'd: user change alters observed layer')
ok(dual.includes('data-layer="observed"') && dual.includes('data-layer="interpreted"'), 'd: dual-track has both layers as separate groups')

// (e) dimension roles
const vary = (dim, lo, hi) => { const a = full(0.5), b = full(0.5); a[dim] = lo; b[dim] = hi; return [buildGlyph(base({ user: a })), buildGlyph(base({ user: b }))] }
let [lo, hi] = vary('directness', 0, 1)
ok(lo.includes('data-joint="round"') && hi.includes('data-joint="miter"'), 'e: directness high -> miter/straight, low -> round/curved')
ok(count(hi, /Q/g) < count(lo, /Q/g), 'e: directness high -> fewer curve control points')
;[lo, hi] = vary('scope', 0, 1)
ok(Number(hi.match(/<circle[^>]*r="([\d.]+)"/)[1]) > Number(lo.match(/<circle[^>]*r="([\d.]+)"/)[1]), 'e: scope high -> larger bounding circle')
;[lo, hi] = vary('iteration', 0, 1)
ok(count(layer(hi, 'observed'), /<circle[^>]*fill="none"/g) > count(layer(lo, 'observed'), /<circle[^>]*fill="none"/g), 'e: iteration high -> more concentric ring repetitions')
;[lo, hi] = vary('context_richness', 0, 1)
ok(count(layer(hi, 'observed'), /<line/g) > count(layer(lo, 'observed'), /<line/g), 'e: richness high -> more sub-elements (spokes)')
;[lo, hi] = vary('analytical_depth', 0, 1)
ok(count(layer(hi, 'observed'), /stroke-width="0.7"/g) > count(layer(lo, 'observed'), /stroke-width="0.7"/g), 'e: depth high -> inner lattice structure vs mass')
ok(hi.match(/points="[^"]*"/)[0].split(' ').length > lo.match(/points="[^"]*"/)[0].split(' ').length, 'e: depth high -> more inner polygon sides')

// (f) no numbers / no text
for (const s of [fullGlyph, dual, under]) ok(!/<text/.test(s) && !/>\s*\d/.test(s.replace(/<svg[^>]*>/, '')), 'f: no <text> and no numeric readouts')
ok(fullGlyph.startsWith('<svg viewBox="0 0 48 48"'), 'f: viewBox 0 0 48 48')

// (g) theme census
const allowed = new Set(['--ui-accent', '--ui-text-secondary', '--ui-text-quaternary', '--ui-stroke-secondary'])
const vars = [...fullGlyph.matchAll(/var\(\s*(--[a-zA-Z0-9-]+)/g)].map(m => m[1])
ok(vars.length > 0 && vars.every(v => allowed.has(v)), 'g: every var(--x) in SVG is a verified token (' + [...new Set(vars)].join(',') + ')')
ok(!/#[0-9a-fA-F]{3,8}\b/.test(fullGlyph) && !/rgb\(/.test(fullGlyph), 'g: no hardcoded hex/rgb colors')

// (h) layers stay geometrically distinct even for identical tracks
const sameTracks = buildGlyph(base({ user: full(0.6), agent: full(0.6) }))
ok(/data-layer="interpreted"[^>]*transform="translate\(24 24\) scale\(0\.92\) translate\(-24 -24\)"/.test(sameTracks), 'h: interpreted layer carries deterministic 0.92 centre scale (no pixel overplot)')
ok(!/data-layer="observed"[^>]*transform=/.test(sameTracks), 'h: observed layer unscaled (single canonical geometry)')
ok(layer(sameTracks, 'observed') === layer(buildGlyph(base({ user: full(0.6), agent: full(0.2) })), 'observed'), 'h: interpreted scale does not leak into observed layer')
const maxDual = buildGlyph(base({ user: full(1), agent: full(1) }))
ok(Math.max(...[...maxDual.matchAll(/<circle[^>]*r="([\d.]+)"/g)].map(m => Number(m[1]))) <= 17.4, 'h: max radius stays inside 48px box (24 + 17.4 + sat <= 48)')
ok(buildGlyph(base({ user: full(0.6), agent: full(0.6) })) === buildGlyph(base({ user: full(0.6), agent: full(0.6) })), 'h: scaled interpreted layer still byte-identical across calls')

// pane wiring (source level)
ok(src.includes('dangerouslySetInnerHTML'), 'pane mounts buildGlyph string as single source of truth')
ok(src.includes('jsx(PortraitView, {') && src.includes('drift: driftQ.data'), 'pane passes drift series evidence to PortraitView')
ok(src.includes('FLANK_ABBR'), 'compact portrait flank: observed/interpreted tick tracks (the glyph is no longer lonely)')
ok(src.includes("' · ⛶ full'"), 'wide-frame portrait readout points to the expanded inspection')
const pv = src.slice(src.indexOf('function PortraitView'), src.indexOf('function driftDelta'))
ok(!pv.includes("d.slice(0, 10)") && !pv.includes('Math.round(u * 100)'), 'bar body removed')

console.log('PASS portrait_glyph: ' + n + ' assertions')
