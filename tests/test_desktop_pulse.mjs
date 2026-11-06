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
const { pulseCellValue, pulseColumns, pulseLiveValue } =
  new Function(block + '\n;return { pulseCellValue, pulseColumns, pulseLiveValue };')()

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

// 4. drawPulse wiring (source-level, same style as the themecompat checks).
assert.ok(src.includes('const columns = pulseColumns(pulse)'), 'drawPulse uses pulseColumns')
assert.ok(src.includes("col === 'live' ? liveV : pulseCellValue(row[col])"),
  'LIVE column reads the derived live value, other columns read their cells')
assert.ok(src.includes('const isLive = liveV !== null'), 'live rows flagged from row.live activity')
assert.ok(src.includes('c2d.globalAlpha = isLive ? 0.85 : 0.35'),
  'live rows highlighted vs muted tool labels')
assert.ok(src.includes('if (!isLive) c2d.globalAlpha *= 0.45'),
  'non-live rows muted cell alpha')
assert.ok(src.includes('globalAlpha = 0.12 + 0.88'), 'heat alpha encoding unchanged')
assert.ok(src.includes('pulse.columns'), 'server columns still consumed (via pulseColumns)')
assert.ok(src.includes('tool_name'), 'pulse rows stay dynamic (tool_name)')
assert.ok(src.includes("'unlabeled'"), "'unlabeled' mapping documented (folds into cell states)")
assert.ok(src.indexOf('// PULSE-START') < src.indexOf('function pulseColumns'),
  'pure helpers live between the PULSE markers')
console.log('ok ' + (++n) + ' - drawPulse wiring assertions')

console.log('PASS pulse: ' + n + ' assertions')
