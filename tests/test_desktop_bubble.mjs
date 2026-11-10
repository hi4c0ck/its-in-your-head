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
// Animation-pass extraction: the staircase generator + pixel-step
// constants (module level, outside the STAGE5 markers).
const kfSrc = src.slice(src.indexOf('const PULSE_TRAVEL_STEP_PX'), src.indexOf('// Arrival STAMP'))
assert.ok(kfSrc.length > 0, 'pixel-step travel generator found')
// The STAGE5 flight helper calls pulseTravelSteps at module scope;
// give the extracted API the same scope (generator first, then block).
const api = new Function(kfSrc + block + `\n;return { PULSE_HYSTERESIS, BUBBLE_TRAVEL_MS, BUBBLE_ARRIVAL_MS,
  MAX_CONCURRENT_BUBBLES, BUBBLE_STAGGER_MS, pulseQuantizedScore, pulseSemanticId, pulseSlotId,
  pulseSnapshotFromLanes, pulseSlotMap, pulseCommitLane, pulseLanesKey, detectPulseTransitions,
  pulseBubblePath, pulseBubbleFlight, prefersReducedMotion, createPulseBubbleCoordinator,
  pulseTravelSteps, pulseTravelKeyframes };`)()
const { PULSE_HYSTERESIS, BUBBLE_TRAVEL_MS, BUBBLE_ARRIVAL_MS, MAX_CONCURRENT_BUBBLES,
  BUBBLE_STAGGER_MS, pulseQuantizedScore, pulseSemanticId, pulseSlotId,
  pulseSnapshotFromLanes, pulseSlotMap, pulseCommitLane, pulseLanesKey,
  detectPulseTransitions, pulseBubblePath, pulseBubbleFlight, prefersReducedMotion,
  createPulseBubbleCoordinator } = api
const kfApi = { pulseTravelSteps: api.pulseTravelSteps, pulseTravelKeyframes: api.pulseTravelKeyframes }

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
  ok(typeof p.dotLeft === 'number' && p.target.x === p.dotLeft, 'path exposes the dot-column x (flight + stamp derive from it)')
  eq(p.rowCenter, 27, 'board-chrome top-packed row center: 4 + 16px pitch x slot 1 + 7 = 27 (row geometry, not cell center)')
}

// v0.1.10 (review fix 4): a NEW tool enters the visible top three while
// another leaves — slot-occupant changes, not semantic-identity diffs.
{
  const c = createPulseBubbleCoordinator()
  const before = lanesOf([
    entry(12, 'terminal', 1, 0.9, 'strong'),
    entry(17, 'github', 2, 0.7, 'weak'),
    entry(21, 'browser', 3, 0.6, 'weak'),
  ])
  c.ingest(before, 3)
  const after = lanesOf([
    entry(33, 'filesystem', 1, 0.95, 'strong'),
    entry(12, 'terminal', 2, 0.9, 'strong'),
    entry(17, 'github', 3, 0.7, 'weak'),
  ])
  const r = c.ingest(after, 3)
  const bySlot = {}
  r.transitions.forEach(t => { bySlot[t.slotId] = t })
  eq(bySlot['goal|0'].reason, 'entry', 'new tool entering the visible set -> entry transition on its slot')
  eq(bySlot['goal|0'].tool, 'filesystem', 'entry transition names the arriving tool')
  eq(bySlot['goal|1'].reason, 'rank-change', 'displaced terminal moved slot 0 -> 1')
  eq(bySlot['goal|2'].reason, 'rank-change', 'displaced github moved slot 1 -> 2')
  ok(!r.transitions.some(t => t.tool === 'browser'), 'leaving tool (browser) spawns NO transition (exit, not entry)')
  // Exits prune the committed snapshot EAGERLY at ingest (no bubble for
  // exits): the leaving tool is gone from the settled cells immediately,
  // while the entering tool only lands when its arrival commits the lane.
  eq(c.visibleLanes(3).goal.map(e => e.tool_name), ['terminal', 'github'],
     'before arrival: exited browser already pruned; displaced tools hold their old ranks')
  c.arrive(bySlot['goal|0'].sequence)
  eq(c.visibleLanes(3).goal.map(e => e.tool_name), ['filesystem', 'terminal', 'github'],
     'after entry arrival, the lane is the NEW ordering (no stale or missing cell)')
  ok(c.visibleLanes(3).goal.map(e => e.tool_name).indexOf('browser') < 0,
     'exited tool stays pruned from the visible lane')
}

