// tests/test_desktop_composition.mjs — node test for Stage 4 measured
// capacity + surface compositions + PULSE data seam.
// Extracts the pure STAGE4 block from desktop/plugin.js and asserts:
// composition selection per (width,height) matrix, coexistence
// (activation never hides sibling), capacity recalculation on resize,
// readability floor (rows reduce before text shrinks), seam shape
// conformance to the v2 TS types (snapshot for a fixture).
// Run: node tests/test_desktop_composition.mjs
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const dir = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(dir, '..', 'desktop', 'plugin.js'), 'utf8')
const block = src.slice(src.indexOf('// STAGE4-START'), src.indexOf('// STAGE4-END'))
assert.ok(block.length > 0, 'STAGE4 markers found in plugin.js')
const fns = new Function(block +
  '\n;return { chooseComposition, visibleCapacity, capacityForComposition, nextFocusFor, adaptPulseToLanes };')()
const { chooseComposition, visibleCapacity, capacityForComposition, nextFocusFor, adaptPulseToLanes } = fns

let n = 0
const eq = (actual, expected, label) => {
  assert.deepEqual(actual, expected, label + ' (got ' + JSON.stringify(actual) + ')')
  console.log('ok ' + (++n) + ' - ' + label)
}
const ok = (cond, label) => { assert.ok(cond, label); console.log('ok ' + (++n) + ' - ' + label) }

// 1. Composition selection per (width,height) matrix.
eq(chooseComposition(1200, 96), 'bottom', 'wide-shallow -> bottom')
eq(chooseComposition(1920, 120), 'bottom', 'wide-shallow 1920x120 -> bottom')
eq(chooseComposition(400, 800), 'sidebar', 'tall-narrow 400x800 -> sidebar')
eq(chooseComposition(400, 600), 'sidebar', 'tall-narrow 400x600 -> sidebar')
eq(chooseComposition(300, 96), 'bottom', 'narrow-shallow stays bottom (height wins over width)')
eq(chooseComposition(480, 110), 'bottom', 'narrow-shallow 480x110 stays bottom')
eq(chooseComposition(400, 60), 'bottom', 'very-short allocation stays bottom and readable')
eq(chooseComposition(400, 500), 'sidebar', 'just above shallow gate + tall ratio 500/400=1.25 -> sidebar')
eq(chooseComposition(800, 600), 'bottom', 'wide-tall 800x600 -> bottom (not narrow)')
eq(chooseComposition(0, 0), 'bottom', 'unknown box -> bottom (registered surface)')
eq(chooseComposition(NaN, 100), 'bottom', 'non-finite -> bottom')
eq(chooseComposition(400, 500), 'sidebar', 'right-zone pane ratio 500/400=1.25 -> sidebar')

// 2. Capacity recalculation on resize: floor((h+gap)/pitch) clamped.
eq(visibleCapacity(48), 3, 'default 48px -> floor(50/16)=3')
eq(visibleCapacity(32), 2, '32px -> floor(34/16)=2 (resize reduces rows)')
eq(visibleCapacity(14), 1, 'tiny -> clamped to min 1 (readable, never 0)')
eq(visibleCapacity(200), 3, 'large -> clamped to max 3 (bottom)')
eq(visibleCapacity(48, { maxCap: 4 }), 3, 'sidebar max 4 still yields 3 at 48px')
eq(capacityForComposition({ width: 1200, height: 96 }, 'bottom'), 3, 'bottom box 1200x96 -> cap 3')
eq(capacityForComposition({ width: 1200, height: 80 }, 'bottom'), 2, 'resized shorter bottom -> cap 2 (recalc)')
eq(capacityForComposition({ width: 400, height: 800 }, 'sidebar'), capacityForComposition({ width: 400, height: 800 }, 'sidebar'), 'sidebar deterministic')
const cap800 = capacityForComposition({ width: 400, height: 800 }, 'sidebar')
const cap400 = capacityForComposition({ width: 400, height: 400 }, 'sidebar')
ok(cap800 >= cap400 && cap800 >= 1 && cap400 >= 1, 'taller sidebar after resize -> capacity recalculates up/equal (got ' + cap800 + ' vs ' + cap400 + ')')
ok(capacityForComposition({ width: 1200, height: 96 }, 'bottom') === capacityForComposition({ width: 1200, height: 96 }, 'bottom'), 'same box -> same capacity (shared across all four lanes)')

