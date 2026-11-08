// tests/test_desktop_pulse.mjs — node test for the PULSE matrix rendering.
// Extracts the pure helpers between the PULSE markers in desktop/plugin.js
// and asserts the LIVE / GOAL / GOOD / FRICTION visible column order, the
// LIVE value derivation from row.live, and the live-row muted/highlighted
// styling hooks. Run: node tests/test_desktop_pulse.mjs
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const dir = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(dir, '..', 'desktop', 'plugin.js'), 'utf8')
const block = src.slice(src.indexOf('// PULSE-START'), src.indexOf('// PULSE-END'))
assert.ok(block.length > 0, 'PULSE markers found in plugin.js')
const { pulseCellValue, pulseColumns, pulseLiveValue, pulseMomentumValue,
        pulseMomentumGlyph, selectPulseRows } =
  new Function(block +
    '\n;return { pulseCellValue, pulseColumns, pulseLiveValue, pulseMomentumValue, pulseMomentumGlyph, selectPulseRows };')()

let n = 0
const eq = (actual, expected, label) => {
  assert.deepEqual(actual, expected, label + ' (got ' + JSON.stringify(actual) + ')')
  console.log('ok ' + (++n) + ' - ' + label)
}
const near = (actual, expected, label) => {
  assert.ok(typeof actual === 'number' && Math.abs(actual - expected) < 1e-9,
    label + ' (got ' + actual + ')')
  console.log('ok ' + (++n) + ' - ' + label)
}

// 1. Visible column order: LIVE first, then GOAL / GOOD / FRICTION.
eq(pulseColumns({ columns: ['goal', 'good', 'friction', 'unlabeled'] }),
  ['live', 'goal', 'good', 'friction'], 'legacy server columns -> LIVE first, unlabeled dropped')
eq(pulseColumns({ columns: ['live', 'goal', 'good', 'friction'] }),
  ['live', 'goal', 'good', 'friction'], 'live already present -> no duplicate')
eq(pulseColumns({ columns: ['good', 'goal'] }),
  ['live', 'goal', 'good'], 'canonical GOAL/GOOD/FRICTION order regardless of server order')
eq(pulseColumns(null), ['live', 'goal', 'good', 'friction'], 'no pulse -> fallback columns')
eq(pulseColumns({}), ['live', 'goal', 'good', 'friction'], 'no columns field -> fallback columns')
eq(pulseColumns({ columns: [] }), ['live', 'goal', 'good', 'friction'], 'empty columns -> fallback columns')
eq(pulseColumns({ columns: ['unlabeled'] }),
  ['live', 'goal', 'good', 'friction'], 'unlabeled-only server list -> fallback semantic columns')

// 2. LIVE value from the per-row live counts the response already carries.
eq(pulseLiveValue(null), null, 'null row -> null live')
eq(pulseLiveValue({}), null, 'row without live -> null live')
eq(pulseLiveValue({ live: null }), null, 'null live -> null live')
eq(pulseLiveValue({ live: {} }), null, 'empty live -> null live')
eq(pulseLiveValue({ live: { calls_started: 0, calls_completed: 0, errors: 0 } }),
  null, 'zero activity -> null live (neutral, not live)')
near(pulseLiveValue({ live: { calls_started: 1 } }), 0.5, 'one started call -> 0.5')
near(pulseLiveValue({ live: { calls_started: 3, calls_completed: 2, errors: 2 } }),
  0.875, 'seven total calls -> 0.875')
near(pulseLiveValue({ live: { calls_completed: 9 } }), 0.9, 'nine completed calls -> 0.9')
const v1 = pulseLiveValue({ live: { calls_started: 1 } })
const v100 = pulseLiveValue({ live: { calls_started: 100 } })
assert.ok(v100 > v1 && v100 < 1, 'LIVE squashes unbounded counts into (0,1), monotone')
console.log('ok ' + (++n) + ' - LIVE squashes unbounded counts into (0,1), monotone')
eq(pulseLiveValue({ live: { calls_started: 'x', calls_completed: 2 } }),
  pulseLiveValue({ live: { calls_completed: 2 } }), 'non-numeric counts ignored, not NaN')