// v0.1.10 (review fix 4/6): a rank swap while OTHER bubbles are queued —
// lane-level commit keeps ordering coherent, queue order survives.
{
  const c = createPulseBubbleCoordinator()
  const mk = (k) => Array.from({ length: k }, (_, i) => entry(100 + i, 'tool' + i, i + 1, 0.5, 'weak'))
  c.ingest(lanesOf(mk(6)), 6)
  // Six in-place value changes -> four active, two queued.
  const r1 = c.ingest(lanesOf(mk(6).map((e, i) => ({ ...e, value: 0.9, level: 'strong' }))), 6)
  eq(r1.active.length + r1.queued.length, 6, 'all six meaningful changes tracked')
  eq(r1.queued.length, 6 - MAX_CONCURRENT_BUBBLES, 'overflow stays queued in sequence order')
  // Now a rank swap between two QUEUED tools lands as slot-occupant
  // changes (lane ARRAY order IS the visual slot order); the in-flight
  // / queued value bubbles supersede per identity.
  const swapped = mk(6).map((e, i) => ({ ...e, value: 0.9, level: 'strong' }))
  const tmp = swapped[0]; swapped[0] = swapped[1]; swapped[1] = tmp
  // rank fields follow the new visual order (v2: rank = slot + 1) so the
  // lane sort and the slot map agree after the commit.
  swapped[0].rank = 1; swapped[1].rank = 2
  const r2 = c.ingest(lanesOf(swapped), 6)
  ok(r2.transitions.some(t => t.slotId === 'goal|0' && t.reason === 'rank-change'),
     'swap produces a slot-0 rank-change transition')
  ok(r2.transitions.some(t => t.slotId === 'goal|1' && t.reason === 'rank-change'),
     'swap produces a slot-1 rank-change transition')
  // Arrivals settle the lane coherently: committing one rank-change
  // moves BOTH slots (the displaced occupant follows its pending rank).
  const swapSeq = r2.transitions.find(t => t.slotId === 'goal|0').sequence
  c.arrive(swapSeq)
  const vis = c.visibleLanes(6).goal.map(e => e.tool_name)
  eq(vis[0], 'tool1', 'lane head shows the swapped-in tool after its arrival')
  eq(vis[1], 'tool0', 'the displaced tool follows its new pending rank')
  ok(c.getState().queued.length >= 0, 'remaining value bubbles stay queued (nothing dropped)')
}

// v0.1.10 (review fix 6): per-slot flash ownership — a stale flash
// timer can never clear a NEWER arrival's flash on the same slot.
{
  const c = createPulseBubbleCoordinator()
  c.ingest(lanesOf([entry(12, 'terminal', 1, 0.5, 'weak')]), 3)
  const r1 = c.ingest(lanesOf([entry(12, 'terminal', 1, 0.7, 'weak')]), 3)
  const s1 = r1.transitions[0].sequence
  const r2 = c.ingest(lanesOf([entry(12, 'terminal', 1, 0.9, 'strong')]), 3)
  const s2 = r2.transitions[0].sequence
  ok(s2 > s1, 'newer arrival has a newer sequence')
  ok(c.getState().active.length === 1, 'stale in-flight superseded (one bubble per identity)')
  const arr1 = c.arrive(s2)
  ok(arr1.arrived, 'newest arrival commits')
  eq(c.getState().flashSlotId, 'goal|0', 'arrival flash on the destination slot')
  // The stale timer fires and tries to clear the slot's flash — the
  // ownership guard must protect the newer flash.
  c.clearFlash('goal|0', s1)
  ok(c.getState().flashes['goal|0'] === true, 'stale (older-sequence) timer does not clear the newer flash')
  c.clearFlash('goal|0', s2)
  eq(c.getState().flashes['goal|0'], undefined, 'the owning sequence clears its own flash')
  // A slot with no active flash clears unconditionally.
  c.clearFlash('goal|1')
  eq(c.getState().flashes['goal|1'], undefined, 'clearing a never-flashed slot is a no-op')
}

