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
        pulseMomentumGlyph, selectPulseRows, pulseBand, buildPulseVerdict, pulseRecency } =
  new Function(block +
    '\n;return { pulseCellValue, pulseColumns, pulseLiveValue, pulseMomentumValue, pulseMomentumGlyph, selectPulseRows, pulseBand, buildPulseVerdict, pulseRecency };')()

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

// 4. PULSE matrix structure (Stage 6: pre-lane table retired, lanes live).
// The old compact table-layout renderer PulseMatrix (label rail + four
// graphic columns + global row ordering) is dead code after S3 — the
// compact surface is PulseLanes. ExpandedPulse keeps the row helpers.
assert.ok(!src.includes('function PulseMatrix'), 'Stage 6: dead PulseMatrix table renderer removed')
assert.ok(src.includes('function PulseLanes'), 'live lane renderer PulseLanes present')
assert.ok(src.includes("role: 'grid'"), 'matrix root carries role="grid"')
assert.ok(!src.includes("gridTemplateColumns: 'minmax(56px, 34%) repeat("),
  'no separate tool-name rail (56px label track removed)')
assert.ok(!src.includes("gridTemplateRows: 'repeat(' + PULSE_MAX_ROWS + ', 1fr)'"),
  'fixed row-track table structure removed (capacity now from measured box)')
assert.ok(src.includes('selectPulseRows(rows, PULSE_MAX_ROWS)'),
  'row selection is deterministic (momentum surge, then live activity, then semantic, then server order)')
assert.ok(src.includes("c === 'friction' ? V_DANGER : V_ACCENT"),
  'friction cells take the danger token, others the accent (theme vars only)')
assert.ok(src.includes('0.12 + 0.88 * v'),
  'LIVE column keeps the count-squash alpha (semantic cols use pulseBand discrete bands)')
assert.ok(!src.includes('isLive ? 1 : 0.45'), 'compact-table live-row muting retired with PulseMatrix')
assert.ok(!src.includes('Math.min(0.55'), 'compact-table LIVE cap retired with PulseMatrix')
assert.ok(!src.includes("borderLeft: '1px solid ' + V_STROKE"), 'compact-table group separator retired with PulseMatrix')
assert.ok(src.includes('LIVE\u00b7CALLS'), 'expanded LIVE header reads as activity (calls), not similarity')
assert.ok(src.includes('pulseBand'), 'semantic cells use the discrete 3-band encoding (pulseBand)')
assert.ok(src.includes('buildPulseVerdict'), 'worded verdict helper is wired into the pane')
assert.ok(src.includes('color: isLive ? V_SEC : V_QUAT'),
  'live rows highlighted vs muted tool labels (no new colors)')
// Canvas surfaces are PORTRAIT-ONLY now (the engine's offscreen morph
// buffer + the ambient particles layer, committed with the portrait
// engine work): PULSE/DOM painting stays canvas-free — the PULSE-era
// canvas path (drawPulse/resolveColors) is gone for good.
assert.ok(src.includes("off.getContext('2d')"), 'portrait offscreen morph canvas (engine work)')
assert.ok(!src.includes('drawPulse'), 'drawPulse removed (PULSE never canvas-painted)')
assert.ok(!src.includes('resolveColors'), 'resolveColors removed (PULSE never canvas-painted)')
// Four ResizeObservers, each a MOUNTED-SURFACE read (no per-frame
// loops): 2165 portrait fit, 3707 PulseLanes grid measure, 5094/5118
// the two Strip composition measures (bottom + sidebar surfaces).
assert.equal((src.match(/new ResizeObserver/g) || []).length, 4,
  'four surface-measure ResizeObservers (portrait fit, PulseLanes grid, two Strip composition reads) — no per-frame redraw')