// 3. Similarity cells unchanged.
eq(pulseCellValue({ value: 0.5 }), 0.5, 'cell 0.5 -> 0.5')
eq(pulseCellValue({ value: 2 }), 1, 'cell value clamped to 1')
eq(pulseCellValue({ value: -1 }), 0, 'cell value clamped to 0')
eq(pulseCellValue({}), null, 'cell without value -> null')
eq(pulseCellValue(null), null, 'null cell -> null')
eq(pulseCellValue({ value: 'x' }), null, 'non-numeric cell -> null')

// 4. PULSE matrix structure (DOM table, not a painted canvas).
assert.ok(src.includes('function PulseMatrix'), 'PulseMatrix DOM component present')
assert.ok(src.includes("role: 'grid'"), 'matrix root carries role="grid"')
assert.ok(src.includes("gridTemplateColumns: 'minmax(56px, 34%) repeat("),
  'matrix columns: label track + one 1fr track per semantic column')
assert.ok(src.includes("gridTemplateRows: 'repeat(' + PULSE_MAX_ROWS + ', 1fr)'"),
  'row count is a fixed track structure (no pitch arithmetic)')
assert.ok(src.includes('selectPulseRows(rows, PULSE_MAX_ROWS)'),
  'row selection is deterministic (momentum surge, then live activity, then semantic, then server order)')
assert.ok(src.includes("col === 'friction' ? V_DANGER : V_ACCENT"),
  'friction cells take the danger token, others the accent (theme vars only)')
assert.ok(src.includes('(0.12 + 0.88 * v)'), 'heat alpha encoding preserved')
assert.ok(src.includes('isLive ? 1 : 0.45'), 'non-live rows muted on their cells')
assert.ok(src.includes('color: isLive ? V_SEC : V_QUAT'),
  'live rows highlighted vs muted tool labels (no new colors)')
assert.ok(!src.includes("getContext('2d')"), 'canvas painting retired')
assert.ok(!src.includes('drawPulse'), 'drawPulse removed')
assert.ok(!src.includes('resolveColors'), 'resolveColors removed')
assert.equal((src.match(/new ResizeObserver/g) || []).length, 1,
  'exactly ONE ResizeObserver: frame-orientation read on the root div (narrow dock); drawing surfaces stay fixed-size')
