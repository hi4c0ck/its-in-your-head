// tests/test_desktop_bubble.mjs — Stage 5 bubble-arrival transition.
// Extracts the pure STAGE5 block and asserts plan §14 bubble rows:
// first load static; sub-threshold noise never animates; exactly one
// transition per meaningful change; rank change shows old name in new
// slot until arrival; ordered bounded multi-change (cap/stagger);
// stale superseded; resize cancels; reduced-motion immediate commit.
// Run: node tests/test_desktop_bubble.mjs
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const dir = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(dir, '..', 'desktop', 'plugin.js'), 'utf8')
const block = src.slice(src.indexOf('// STAGE5-START'), src.indexOf('// STAGE5-END'))
assert.ok(block.length > 0, 'STAGE5 markers found')
const api = new Function(block + `\n;return { PULSE_HYSTERESIS, BUBBLE_TRAVEL_MS, BUBBLE_ARRIVAL_MS,
  MAX_CONCURRENT_BUBBLES, BUBBLE_STAGGER_MS, pulseQuantizedScore, pulseSemanticId, pulseSlotId,
  pulseSnapshotFromLanes, detectPulseTransitions, pulseBubblePath, prefersReducedMotion,
  createPulseBubbleCoordinator };`)()
const { PULSE_HYSTERESIS, BUBBLE_TRAVEL_MS, BUBBLE_ARRIVAL_MS, MAX_CONCURRENT_BUBBLES,
  BUBBLE_STAGGER_MS, pulseQuantizedScore, pulseSemanticId, pulseSlotId,
  pulseSnapshotFromLanes, detectPulseTransitions, pulseBubblePath, prefersReducedMotion,
  createPulseBubbleCoordinator } = api

let n = 0
const eq = (a, e, label) => { assert.deepEqual(a, e, label + ' (got ' + JSON.stringify(a) + ')'); console.log('ok ' + (++n) + ' - ' + label) }
const ok = (c, label) => { assert.ok(c, label); console.log('ok ' + (++n) + ' - ' + label) }

const entry = (id, name, rank, value, level = 'weak', state = 'provisional') =>
  ({ tool_id: id, tool_name: name, rank, value, threshold: 0.8, level, state, support: { count: 1, required: 3, ratio: 1 / 3 } })
const lanesOf = (goal) => ({ live: [], goal, good: [], friction: [] })

// Tuning constants within plan ranges (350-550 / 120-180), named.
ok(BUBBLE_TRAVEL_MS >= 350 && BUBBLE_TRAVEL_MS <= 550, 'travel tuning 350-550ms (got ' + BUBBLE_TRAVEL_MS + ')')
ok(BUBBLE_ARRIVAL_MS >= 120 && BUBBLE_ARRIVAL_MS <= 180, 'arrival tuning 120-180ms (got ' + BUBBLE_ARRIVAL_MS + ')')
ok(MAX_CONCURRENT_BUBBLES >= 1 && MAX_CONCURRENT_BUBBLES <= 8, 'bounded concurrent cap present (got ' + MAX_CONCURRENT_BUBBLES + ')')
ok(BUBBLE_STAGGER_MS > 0, 'stagger tuning present')
eq(PULSE_HYSTERESIS, 1e-6, 'hysteresis mirrors pulse/matrix.py HYSTERESIS = 1e-6 (no second threshold)')
eq(pulseQuantizedScore(0.5 + 4e-7), pulseQuantizedScore(0.5), 'quantized bucket: sub-hysteresis noise shares bucket')
ok(pulseQuantizedScore(0.5 + 2e-6) !== pulseQuantizedScore(0.5), 'quantized bucket: above-hysteresis change differs')

// Identities.
eq(pulseSemanticId('goal', { tool_id: 12 }), 'goal|12', 'semantic identity metric|tool')
eq(pulseSlotId('goal', 0), 'goal|0', 'visual slot identity metric|slotIndex')

// First load static: hydration never animates.
{
  const c = createPulseBubbleCoordinator()
  const r = c.ingest(lanesOf([entry(12, 'terminal', 1, 0.86, 'strong')]), 3)
  eq(r.transitions, [], 'first load (hydration) creates zero transitions')
  eq(r.active, [], 'first load: no active bubbles')
  eq(c.visibleLanes(3).goal.map(e => e.tool_name), ['terminal'], 'first load committed visible immediately')
}

// Sub-threshold noise never animates.
{
  const c = createPulseBubbleCoordinator()
  c.ingest(lanesOf([entry(12, 'terminal', 1, 0.8600000, 'strong')]), 3)
  const r = c.ingest(lanesOf([entry(12, 'terminal', 1, 0.86000004, 'strong')]), 3)
  eq(r.transitions, [], 'value change below hysteresis bucket -> no transition')
  eq(r.active, [], 'sub-threshold noise: no bubble')
}

