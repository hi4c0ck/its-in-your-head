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

// The old radial PORTRAIT-GLYPH block was retired in favor of the IYH_PORTRAIT
// contour engine (IYH-PORTRAIT-ENGINE block). The workspace test now verifies
// the engine is wired into both views instead of the size-parametrized glyph.

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

// 2. Portrait engine wiring: the IYH_PORTRAIT contour engine replaces the old
// radial glyph; both views render through the same engine + shared template
// preference store (no second portrait mechanism to drift out of sync).
ok(src.includes('const IYH_PORTRAIT = (() =>'), 'portrait engine inline bundle present')
ok(src.includes('id: \'neutral-pencil\'') && src.includes('id: \'angular-pencil\'') && src.includes('id: \'soft-flow-study\''),
  'all three drawing templates registered in the engine')
ok(src.includes('IYH_PORTRAIT.buildPortraitSvg'), 'the engine buildPortraitSvg API is consumed')
ok(src.includes("jsx(IyhPortraitCompact, { data, preferences, size: 48, fill: true })") && src.includes("flex: '0 0 132px', width: 132"),
  'full-height PORTRAIT: fill: true is now BOTH the tall sidebar and the wide bottom-dock; the flank + readout are compacted to a thin centered side column so the face covers most of the place (the fixed 48px strip square is retired)')
ok(src.includes('jsx(IyhPortraitStudio, { data, preferences })'),
  'expanded PORTRAIT renders the engine studio view')
ok(src.includes('createIyhPortraitTemplatePreferences(ctx)'),
  'one shared preference store is created in register(ctx)')

// 3. Expand control wiring (source-level, same style as the other suites).
ok(src.includes("typeof host.openWorkspace === 'function'"),
  'expand control feature-detects host.openWorkspace (older-build graceful degradation)')
ok(src.includes("const WS_WORKSPACE_ID = 'its-in-your-head-workspace'"),
  'stable workspace id (re-fronts the existing tab, never duplicates)')
ok(src.includes('host.openWorkspace(WS_WORKSPACE_ID, {'),
  'expand click calls host.openWorkspace with the stable id')
ok(src.includes("jsx(ExpandedWorkspace, { source: source, rest: rest, portraitPreferences, onCollapse: closeIyhWorkspace })"),
  'workspace render passes the pane ctx bridge + the shared portrait-template store + the collapse callback (expand/collapse symmetry, 32aab6e)')
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
eq(count("rest('/pulse'"), 2, 'no extra /pulse route consumers (both queryFns ride the cursor suffix: rest("/pulse" + pulseActivityQuerySuffix()))')
eq(count("rest('/portrait')"), 2, 'no extra /portrait route consumers')
ok(!src.includes("rest('/pulse?'"), 'no invented route variants')

// 5. Compact instrument unchanged by the workspace layer.
ok(src.includes('selectPulseRows(rows, PULSE_MAX_ROWS)'), 'compact PULSE still caps at PULSE_MAX_ROWS')
ok(src.includes('const PULSE_MAX_ROWS = 4'), 'compact lane still shows up to 4 tool rows')
ok(src.includes('rows.map((row, ri)'), 'expanded PULSE walks ALL tool rows (uncapped)')
ok(src.includes("gridTemplateColumns: 'repeat(4, minmax(0, 1fr))'"),
  'compact lanes keep equal-width 4-column tracks (PulseLanes; old row-table removed)')
ok(src.includes('jsx(IyhPortraitCompact, { data, preferences, size: 48, fill: true })') && src.includes("flex: '0 0 132px', width: 132"), 'full-height PORTRAIT: fill: true is now BOTH the tall sidebar and the wide bottom-dock; the flank + readout are compacted to a thin centered side column so the face covers most of the place (the fixed 48px strip square is retired)')
ok(src.includes("'PORTRAIT · no data yet — evidence empty'"),
  'honest compact empty-state placeholder retained (the studio view carries its own)')

// 6. Stage 6 gap-fill (§15 criterion 13): DRIFT + expanded inspection remain.
// ExpandedWorkspace still mounts ExpandedPulse / ExpandedPortrait /
// ExpandedDrift with their shared query data (not dead, not removed).
ok(src.includes('function ExpandedPulse'), 'ExpandedPulse (full tool matrix) still present')
ok(src.includes('function ExpandedDrift'), 'ExpandedDrift (history) still present')
ok(src.includes('function ExpandedWorkspace'), 'ExpandedWorkspace surface still present')
ok(src.includes('jsx(ExpandedPulse, { pulse: pulseQ.data })'), 'workspace PULSE tab renders ExpandedPulse from the shared pulse query')
ok(src.includes('jsx(ExpandedDrift, { data: driftQ.data })'), 'workspace DRIFT tab renders ExpandedDrift from the shared drift query')
ok(src.includes('jsx(ExpandedPortrait, { data: portraitQ.data, drift: driftQ.data, preferences: portraitPreferences })'),
  'workspace PORTRAIT tab renders ExpandedPortrait with drift + preferences')
ok(src.includes("{ id: 'drift', label: 'DRIFT' }"), 'DRIFT tab entry retained in expanded TABS')
ok(!src.includes('jsx(PulseMatrix'), 'pre-lane compact table renderer is gone (expanded inspection is ExpandedPulse, compact is PulseLanes)')

console.log('PASS workspace: ' + n + ' assertions')
