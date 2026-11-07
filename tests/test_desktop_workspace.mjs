// tests/test_desktop_workspace.mjs — node test for the expanded workspace.
// Extracts the pure WORKSPACE + PORTRAIT-GLYPH blocks from desktop/plugin.js
// and asserts: drift sparkline determinism/geometry, the size-parametrized
// glyph (byte-identical 48px case), and the source wiring of the single
// expand control (feature-detected host.openWorkspace, stable id, shared
// query keys — no second collector). Run: node tests/test_desktop_workspace.mjs
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const dir = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(dir, '..', 'desktop', 'plugin.js'), 'utf8')
const wsBlock = src.slice(src.indexOf('// WORKSPACE-START'), src.indexOf('// WORKSPACE-END'))
assert.ok(wsBlock.length > 0, 'WORKSPACE markers found in plugin.js')
const { driftSparkline } = new Function(wsBlock + '\n;return { driftSparkline };')()
const glyphBlock = src.slice(src.indexOf('// PORTRAIT-GLYPH-START'), src.indexOf('// PORTRAIT-GLYPH-END'))
assert.ok(glyphBlock.length > 0, 'PORTRAIT-GLYPH markers found in plugin.js')
const { buildGlyph, buildGlyphAt } = new Function(glyphBlock + '\n;return { buildGlyph, buildGlyphAt };')()

let n = 0
const eq = (actual, expected, label) => {
  assert.deepEqual(actual, expected, label + ' (got ' + JSON.stringify(actual) + ')')
  console.log('ok ' + (++n) + ' - ' + label)
}
const ok = (cond, label) => {
  assert.ok(cond, label)
  console.log('ok ' + (++n) + ' - ' + label)
}

// 1. driftSparkline: deterministic sparkline geometry.
eq(driftSparkline(null), null, 'null -> null')
eq(driftSparkline('nope'), null, 'non-array -> null')
eq(driftSparkline([0.5]), null, 'single value -> null (need >= 2)')
eq(driftSparkline([null, undefined, 'x']), null, 'non-finite values filtered out -> null')
const s1 = driftSparkline([0.2, 0.4, 0.6, 0.8])
ok(s1 && s1.count === 4, 'count reflects finite values')
ok(s1.points === driftSparkline([0.2, 0.4, 0.6, 0.8]).points,
  'identical series -> byte-identical points (deterministic)')
const shuffled = driftSparkline([0.8, 0.2, 0.4, 0.6])
ok(shuffled.points !== s1.points, 'reordered series -> different geometry (series order is the signal)')
const parts = s1.points.split(' ')
ok(parts.length === 4, 'one point per value')
for (const p of parts) {
  const [x, y] = p.split(',').map(Number)
  ok(x >= 2 && x <= 94, 'x within padded 96-wide box (' + x + ')')
  ok(y >= 2 && y <= 22, 'y within padded 24-high box (' + y + ')')
}
const xs = parts.map((p) => Number(p.split(',')[0]))
ok(xs.every((x, i) => i === 0 || x > xs[i - 1]), 'x strictly increasing (time axis)')
eq(driftSparkline([0.5, 0.5, 0.5]).points, '2.0,22.0 48.0,22.0 94.0,22.0',
  'flat series -> bottom-padded flat line (x evenly spaced)')
eq(driftSparkline([0.2, 0.6]).delta, 0.4, 'delta = last - previous')
ok(driftSparkline([0.9, 0.4]).delta < 0, 'falling series -> negative delta')

// 2. Glyph sizing: buildGlyph is the 48px case; geometry is size-invariant.
const input = {
  available: true,
  user: { directness: 0.8, context_richness: 0.6, exploration: 0.5,
    analytical_depth: 0.7, iteration: 0.4, scope: 0.9 },
  agent: null,
  confidence: null,
  evidenceCount: 5,
}
eq(buildGlyph(input), buildGlyphAt(input, 48), 'buildGlyph === buildGlyphAt(_, 48) (compact case)')
const big = buildGlyphAt(input, 160)
ok(big.startsWith('<svg viewBox="0 0 48 48"') && big.includes('width="160" height="160"'),
  '160px glyph keeps the 48-unit viewBox at 160 render size')
eq(big.slice(big.indexOf('>') + 1, -'</svg>'.length),
  buildGlyph(input).slice(buildGlyph(input).indexOf('>') + 1, -'</svg>'.length),
  'glyph BODY is size-invariant (only the width/height attrs change)')
ok(buildGlyphAt(input, 0).includes('width="48"'), 'non-positive size falls back to 48')
ok(buildGlyphAt(input, null).includes('width="48"'), 'null size falls back to 48')
ok(buildGlyphAt(null, 160).includes('opacity="0.25"'), 'empty input -> the understated seed glyph at any size')

// 3. Expand control wiring (source-level, same style as the other suites).
ok(src.includes("typeof host.openWorkspace === 'function'"),
  'expand control feature-detects host.openWorkspace (older-build graceful degradation)')
ok(src.includes("const WS_WORKSPACE_ID = 'its-in-your-head-workspace'"),
  'stable workspace id (re-fronts the existing tab, never duplicates)')
ok(src.includes('host.openWorkspace(WS_WORKSPACE_ID, {'),
  'expand click calls host.openWorkspace with the stable id')
ok(src.includes("jsx(ExpandedWorkspace, { source: source, rest: rest })"),
  'workspace render is the ExpandedWorkspace component with the pane ctx bridge')
ok(src.includes("name: 'expand'") && src.includes('IYH_IC'),
  'expand icon is the plugin atlas symbol (iyh expand, tintable) — not the SDK set')
ok(!src.includes('icons.Maximize'), 'SDK icons namespace retired in favor of the custom atlas')
ok(src.includes('disabled: !wsCan'), 'icon is disabled when the host API is absent')
ok(src.includes("title: \"It's in Your Head\""), 'workspace tab carries the plugin title')

// 4. Data discipline: one observer set, shared keys, no second collector.
const count = (needle) => src.split(needle).length - 1
eq(count("[source, 'iyh-pulse']"), 2, 'pulse query defined in exactly two places (compact + workspace, shared cache)')
eq(count("[source, 'iyh-metrics']"), 2, 'metrics query defined in exactly two places')
eq(count("[source, 'iyh-portrait']"), 2, 'portrait query defined in exactly two places')
eq(count("[source, 'iyh-drift']"), 2, 'drift query defined in exactly two places')
eq(count("rest('/pulse')"), 2, 'no extra /pulse route consumers')
eq(count("rest('/portrait')"), 2, 'no extra /portrait route consumers')
ok(!src.includes("rest('/pulse?'"), 'no invented route variants')

// 5. Compact instrument unchanged by the workspace layer.
ok(src.includes('selectPulseRows(rows, PULSE_MAX_ROWS)'), 'compact PULSE still caps at PULSE_MAX_ROWS')
ok(src.includes('const PULSE_MAX_ROWS = 4'), 'compact lane still shows up to 4 tool rows')
ok(src.includes('rows.map((row, ri)'), 'expanded PULSE walks ALL tool rows (uncapped)')
ok(src.includes("gridTemplateRows: 'repeat(' + PULSE_MAX_ROWS + ', 1fr)'"),
  'compact matrix keeps its fixed track structure')
ok(src.includes('buildGlyph({'), 'compact PORTRAIT still renders the 48px glyph')
ok(src.includes("children: 'PORTRAIT · no data yet — evidence empty'") ||
  src.includes("'PORTRAIT · no data yet — evidence empty'"),
  'honest empty-state placeholder retained in both views')

console.log('PASS workspace: ' + n + ' assertions')