// Exactly one transition per meaningful change; committed stays until arrival.
{
  const c = createPulseBubbleCoordinator()
  c.ingest(lanesOf([entry(12, 'terminal', 1, 0.5, 'weak')]), 3)
  const r = c.ingest(lanesOf([entry(12, 'terminal', 1, 0.9, 'strong')]), 3)
  eq(r.transitions.length, 1, 'one meaningful value/level change -> exactly one transition')
  const t = r.transitions[0]
  eq(t.reason, 'level-change', 'level change reason reported')
  eq(t.metric, 'goal', 'transition metric')
  eq(t.tool, 'terminal', 'transition tool = semantic identity name')
  eq(t.destinationSlot, 0, 'destination slot = next rank - 1')
  eq(t.slotId, 'goal|0', 'transition slot identity')
  eq(t.previousValue, 0.5, 'previous value carried'); eq(t.nextValue, 0.9, 'next value carried')
  eq(c.visibleLanes(3).goal[0].value, 0.5, 'committed (old) content stays visible during travel')
  const arr = c.arrive(t.sequence)
  ok(arr.arrived, 'arrival commits')
  eq(c.visibleLanes(3).goal[0].value, 0.9, 'pending content committed on arrival')
  eq(c.getState().flashSlotId, 'goal|0', 'arrival flash targets destination slot')
  c.clearFlash(); eq(c.getState().flashSlotId, null, 'flash clears after arrival window')
  // Same poll again -> no duplicate.
  const r2 = c.ingest(lanesOf([entry(12, 'terminal', 1, 0.9, 'strong')]), 3)
  eq(r2.transitions, [], 'repeat poll of same snapshot -> no duplicate transition')
}

// Rank change: NEW slot shows OLD name until arrival.
{
  const c = createPulseBubbleCoordinator()
  const before = lanesOf([entry(12, 'terminal', 1, 0.9, 'strong'), entry(17, 'github', 2, 0.7, 'weak')])
  c.ingest(before, 3)
  const after = lanesOf([entry(17, 'github', 1, 0.95, 'strong'), entry(12, 'terminal', 2, 0.9, 'strong')])
  const r = c.ingest(after, 3)
  ok(r.transitions.some(t => t.reason === 'rank-change' && t.tool === 'github'), 'rank change detected for github')
  const gh = r.transitions.find(t => t.tool === 'github')
  eq(gh.destinationSlot, 0, 'rank change targets the NEW slot (slot 0)')
  eq(c.visibleLanes(3).goal[0].tool_name, 'terminal', 'new slot still shows OLD name until arrival')
  c.arrive(gh.sequence)
  eq(c.visibleLanes(3).goal[0].tool_name, 'github', 'after arrival, new slot shows arriving tool')
}

// Ordered bounded multi-change: cap respected, queue ordered, stagger named.
{
  const c = createPulseBubbleCoordinator()
  const mk = (k) => Array.from({ length: k }, (_, i) => entry(100 + i, 'tool' + i, i + 1, 0.5, 'weak'))
  c.ingest(lanesOf(mk(6)), 6)
  const next = Array.from({ length: 6 }, (_, i) => entry(100 + i, 'tool' + i, i + 1, 0.9, 'strong'))
  const r = c.ingest(lanesOf(next), 6)
  eq(r.transitions.length, 6, 'six simultaneous meaningful changes -> six transitions')
  ok(r.active.length <= MAX_CONCURRENT_BUBBLES, 'concurrent bubbles bounded by cap (active=' + r.active.length + ')')
  eq(r.active.length + r.queued.length, 6, 'overflow queued, none dropped')
  const seqs = [...r.active, ...r.queued].map(t => t.sequence)
  const sorted = [...seqs].sort((a, b) => a - b)
  eq(seqs, sorted, 'multi-change ordered by sequence (stagger ' + BUBBLE_STAGGER_MS + 'ms apart)')
  // Arrival promotes queued in order.
  const first = r.active[0]
  c.arrive(first.sequence)
  ok(c.getState().active.length <= MAX_CONCURRENT_BUBBLES, 'promotion keeps cap after arrival')
}