assert.equal((src.match(/setInterval\(/g) || []).length, 0,
  'no setInterval anywhere: polling runs through useQuery refetchInterval')
assert.ok(src.includes('pulse.columns'), 'server columns still consumed (via pulseColumns)')
assert.ok(src.includes('tool_name'), 'pulse rows stay dynamic (tool_name)')
assert.ok(src.includes('NARROW_W'), 'narrow-dock threshold defined (sidebar frame)')
assert.ok(src.includes('PULSE_GLOW_CSS'), 'bubble keyframes: theme-var rise on value change (Stage 6: glow retired with PulseMatrix, bubble rise kept for PulseLanes)')
assert.ok(!src.includes('iyh-glow-warn'), 'Stage 6: friction glow keyframe retired with dead PulseMatrix')
assert.ok(!src.includes("className: g ? 'iyh-glow'"), 'Stage 6: changed-cell glow class retired with dead PulseMatrix')
assert.ok(src.includes('iyh-bubble-travel'), 'bubble keyframes still present for PulseLanes (travel: bottom-edge -> destination cell, the rise keyframe was superseded by the measured-surface travel)')
assert.ok(src.includes("flex: narrow ? '1 1 30%' : undefined"),
  'drift wraps 3x2 in the narrow dock (labels to tooltip, numbers stay)')
assert.ok(src.includes("'data-testid': 'pulse-io-strip'"),
  'v0.1.12: the I/O readout is a compact bottom-strip ticker (pulse-io-strip, dual-purpose spawn band), not a 220px side column — the 4 lane boxes own the full width')
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

// 6. pulseBand: discrete 3-band encoding of the server cell.level.
eq(pulseBand({ level: 'strong' }), 'strong', 'strong level -> strong band')
eq(pulseBand({ level: 'weak' }), 'weak', 'weak level -> weak band')
eq(pulseBand({ level: 'neutral' }), 'empty', 'neutral -> empty band')
eq(pulseBand({ state: 'learning' }), 'empty', 'learning (no level) -> empty')
eq(pulseBand({ value: 0.9, state: 'ready' }), 'empty', 'value without level -> empty (level is the signal)')
eq(pulseBand(null), 'empty', 'null cell -> empty')
eq(pulseBand({}), 'empty', 'empty object -> empty')
assert.notStrictEqual(pulseBand({ level: 'neutral' }), pulseBand({ level: 'weak' }),
  'empty and weak are structurally different')

// 7. buildPulseVerdict: deterministic, digit-free, confidence-gated.
const vdRows = [
  { tool_name: 'alpha', momentum: { value: 0.9, state: 'surging' } },
  { tool_name: 'beta', momentum: { value: 0.5, state: 'steady' }, goal: { level: 'strong' } },
  { tool_name: 'gamma', momentum: { value: 0.5, state: 'steady' } },
]
const vd = buildPulseVerdict(vdRows)
eq(vd.compact, 'alpha surging ahead of baseline', 'compact = single top-row token')
assert.ok(vd.full.indexOf('alpha surging ahead of baseline') !== -1, 'full carries the surger clause')
assert.ok(vd.full.indexOf('beta reads strong on goal') !== -1, 'full carries the strong-read clause')
assert.ok(vd.full.indexOf('gamma') === -1, 'weak/empty row contributes no clause (gated)')
eq(buildPulseVerdict(vdRows).full, vd.full, 'verdict is deterministic')
eq(buildPulseVerdict([{ tool_name: 'q', momentum: { state: 'steady' } }]),
  { compact: '', full: '' }, 'all-quiet payload -> empty strings, no filler')
eq(buildPulseVerdict([]), { compact: '', full: '' }, 'empty rows -> empty strings')
const skeleton = buildPulseVerdict([
  { tool_name: 'zz', momentum: { state: 'surging' } },
  { tool_name: 'zz', momentum: { state: 'cooling' } },
  { tool_name: 'zz', momentum: { state: 'steady' }, good: { level: 'strong' } },
])
assert.ok(!/[0-9]/.test(skeleton.full), 'verdict template is word-only (no digits in the skeleton)')

// 8. pulseRecency (move 4): PULSE = NOW recency read; DRIFT owns the trend.
// Pure + deterministic: takes nowSec as an argument, never reads the clock.
const NOW = 1000000
eq(pulseRecency(null, NOW).word, '', 'null last_seen -> no word (honest absence)')
eq(pulseRecency(null, NOW).label, 'no activity yet', 'absence label')
eq(pulseRecency(0, NOW).word, '', 's <= 0 -> absent (never a fabricated recency)')
eq(pulseRecency(NOW - 30, NOW).word, 'now', '< 60s age -> now')
eq(pulseRecency(NOW - 300, NOW).word, 'recent', '< 3600s age -> recent')
eq(pulseRecency(NOW - 7200, NOW).word, 'today', '< 86400s age -> today')
eq(pulseRecency(NOW - 90000, NOW).word, 'stale', '>= 86400s age -> stale')
eq(pulseRecency(NOW + 100, NOW).word, 'now', 'future / clock-skew clamps to now')
eq(JSON.stringify(pulseRecency(NOW - 300, NOW)), JSON.stringify(pulseRecency(NOW - 300, NOW)),
  'pulseRecency is deterministic (same input -> same output)')
// Source pins: the recency read is wired into the pane (no canvas, no trend axis).
assert.ok(src.includes('pulseRecency'), 'recency helper wired')
assert.ok(src.includes("+ ' \u00b7 '"), 'expanded LIVE cell joins count + recency word')
assert.ok(src.includes('no activity yet'), 'honest absence for unseen tools')
console.log('ok ' + (++n) + ' - pulseRecency buckets (pure + deterministic)')

// 9. Stage 3: four independent lanes (data + first UI pass).
// The PULSE data seam (STAGE4 block) must prefer the native additive
// `lanes` API verbatim and only fall back to the legacy-rows mapping
// for hosts that predate the lanes field.
{
  const s4 = src.slice(src.indexOf('// STAGE4-START'), src.indexOf('// STAGE4-END'))
  assert.ok(s4.length > 0, 'STAGE4 markers found in plugin.js')
  const seam = new Function(s4 + '\n;return { adaptPulseToLanes };')().adaptPulseToLanes
  const eq3 = (a, e, label) => eq(a, e, label)
  const ok3 = (c, label) => { assert.ok(c, label); console.log('ok ' + (++n) + ' - ' + label) }
  const lanesSrc = src.slice(src.indexOf('function PulseLanes'), src.indexOf('function IoLine'))
  ok3(lanesSrc.includes("gridTemplateColumns: 'repeat(4, minmax(0, 1fr))'"), 'four equal-width lanes (repeat(4, minmax(0,1fr)))')
  ok3(lanesSrc.includes('slice(0, cap)'), 'every visible cell owns its truncated tool name (rows capped by the measured capacity, the fixed 12 was retired with the measured-box capacity)')
  ok3(lanesSrc.includes("'empty-' + i"), 'empty slots stay empty (no neighbor expansion)')
  ok3(!lanesSrc.includes('lbl-'), 'no separate tool-name rail inside PulseLanes')
  ok3(lanesSrc.includes('threshold ') && lanesSrc.includes('evidence: ') && lanesSrc.includes('support '),
    'tooltip/detail carries threshold + evidence source + support_count')
  ok3(!lanesSrc.includes('destination_slot'), 'no animation fields in the lane UI path')
  const pulseModuleSrc = src.slice(src.indexOf("'data-module': 'pulse'"), src.indexOf("'data-module': 'portrait'"))
  ok3(pulseModuleSrc.includes('PulseLanes'), 'compact PULSE renders the lanes surface')
  // native passthrough: independent ranking preserved verbatim
  const native = {
    schema_version: 2, available: true,
    lanes: {
      live: [{ tool_id: 12, tool_name: 'terminal', rank: 1, activity: { calls_started: 34, calls_completed: 32, errors: 2, first_seen: 1, last_seen: 2, recent_capsules: 8 }, momentum: { momentum: 0.18, surge: 0.18, value: 0.59, state: 'surging' } }],
      goal: [{ tool_id: 17, tool_name: 'github', rank: 1, value: 0.9, threshold: 0.8, level: 'strong', state: 'provisional', support: { count: 2, required: 3, ratio: 0.6667 }, evidence: { source: 'prototype_banks', prototype_count: 2, capsule_count: 4 } }],
      good: [{ tool_id: 12, tool_name: 'terminal', rank: 1, value: 0.8, threshold: 0.6, level: 'strong', state: 'ready', support: { count: 3, required: 3, ratio: 1 }, evidence_source: 'good_prototypes' }],
      friction: [{ tool_id: 21, tool_name: 'browser', rank: 1, value: 0.91, threshold: 0.4, level: 'strong', state: 'ready', dominant_source: 'objective', user: { value: 0.72, level: 'strong', state: 'provisional', support: { count: 1, required: 3, ratio: 0.3333 } }, objective: { value: 0.91, level: 'strong', state: 'ready', support: { count: 3, required: 3, ratio: 1 } } }],
    },
    rows: [], columns: ['goal', 'good', 'friction', 'unlabeled'],
  }
  const out = seam(native)
  eq3(out.lanes, native.lanes, 'native lanes pass through verbatim')
  eq3(out.lanes.goal[0].tool_name, 'github', 'GOAL ranks github first (independent of LIVE)')
  eq3(out.schema_version, 2, 'seam emits schema_version 2')
  eq3(out.lane_order, ['live', 'goal', 'good', 'friction'], 'lane_order verbatim')
  console.log('ok ' + (++n) + ' - native lanes passthrough')
  // legacy fallback still maps rows to ALL lanes
  const fb = seam({ columns: ['goal', 'good', 'friction', 'unlabeled'], rows: [
    { tool_id: 12, tool_name: 'terminal', live: { calls_started: 34, calls_completed: 32, errors: 2 }, momentum: { value: 0.59, state: 'surging' }, goal: { value: 0.86 }, good: { value: 0.5 }, friction: { value: 0.2, user: 0.1, objective: 0.2 } },
    { tool_id: 17, tool_name: 'github', live: { calls_started: 5, calls_completed: 5, errors: 0 }, momentum: { value: 0.5, state: 'steady' }, goal: { value: 0.7 }, good: { value: 0.73 }, friction: { value: 0.91, user: 0.72, objective: 0.91 } },
  ] })
  eq3(fb.lanes.live.length, 2, 'legacy fallback returns ALL tools per lane')
  eq3(fb.lanes.friction.find((e) => e.tool_name === 'github').dominant_source, 'objective', 'fallback friction dominant objective')
  eq3(seam(null).lanes.goal, [], 'null payload -> empty lanes, never omits lanes')
  // v0.1.11 presence floor (C): the meter itself carries "we have a state."
  //   An evaluated cell (state != 'empty') shows >=1 solid dot; an empty
  //   cell shows only the faint 0.15-opacity dots. B (bubble on state)
  //   already lives in detectPulseTransitions (reason 'state-change').
  //   The earlier leading state-lamp was dropped: it was sized like a meter
  //   dot, so it read as a phantom 13th dot (friction: 2 real dots + lamp =
  //   "3") and dropped a second color onto a monochrome meter.
  ok3(lanesSrc.includes('function dotsFor(value, evaluated, driftBase)'), 'dotsFor is state-aware + drift-aware (presence floor + drift order wired)')
  ok3(lanesSrc.includes('if (evaluated && filled < 1) filled = 1'),
    'C floor: an evaluated cell always shows >=1 meter dot (weak can not read as empty)')
  ok3(lanesSrc.includes('dotsFor(val, evaluated, (coord.getState().seq || 0) + i)'),
    'the meter gets evaluated + a deterministic drift base at the render site')
  ok3(lanesSrc.includes('pulseDriftMask(filled, driftBase, PULSE_DOT_DRIFT_MULT)'),
    'dotsFor places the lit dots in a deterministic drift order (pulseDriftMask)')
  ok3(lanesSrc.includes('opacity: (mask ? mask[i] : i < filled)'),
    'drift only reorders the lit slots; no driftBase keeps the sequential fill')
  ok3(lanesSrc.includes("val === null ? '–' : val.toFixed(2)"),
    'v0.1.16 narrow pane surfaces the 2-decimal score inline after the dots')
  ok3(!lanesSrc.includes('state-lamp'), 'no phantom leading lamp dot (presence is the floor, not a 13th dot)')
  ok3(!lanesSrc.includes('markerColor'), 'no separate marker color (one dot-language, no second hue)')
  // v0.1.13 honest meter: a ball only where a value EXISTS. 'learning' and
  // 'empty' both carry value=null -> no dots (no phantom progress); a
  // provisional/ready value (even weak) still gets the presence floor.
  ok3(lanesSrc.includes('var evaluated = val !== null'),
    'v0.1.13: the meter is value-driven (learning/empty null-value -> 0 dots)')
  ok3(!/var evaluated = e\.state \?/.test(lanesSrc),
    'the old state-keyed floor (state != empty -> 1 dot) is gone')
  console.log('ok ' + (++n) + ' - v0.1.11 presence floor (C) carried by the meter; phantom lamp dropped')
  // v0.1.12 PULSE space pass: each lane is a bordered box that fills its 1fr
  // column (fluid on resize), tiny padding, a reserved bubble-spawn strip at
  // the surface bottom; the I/O readout is a compact bottom-strip ticker.
  ok3(lanesSrc.includes("border: '1px solid var(--ui-border, var(--border, transparent))'"),
    'each lane is a bordered box (fills its 1fr column)')
  ok3(lanesSrc.includes('PULSE_BOARD_PAD'), 'lane boxes carry tiny internal padding (margins)')
  ok3(src.includes('PULSE_SPAWN_SPACE'), 'a reserved bubble-spawn strip is defined')
  ok3(src.includes('pulseReserve: PULSE_SPAWN_SPACE + PULSE_BOARD_CHROME'),
    'the Strip opts the spawn strip + box chrome into the row capacity')
  ok3(src.includes("'data-testid': 'pulse-io-strip'"),
    'I/O readout is the compact bottom-strip ticker (not a 220px side column)')
  // v0.1.12 bubble fix: activity whoop-ups land on the tool's DOMINANT visible
  // lane (spread across columns), not always 'live'; the momentum DATA commit
  // stays on the live lane via dataLane (V1 hold preserved).
  ok3(src.includes('function dominantLaneFor('),
    'activity bubbles route to the tool\'s most-prominent visible lane')
  ok3(!src.includes("metric: 'live', tool: ev.tool_name"),
    'activity no longer hard-codes metric:"live" (the one-column clustering bug)')
  ok3(src.includes("dataLane: 'live'"), 'the momentum commit stays on the live lane (visual spreads, data holds)')
  // v0.1.12 sharper good eval: the PULSE_DOT_SHARPNESS power curve is now
  // retired from the fill (retained as a constant) — superseded below.
  ok3(src.includes('PULSE_DOT_SHARPNESS'), 'the legacy sharpness constant is retained')
  // v0.1.14: the owner's X-space wave (the "bubbles count" formula) drives the
  // fill: dots = round(wave(value) * 12); the honest 0-dot floor for
  // value=null (learning/empty) is preserved.
  ok3(src.includes('function pulseDotWave('),
    'the owner X-space value->dots wave is defined')
  ok3(src.includes('Math.pow(v, 0.78)')
        && src.includes('0.420')
        && src.includes('0.500 * Math.cos(PI * u)'),
    'the wave uses the owner coefficients (0.420 base) + the x^0.78 X-space transform')
  ok3(lanesSrc.includes('pulseDotWave(v)'),
    'dotsFor fills the meter from the wave (round(wave * 12))')
  ok3(lanesSrc.includes('Math.round(shaped * 12)'),
    'the wave fraction scales to the 12-dot meter')
  // v0.1.12 distinct dequeued dots: a whoop-up is ONE per tool (its newest
  // event), not one per call — the cursor queue exists to surface a NEW
  // tool, not the same tool N times. activity() dedupes via the helper;
  // flushTool() emits a single newest-event whoop-up.
  ok3(src.includes('function distinctEventsByTool('), 'the one-per-tool dedup helper exists')
  ok3(src.includes('distinctEventsByTool(newEvents)'),
    'activity() collapses a batch to the LATEST event per tool (distinct whoop-ups)')
  ok3(src.includes('newest = ev; // first unconsumed = the newest'),
    'flushTool() emits ONE whoop-up (the newest), absorbing the tool\'s backlog')
  // v0.1.12 cut-off fix: the flight keyframes CSS is enumerated over LAYOUT
  // (lane x row), not the in-flight set, so a new bubble never re-injects
  // @keyframes and can't restart an in-flight staircase.
  ok3(src.includes('byte-stable across bubble arrivals'),
    'the @keyframes CSS is layout-only (pre-enumerated), so arrivals never restart in-flight flights')
  ok3(!/active5\.forEach\([^\n]*\)\s*\{[\s\S]*?dySeen\.indexOf/.test(src.slice(src.indexOf('Per-flight staircase keyframes'))),
    'the keyframes CSS no longer derives from the current active set')
  console.log('ok ' + (++n) + ' - v0.1.12: I/O bottom-strip + dominant-lane routing + sharper curve + distinct whoop-ups + no-cut-off flights')
}
console.log('PASS pulse: ' + n + ' assertions')