// 3. Readability floor: rows reduce BEFORE text shrinks.
// The pitch (16) is the floor; font/dots are fixed in PulseLanes.
// PulseLanes keeps fixed readout sizes; capacity shrinks rows only.
const lanesSrc = src.slice(src.indexOf('function PulseLanes'), src.indexOf('function IoLine'))
ok(lanesSrc.includes('fontSize: 9') && lanesSrc.includes('width: 4, height: 4'), 'readability floor fixed sizes present (font 9, dots 4px)')
ok(!lanesSrc.includes('fontSize: 7'), 'PulseLanes never shrinks below the readability floor (no fontSize 7)')
// PulseLanes slices every lane by the SAME capacity.
ok(src.includes('capacity') && src.includes('lanes[lane].slice(0, cap)'), 'all lanes slice by shared capacity')

// 4. Coexistence: activation never hides sibling.
eq(nextFocusFor('pulse', 'portrait'), 'portrait', 'activating PORTRAIT moves focus to portrait')
eq(nextFocusFor('portrait', 'pulse'), 'pulse', 'activating PULSE moves focus to pulse')
eq(nextFocusFor('pulse', 'pulse'), 'pulse', 're-activating same keeps focus')
eq(nextFocusFor('pulse', 'driftXYZ'), 'pulse', 'invalid activation keeps current (never nulls/hides)')
ok(src.includes("'data-module': 'pulse'") && src.includes("'data-module': 'portrait'"), 'both modules always mounted (data-module attrs)')
ok(src.includes("'data-composition': 'bottom'") && src.includes("'data-composition': 'sidebar'"), 'both compositions rendered')
ok(src.includes("'data-focus': tab === 'pulse'") && src.includes("'data-focus': tab === 'portrait'"), 'focus outline is the only tab effect')
ok(src.includes('activateFocus'), 'header controls call activateFocus')
ok(src.includes("data-focus"), 'focus outline keys off tab state (no exclusive hiding)')
// The only allowed tab=== remaining is the focus outline/expanded workspace.
ok(src.includes("TABS.filter((t) => t.id !== 'drift')") || src.includes("compactTabs"), 'compact tabs exclude DRIFT (DRIFT stays expanded)')
ok(src.includes("placement: 'main'") && src.includes("dock: { pane: 'workspace', pos: 'bottom' }") && src.includes("height: '96px'"), 'bottom registration verbatim')
// Sidebar surface (the user's working view): right-zone pane on the
// same measured Strip, gated on host signals (older hosts degrade to
// bottom-only).
ok(src.includes("placement: 'right'") && src.includes("width: '400px'"), 'right-zone sidebar registration present (user working surface)')
ok(src.includes("typeof host.paneVisibility === 'function'") && src.includes("typeof host.state.viewport"), 'sidebar registration gated on host signals')
ok((src.match(/ctx\.register\(\{/g) || []).length === 2, 'two registrations: bottom instrument + right sidebar (same surface model)')

// 5. Seam shape conformance to v2 TS types (snapshot for a fixture).
const fixture = {
  columns: ['goal', 'good', 'friction', 'unlabeled'],
  rows: [
    { tool_id: 12, tool_name: 'terminal', live: { calls_started: 34, calls_completed: 32, errors: 2, first_seen: 1794230100, last_seen: 1794231600 }, momentum: { value: 0.59, state: 'surging', momentum: 0.18, surge: 0.18 }, goal: { value: 0.86 }, good: { value: 0.5 }, friction: { value: 0.2, user: 0.1, objective: 0.2 } },
    { tool_id: 17, tool_name: 'github', live: { calls_started: 5, calls_completed: 5, errors: 0, first_seen: 1794230000, last_seen: 1794231000 }, momentum: { value: 0.5, state: 'steady' }, goal: { value: 0.7 }, good: { value: 0.73 }, friction: { value: 0.91, user: 0.72, objective: 0.91 } },
    { tool_id: 21, tool_name: 'browser', live: { calls_started: 0, calls_completed: 0, errors: 0 }, goal: null, good: null, friction: null },
  ],
}
const v2 = adaptPulseToLanes(fixture)
eq(v2.schema_version, 2, 'seam emits schema_version 2')
eq(v2.available, true, 'available true with rows')
eq(v2.lane_order, ['live', 'goal', 'good', 'friction'], 'lane_order verbatim')
ok(Array.isArray(v2.lanes.live) && Array.isArray(v2.lanes.goal) && Array.isArray(v2.lanes.good) && Array.isArray(v2.lanes.friction), 'four lanes present')
eq(v2.lanes.live.length, 3, 'live lane returns ALL tools (desktop picks prefix)')
eq(v2.lanes.live[0].tool_name, 'terminal', 'live rank 1 = most active')
eq(v2.lanes.live[0].rank, 1, 'live rank one-based')
ok(typeof v2.lanes.live[0].tool_id === 'number', 'tool_id numeric')
ok(v2.lanes.live[0].activity && typeof v2.lanes.live[0].activity.calls_started === 'number', 'live entry has activity')
ok(['surging', 'steady', 'cooling'].includes(v2.lanes.live[0].momentum.state), 'momentum state enum')
const g = v2.lanes.goal.find((e) => e.tool_name === 'terminal')
eq(g.value, 0.86, 'goal value preserved')
eq(g.threshold, 0.8, 'goal threshold 0.8')
eq(g.level, 'strong', 'goal level strong at 0.86 >= 0.8')
ok(['empty', 'learning', 'provisional', 'ready'].includes(g.state), 'goal state enum')
ok(g.support && g.support.count === null && g.support.required === null && g.support.ratio === null,
  'legacy support is UNKNOWN (nulls) — never a fabricated 0-of-3 (review fix 9)')
ok(g.compat === 'legacy', 'legacy entries carry the compat marker')
ok(g.state === 'learning', 'unknown evidence cannot claim provisional')
ok(g.evidence && ['prototype_banks', 'recent_capsules', 'none'].includes(g.evidence.source), 'goal evidence source enum')
const gd = v2.lanes.good.find((e) => e.tool_name === 'github')
eq(gd.threshold, 0.6, 'good threshold 0.6')
eq(gd.value, 0.73, 'good value preserved')
ok('evidence_source' in gd, 'good carries evidence_source')
const fr = v2.lanes.friction.find((e) => e.tool_name === 'github')
eq(fr.threshold, 0.4, 'friction threshold 0.4')
eq(fr.value, 0.91, 'friction combined = max user/objective')
eq(fr.dominant_source, 'objective', 'dominant_source = objective (0.91 > 0.72)')
ok(fr.user && fr.objective && typeof fr.user.value === 'number' && typeof fr.objective.value === 'number', 'friction keeps user/objective separate')
eq(adaptPulseToLanes({ rows: [], columns: [] }).available, false, 'no tools -> available false')
eq(adaptPulseToLanes(null).lanes.goal, [], 'null payload -> empty lanes, never omits lanes')

// 6. Stage 6 gap-fill (§15 criterion 11): sidebar proportional shrink.
// Re-baselined after the v13–v19 portrait brick-mosaic work: PORTRAIT
// became the content-sized working surface — a FIXED '0 0 auto' module
// (template picker + canvas + evidence row) in BOTH compositions, while
// PULSE takes the remaining space with '1 1 0%' in sidebar mode
// (flex: 1 in bottom). The v0.1.10 review-fix-7 'pulse fixed / portrait
// flexes' split was inverted when the portrait became the surface.
ok(src.includes("flex: composition === 'sidebar' ? '1 1 0%' : 1"),
  'sidebar pulse module flexes in sidebar mode (takes the remaining space; portrait is content-sized)')
ok(src.includes("flex: '0 0 auto'"),
  'sidebar portrait module is fixed 0 0 auto (content-sized canvas, v18+)')
ok(!/flex: composition === 'sidebar' \? '1 1 50%'/.test(src), 'no functional 50%-basis flex left (the 75/25 split is gone)')
ok(src.includes("'data-composition': 'sidebar'"), 'sidebar composition hook present')
ok(src.includes("className: 'flex flex-1 flex-col'") && src.includes("gap: 4, minHeight: 0, overflow: 'hidden'"),
  'sidebar column flexes vertically with minHeight 0 (no large min-heights pushing other plugins out)')
ok(src.includes("'data-composition': 'bottom'"), 'bottom composition hook present')

// 7. v0.1.10 (review fixes 7/9): the two-row header is a SIDEBAR-only
// layout (composition decides), and capacity has a measured path with
// no chrome estimate.
ok(src.includes("narrow && composition === 'sidebar'"),
  'two-row header only in sidebar composition (a narrow bottom strip keeps the shallow header)')
eq(capacityForComposition({ width: 400, height: 48 }, 'bottom', { measured: true }), 3,
  'measured 48px PULSE module -> cap 3 (no 48px chrome subtracted from it)')
eq(capacityForComposition({ width: 400, height: 16 }, 'bottom', { measured: true }), 1,
  'measured 16px module -> single row (fits exactly one 14px cell)')
ok(capacityForComposition({ width: 400, height: 48 }, 'bottom', { measured: true })
  === capacityForComposition({ width: 400, height: 48 }, 'sidebar', { measured: true }),
  'a measured box is the PULSE module itself: no composition halving applies')

// 8. v0.1.10 (review fix 9): the adapter's available flag + the
// restrained empty state reach the lanes renderer.
const lanesCall = src.slice(src.indexOf('jsx(PulseLanes, {'), src.indexOf('}),', src.indexOf('jsx(PulseLanes, {')))
ok(lanesCall.includes('lanes: pulseV2.lanes') && lanesCall.includes('available: pulseV2.available'),
  'Strip passes lanes AND the available flag to PulseLanes')
const lanesSrc2 = src.slice(src.indexOf('function PulseLanes'), src.indexOf('function IoLine'))
ok(lanesSrc2.includes("'data-testid': 'pulse-empty'"), 'PulseLanes renders the restrained empty state')
ok(lanesSrc2.includes("'no pulse data yet'"), 'empty state labels the absence of data')
ok(lanesSrc2.includes('hasEntries'), 'empty state keeps lane geometry (rendered only when all lanes are empty)')

// 9. Expand/collapse symmetry: the compact strip's expand icon opens the
// inspection workspace; the EXPANDED view carries the inverse button
// (minimize glyph) that routes through the host close handle openWorkspace
// returns — back to the compact panel (strip/sidebar).
ok(src.includes('let IYH_WS_CLOSE = null') && src.includes('function closeIyhWorkspace()'),
  'module-level workspace close handle + guarded close function')
ok(src.includes('if (typeof handle === \'function\') IYH_WS_CLOSE = handle'),
  'openWorkspace\'s returned close handle is captured')
ok(src.includes('onCollapse: closeIyhWorkspace'), 'ExpandedWorkspace receives the collapse callback')
const wsSrc = src.slice(src.indexOf('function ExpandedWorkspace'), src.indexOf('const TABS'))
ok(wsSrc.includes('name: \'minimize\'') && wsSrc.includes('onCollapse'),
  'the expanded header offers the minimize (inverse of expand) button')
ok(wsSrc.includes('typeof onCollapse === \'function\''), 'the button degrades away when no close path exists')

console.log('PASS composition: ' + n + ' assertions')