// Stale transition superseded by newer ranking while in flight.
{
  const c = createPulseBubbleCoordinator()
  c.ingest(lanesOf([entry(12, 'terminal', 1, 0.5, 'weak')]), 3)
  const r1 = c.ingest(lanesOf([entry(12, 'terminal', 1, 0.7, 'weak')]), 3)
  const s1 = r1.transitions[0].sequence
  const r2 = c.ingest(lanesOf([entry(12, 'terminal', 1, 0.9, 'strong')]), 3)
  const s2 = r2.transitions[0].sequence
  ok(s2 > s1, 'newer ranking creates newer sequence')
  eq(c.getState().active.filter(t => t.semanticId === 'goal|12').length, 1, 'only one in-flight transition per semantic identity (superseded)')
  eq(c.getState().active[0].sequence, s2, 'in-flight is the newest, stale one removed')
  eq(c.arrive(s1).arrived, false, 'stale sequence cannot commit')
  c.arrive(s2)
  eq(c.visibleLanes(3).goal[0].value, 0.9, 'newest pending value commits, not stale')
}

// Resize cancels travel (no commit to obsolete coordinate).
{
  const c = createPulseBubbleCoordinator()
  c.ingest(lanesOf([entry(12, 'terminal', 1, 0.5, 'weak')]), 3)
  c.ingest(lanesOf([entry(12, 'terminal', 1, 0.9, 'strong')]), 3)
  eq(c.getState().active.length, 1, 'bubble in flight before resize')
  const rs = c.resize({ width: 400, height: 800 })
  eq(rs.cancelled, 1, 'resize cancels in-flight bubble')
  eq(c.getState().active, [], 'no active bubbles after resize')
  eq(c.visibleLanes(3).goal[0].value, 0.9, 'resize commits latest immediately (no obsolete-coordinate landing)')
}

// Reduced motion: immediate commit, no travel.
{
  eq(prefersReducedMotion({ matches: true }), true, 'matchMedia reduced-motion detected')
  eq(prefersReducedMotion({ matches: false }), false, 'matchMedia no-preference')
  eq(prefersReducedMotion(null), false, 'no matchMedia -> not reduced (safe default)')
  const c = createPulseBubbleCoordinator()
  c.ingest(lanesOf([entry(12, 'terminal', 1, 0.5, 'weak')]), 3)
  const r = c.ingest(lanesOf([entry(12, 'terminal', 1, 0.9, 'strong')]), 3, { reducedMotion: true })
  eq(r.immediate, true, 'reduced-motion: immediate commit flag')
  eq(r.active, [], 'reduced-motion: no travel')
  eq(c.visibleLanes(3).goal[0].value, 0.9, 'reduced-motion: pending visible without waiting')
}

// Surface-relative coordinates: bottom edge spawn, no hardcoded dims.
{
  const p = pulseBubblePath({ width: 800, height: 96 }, 'good', 1, 3)
  eq(p.spawn.y, 96, 'spawn at surface bottom edge (y = height)')
  ok(p.target.y < p.spawn.y, 'target above spawn (travels upward)')
  ok(p.target.x > 0 && p.target.x < 800, 'target inside surface width')
  const q = pulseBubblePath({ width: 400, height: 800 }, 'good', 1, 3)
  eq(q.spawn.y, 800, 'tall sidebar surface: spawn scales with measured height (no hardcode)')
  ok(q.target.x !== p.target.x, 'coordinates derive from the measured box, not constants')
}

// Source pins: wired into PulseLanes, UI-only (no backend schema fields).
{
  const lanesSrc = src.slice(src.indexOf('function PulseLanes'), src.indexOf('function IoLine'))
  ok(lanesSrc.includes('createPulseBubbleCoordinator'), 'PulseLanes owns the coordinator')
  ok(lanesSrc.includes('prefers-reduced-motion'), 'reduced-motion read via matchMedia in PulseLanes')
  ok(lanesSrc.includes('pulse-bubble') && lanesSrc.includes('pulseBubblePath'), 'bubble overlay rendered surface-relative')
  ok(lanesSrc.includes('BUBBLE_TRAVEL_MS') && lanesSrc.includes('BUBBLE_ARRIVAL_MS'), 'named timing constants used in PulseLanes')
  ok(!lanesSrc.includes('destination_slot'), 'no destination_slot in lane UI (desktop resolves slot, backend schema clean)')
  ok(src.includes('iyh-bubble-rise'), 'bubble keyframes present')
  ok(!/fetch\(|XMLHttpRequest/.test(block), 'transition layer is pure UI (no I/O)')
  // Imports invariant: only SDK + react.
  const imports = src.match(/^import .*$/gm) || []
  ok(imports.every(l => l.includes('@hermes/plugin-sdk') || l.includes('react')), 'desktop imports only SDK + react (got ' + imports.join('; ') + ')')
  ok(!/animation_ms|travel_ms/.test(block), 'no animation fields added to any backend-shaped data')
}
console.log('PASS bubble: ' + n + ' assertions')