assert.equal((src.match(/useEffect\(/g) || []).length, 2,
  'exactly two mount-once effects: the Strip root orientation observer + the EncoderSwitcher engine-status poll — no per-frame / no-interval redraw loops')
assert.ok(src.includes('pulse.columns'), 'server columns still consumed (via pulseColumns)')
assert.ok(src.includes('tool_name'), 'pulse rows stay dynamic (tool_name)')
assert.ok(src.includes('NARROW_W'), 'narrow-dock threshold defined (sidebar frame)')
assert.ok(src.includes('PULSE_GLOW_CSS'), 'live-glow keyframes: theme-var box-shadow pulse on value change')
assert.ok(src.includes('iyh-glow-warn'), 'friction cells glow on the destructive token (value cells on accent)')
assert.ok(src.includes("className: g ? 'iyh-glow'"), 'changed cells carry the one-shot glow class (key counter restarts the keyframe)')
assert.ok(src.includes("flex: narrow ? '1 1 30%' : undefined"),
  'drift wraps 3x2 in the narrow dock (labels to tooltip, numbers stay)')
assert.ok(src.indexOf('...(!narrow') > -1,
  'header wordmark / status / I/O column collapse under the narrow dock (tooltips keep them reachable)')
assert.ok(src.indexOf('// PULSE-START') < src.indexOf('function selectPulseRows'),
  'pure helpers live between the PULSE markers')
console.log('ok ' + (++n) + ' - matrix structure assertions')

// 5. selectPulseRows: deterministic row selection for the matrix.
eq(selectPulseRows(null, 4), [], 'null pulse rows -> []')
eq(selectPulseRows('nope', 4), [], 'non-array -> []')
eq(selectPulseRows([{ tool_name: 'a' }], 0), [], 'cap 0 -> []')
eq(selectPulseRows([{ tool_name: 'a' }], -2), [], 'negative cap -> []')
const many = ['a', 'b', 'c', 'd', 'e', 'f'].map((t) => ({ tool_name: t }))
eq(selectPulseRows(many, 4).map((r) => r.tool_name), ['a', 'b', 'c', 'd'],
  'cap respected, server order kept when nothing else ranks')
const rowsMixed = [
  { tool_name: 'quiet', goal: { value: 0.9 }, good: { value: 0.8 }, friction: { value: 0.7 } },
  { tool_name: 'lively', live: { calls_started: 1 } },
]
eq(selectPulseRows(rowsMixed, 2).map((r) => r.tool_name), ['lively', 'quiet'],
  'live activity ranks ahead of strong-but-unlive cells')
const twoLive = [
  { tool_name: 'low', live: { calls_started: 1 } },
  { tool_name: 'high', live: { calls_started: 5, calls_completed: 9 } },
]
eq(selectPulseRows(twoLive, 2).map((r) => r.tool_name), ['high', 'low'],
  'higher live total ranks first')
const semTie = [
  { tool_name: 'soft', good: { value: 0.2 } },
  { tool_name: 'hard', goal: { value: 0.9 } },
  { tool_name: 'mid', friction: { value: 0.5 } },
]
eq(selectPulseRows(semTie, 3).map((r) => r.tool_name), ['hard', 'mid', 'soft'],
  'among non-live rows: strongest semantic cell first, stable server order')
const stable = [
  { tool_name: 'x', live: { calls_started: 3 } },
  { tool_name: 'y', live: { calls_started: 3 } },
  { tool_name: 'z', live: { calls_started: 3 } },
]
eq(selectPulseRows(stable, 2).map((r) => r.tool_name), ['x', 'y'],
  'ties keep server (first_seen) order — stable, deterministic')
const again = selectPulseRows(stable, 2)
eq(again.map((r) => r.tool_name),
  selectPulseRows(stable, 2).map((r) => r.tool_name),
  'repeated calls are identical (deterministic)')

// 5b. Momentum (Tier-0 positive feedback): surge is the PRIMARY rank key.
eq(pulseMomentumValue({}), 0.5, 'no momentum cell -> neutral 0.5')
eq(pulseMomentumValue(null), 0.5, 'null row -> neutral 0.5')
eq(pulseMomentumValue({ momentum: { value: 0.8 } }), 0.8, 'reads momentum.value')
eq(pulseMomentumGlyph({ momentum: { state: 'surging' } }), '\u25B2', 'surging -> ▲')
eq(pulseMomentumGlyph({ momentum: { state: 'cooling' } }), '\u25BC', 'cooling -> ▼')
eq(pulseMomentumGlyph({ momentum: { state: 'steady' } }), '', 'steady -> empty')
eq(pulseMomentumGlyph({}), '', 'legacy row (no momentum) -> empty')
const surgeJump = [
  { tool_name: 'idle_heavy', live: { calls_started: 10, calls_completed: 20 } },
  { tool_name: 'hot_new', momentum: { value: 1.0, state: 'surging' } },
]
eq(selectPulseRows(surgeJump, 2).map((r) => r.tool_name),
  ['hot_new', 'idle_heavy'],
  'a surging tool (momentum 1.0) pops ahead of a higher-live idle tool')
const momOrder = [
  { tool_name: 'cooling', momentum: { value: 0.2, state: 'cooling' } },
  { tool_name: 'steady', momentum: { value: 0.5, state: 'steady' } },
  { tool_name: 'surging', momentum: { value: 0.9, state: 'surging' } },
]
eq(selectPulseRows(momOrder, 3).map((r) => r.tool_name),
  ['surging', 'steady', 'cooling'],
  'momentum descending: surging > steady > cooling')
console.log('PASS pulse: ' + n + ' assertions')