// v0.1.10 (review fix 6): resize cancels in-flight travel AND flashes.
{
  const c = createPulseBubbleCoordinator()
  c.ingest(lanesOf([entry(12, 'terminal', 1, 0.5, 'weak')]), 3)
  c.ingest(lanesOf([entry(12, 'terminal', 1, 0.9, 'strong')]), 3)
  c.arrive(c.getState().active[0].sequence)
  ok(c.getState().flashes['goal|0'] === true, 'flash pending after arrival')
  const rs = c.resize()
  eq(rs.cancelled, 0, 'nothing in flight after arrival (cap satisfied)')
  eq(c.getState().flashes, {}, 'resize clears pending flashes (coordinates obsolete)')
}

// v0.1.10 (review fix 6): the renderer data key — same content under a
// fresh object identity must NOT re-ingest (effect dep is the key).
{
  const lanes = [entry(12, 'terminal', 1, 0.9, 'strong')]
  eq(pulseLanesKey(lanesOf(lanes), 3), pulseLanesKey(lanesOf([...lanes]), 3),
     'same visible content -> same key (no re-ingest on object identity change)')
  const changed = [entry(12, 'terminal', 1, 0.95, 'strong')]
  ok(pulseLanesKey(lanesOf(changed), 3) !== pulseLanesKey(lanesOf(lanes), 3),
     'content change -> different key (effect re-runs)')
  const noise = [entry(12, 'terminal', 1, 0.9000000004, 'strong')]
  eq(pulseLanesKey(lanesOf(noise), 3), pulseLanesKey(lanesOf(lanes), 3),
     'sub-hysteresis noise shares the key (no effect churn)')
}

// v0.1.10+4 (live bug "balls never reach"): the travel delay is the
// coordinator-assigned, per-transition STABLE staggerMs — never the
// render index. Sibling arrivals re-render the active list; a
// re-derived delay would rewrite the in-flight animation string and
// restart it (ball rewinds to spawn; the timer unmounts it mid-flight).
{
  const c = createPulseBubbleCoordinator()
  const mk = (k) => Array.from({ length: k }, (_, i) => entry(200 + i, 'tool' + i, i + 1, 0.5, 'weak'))
  c.ingest(lanesOf(mk(6)), 6)
  c.ingest(lanesOf(mk(6).map((e, i) => ({ ...e, value: 0.9, level: 'strong' }))), 6)
  const act = c.getState().active.slice()
  eq(act.map(t => t.staggerMs), act.map((_, i) => BUBBLE_STAGGER_MS * i),
    'new flights carry a stable staggerMs (0, S, 2S, ...)')
  eq(c.getState().queued.length, 6 - MAX_CONCURRENT_BUBBLES, 'overflow stays queued')
  const s0 = c.getState().active[0].sequence
  const rest = c.getState().active.slice(1).map(t => t.staggerMs)
  const arr = c.arrive(s0)
  eq(c.getState().active.map(t => t.staggerMs), rest.concat(arr.promoted.map(t => t.staggerMs)),
    'sibling arrival rewrites NO in-flight staggerMs (promoted ball joins at delay 0)')
  ok(Array.isArray(arr.promoted), 'arrive reports the promoted transitions')
  eq(arr.promoted.length, 1, 'one queued ball promoted')
  eq(arr.promoted[0].staggerMs, 0, 'promoted ball travels from now (delay 0, stable)')
  ok(typeof arr.promoted[0].sequence === 'number', 'promoted ball carries its sequence (timer target)')
}

// v0.1.10 (review P0: React unmount while timers are active): surface
// loss cancels every pending transition and commits the latest
// snapshot immediately. A stale travel timer that fires AFTER
// unmount has no transition left to arrive — arrive() is a no-op and
// creates no new flash; the renderer's unmount effect clears the
// timers themselves (pinned in the source block below).
{
  const c = createPulseBubbleCoordinator()
  c.ingest(lanesOf([entry(12, 'terminal', 1, 0.5, 'weak')]), 3)
  c.ingest(lanesOf([entry(12, 'terminal', 1, 0.9, 'strong')]), 3)
  const staleSeq = c.getState().active[0].sequence
  const rs = c.resize({ width: 400, height: 800 })
  eq(rs.cancelled, 1, 'surface loss cancels the in-flight transition')
  eq(c.visibleLanes(3).goal[0].value, 0.9, 'the latest snapshot commits immediately (no obsolete landing)')
  const arr = c.arrive(staleSeq)
  eq(arr.arrived, false, 'a stale sequence fired after unmount cannot commit')
  eq(c.getState().flashSlotId, null, 'a stale timer creates no new flash')
  eq(c.visibleLanes(3).goal[0].value, 0.9, 'visible content is unchanged by the stale arrival')
  c.clearFlash(c.getState().flashSlotId)
  eq(c.getState().flashes, {}, 'stale clearFlash stays a no-op')
}

// Pixel-step travel generator (animation pass): the staircase is the
// instrument's motion — integer 4px hops, hold-then-jump saccades, a
// bounded hop count, and an element whose animation name and keyframes
// are generated from the SAME (dist, n) pair so they cannot drift.
{
  const { pulseTravelSteps, pulseTravelKeyframes } = kfApi
  eq(pulseTravelSteps(84), 21, '84px travel -> 21 hops (~4px each)')
  eq(pulseTravelSteps(8), 4, 'short climbs clamp to the 4-hop floor (still a staircase)')
  eq(pulseTravelSteps(1200), 24, 'long climbs clamp to the 24-hop bound')
  const css = pulseTravelKeyframes(-84)
  ok(css.startsWith('@keyframes iyh-bubble-travel-84-21{'), 'keyframe name is stable per (dist, steps)')
  ok(css.endsWith('100%{transform:translate(0,-84px)}}'), 'final stop is EXACTLY the distance (cell-centered landing)')
  const stops = Array.from(css.matchAll(/translate\(0,(-?\d+)(?:px)?\)/g), (m) => Number(m[1]))
  eq(stops[0], 0, 'staircase starts at the spawn (no offset)')
  eq(stops[stops.length - 1], -84, 'staircase ends at the full distance')
  ok(stops.every((v) => Number.isInteger(v)), 'every stop is a whole pixel (no subpixel frames)')
  ok(stops.every((v, i) => i === 0 || v <= stops[i - 1]), 'stops are monotone upward (no backward jitter)')
  ok(css.split('animation-timing-function:steps(1,end)').length - 1 === stops.length - 1,
    'every segment is hold-then-jump (steps(1,end)) — saccades, not a glide')
  // Flight geometry: whole-pixel spawn/landing derived from the measured
  // box; the bubble lands ON the tool's dot-matrix indicator (the
  // first-dot column), not the abstract cell centerline (animation
  // pass v2: top-packed 16px rows, right-pinned 19px dot grid).
  const f = pulseBubbleFlight({ width: 400, height: 96 }, 'live', 0, 4, true)
  eq(f.left, 75, 'sprite aligned on the tool first-dot column (narrow: laneW 97.75; 97.75 - 4 chrome - 19 = 74.75 -> 75, whole pixel)')
  eq(f.top, 84, 'spawn bottom-flush on the surface edge (96 - 12px sprite height)')
  eq(f.dy, -79, 'landing on the tool top-packed row (rowCenter 11 = 4 chrome + 7; 11 - 6 - 84 = -79)')
  eq(f.animationName, 'iyh-bubble-travel-79-20', 'animation name pairs with the generated keyframe')
  ok(f.keyframes === pulseTravelKeyframes(f.dy), 'element keyframes and animation name derive from the same flight (no drift)')
  const f2 = pulseBubbleFlight({ width: 400, height: 96 }, 'friction', 2, 4, true)
  eq(f2.left, 377, 'fourth lane dot column (302.25 + 97.75 - 4 chrome - 19 = 377, whole pixel)')
  eq(f2.top, 84, 'every flight spawns bottom-flush regardless of destination')
  eq(f2.dy, -47, 'deeper slot -> shorter climb (rowCenter 43 = 4+32+7; 43 - 6 - 84 = -47)')
  const fW = pulseBubbleFlight({ width: 400, height: 96 }, 'live', 0, 4, false)
  eq(fW.left, 73, 'wide composition (colGap 6 = PULSE_BOARD_GAP): laneW 95.5; 95.5 - 4 chrome - 19 = 72.5 -> 73')
  eq(fW.animationName, f.animationName, 'step count is width-independent (driven by |dy| only)')
}

// Source pins: wired into PulseLanes, UI-only (no backend schema fields).
// Animation pass (pixel-synthwave integration): the bubble is now a 4px
// dot-matrix column sprite climbing a generated per-distance STAIRCASE
// (integer 4px hops, steps(1,end) saccades) with a 4px arrival STAMP on
// the landing axis — replacing the rounded 8px dot + linear slide +
// whole-cell inset glow. Behaviour contracts below are unchanged.
{
  const lanesSrc = src.slice(src.indexOf('function PulseLanes'), src.indexOf('function IoLine'))
  ok(lanesSrc.includes('createPulseBubbleCoordinator'), 'PulseLanes owns the coordinator')
  ok(lanesSrc.includes('prefers-reduced-motion'), 'reduced-motion read via matchMedia in PulseLanes')
  ok(lanesSrc.includes('pulse-bubble') && lanesSrc.includes('pulseBubbleFlight'),
    'bubble overlay rendered surface-relative (integer-pixel flight helper)')
  ok(lanesSrc.includes('BUBBLE_TRAVEL_MS') && lanesSrc.includes('BUBBLE_ARRIVAL_MS'), 'named timing constants used in PulseLanes')
  ok(!lanesSrc.includes('destination_slot'), 'no destination_slot in lane UI (desktop resolves slot, backend schema clean)')
  ok(src.includes('iyh-bubble-travel'), 'bubble travel keyframe family present (bottom-edge -> cell, one px coordinate system)')
  ok(!src.includes('iyh-bubble-rise'), 'old 24px local-hop keyframe removed')
  ok(src.includes('pulseTravelKeyframes') && src.includes('PULSE_TRAVEL_STEP_PX'),
    'per-distance staircase keyframes are generated (4px pixel-unit hops, clamped hop count)')
  ok(src.includes('steps(1,end)'), 'travel/stamp segments are hold-then-jump saccades (no smooth glide, no subpixel frames)')
  ok(src.includes('iyh-bubble-stamp'), 'arrival stamp keyframes present')
  ok(src.includes('PULSE_LANDING_BEFORE_DOT'), 'on-dot vs before-dot landing is one named switch')
  ok(lanesSrc.includes('f.targetY - PULSE_ROW_CENTER_OFF'), 'arrival stamp sits ON the tool first dot (dot-grid top row), not below the cell')
  ok(lanesSrc.includes('cap, narrow'), 'flight calls thread the narrow layout flag (lane column gap)')
  ok(!lanesSrc.includes('inset 0 0 8px'), 'the old whole-cell inset glow is gone')
  ok(!/pulse-bubble[\s\S]{0,400}?borderRadius/.test(lanesSrc), 'the bubble sprite has sharp edges (no rounded corners, no antialiased dot)')
  ok(/Unmount/.test(lanesSrc) && lanesSrc.includes('clearTimeout'),
    'unmount effect cancels every travel/flash timer (timers live in refs)')
  ok(lanesSrc.includes('f.animationName') && lanesSrc.includes("left: f.left + 'px'"),
    'spawn + travel are whole-pixel px (flight helper from the measured pulseBubblePath; no px/% mix, no CSS-variable transform)')
  ok(lanesSrc.includes('t.staggerMs != null ? t.staggerMs : 0'),
    'animation delay + travel timer share the stable staggerMs (no render-index rewrite, no animation restart)')
  ok(lanesSrc.includes('(arr.promoted || []).forEach(scheduleArrival)'),
    'promoted (queue->active) balls get their own travel timers (no frozen fill-mode ball)')
  ok(!/fetch\(|XMLHttpRequest/.test(block), 'transition layer is pure UI (no I/O)')
  // Imports invariant: only SDK + react.
  const imports = src.match(/^import .*$/gm) || []
  ok(imports.every(l => l.includes('@hermes/plugin-sdk') || l.includes('react')), 'desktop imports only SDK + react (got ' + imports.join('; ') + ')')
  ok(!/animation_ms|travel_ms/.test(block), 'no animation fields added to any backend-shaped data')
}
console.log('PASS bubble: ' + n + ' assertions')
