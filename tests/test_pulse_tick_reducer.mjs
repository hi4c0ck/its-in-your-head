// tests/test_pulse_tick_reducer.mjs — v0.1.10 Continuous Tool Activity.
// Spec §4 (reducer/cursor) + §5 (activity transitions) + §6 (key ext.):
//   R — createPulseTickReducer: fixed 1.3s window, never a trailing
//       debounce; ticks during an in-flight refresh are not lost; no
//       overlapping refreshes; cancel tears the window down.
//   A — coordinator.activity(): same tool called N times = N distinct
//       FIFO bubbles (identity evt|<eventId>); redelivery dedupe by
//       watermark; hidden tools hold (bounded queue) and promote when
//       visible; gone-from-lane drops without a PULSE error; V1 hold
//       of semantic lanes + V2 immediate LIVE commit; reduced motion.
//   S — semantic suppression: an activity-driven batch must not also
//       emit LIVE rank-change bubbles (spec §5.3).
//   K — LIVE lane key extension: a completion with unchanged rank and
//       momentum bucket still changes the key (spec §6).
// Run: node tests/test_pulse_tick_reducer.mjs
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const dir = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(dir, '..', 'desktop', 'plugin.js'), 'utf8')

// --- Extract the reducer region (PULSE_REDUCER_MS .. portrait cadence)
const rStart = src.indexOf('const PULSE_REDUCER_MS')
assert.ok(rStart > 0, 'PULSE_REDUCER_MS region found')
const rEnd = src.indexOf('// Portrait-only cadence', rStart)
assert.ok(rEnd > rStart, 'reducer region bounded')
const rApi = new Function(src.slice(rStart, rEnd) +
  '\n;return { PULSE_REDUCER_MS, createPulseTickReducer, pulseActivityQuerySuffix, pulseActivityAccept }')()
const { PULSE_REDUCER_MS, createPulseTickReducer, pulseActivityQuerySuffix } = rApi

// --- Extract the STAGE5 block (coordinator + key + detect)
const block = src.slice(src.indexOf('// STAGE5-START'), src.indexOf('// STAGE5-END'))
assert.ok(block.length > 0, 'STAGE5 markers found')
const api = new Function(block + `
  ;return { BUBBLE_STAGGER_MS, pulseLanesKey, detectPulseTransitions,
    pulseSnapshotFromLanes, createPulseBubbleCoordinator };`)()
const { BUBBLE_STAGGER_MS, pulseLanesKey, detectPulseTransitions,
  pulseSnapshotFromLanes, createPulseBubbleCoordinator } = api

let n = 0
const eq = (a, e, label) => { assert.deepEqual(a, e, label + ' (got ' + JSON.stringify(a) + ')'); console.log('ok ' + (++n) + ' - ' + label) }
const ok = (c, label) => { assert.ok(c, label); console.log('ok ' + (++n) + ' - ' + label) }

// Fake clock: collectable setTimeout, monotonic advance.
function fakeClock() {
  var t = 0, seq = 0
  var timers = new Map()
  function advance(ms) {
    var end = t + ms
    for (;;) {
      var nextAt = Infinity, nextId = null
      timers.forEach(function (tm, id) {
        if (tm.at < nextAt) { nextAt = tm.at; nextId = id }
      })
      if (nextAt === Infinity || nextAt > end) { t = end; return }
      t = nextAt
      var tm = timers.get(nextId)
      timers.delete(nextId)
      tm.fn()
    }
  }
  return {
    advance,
    pending: function () { return timers.size },
    setTimeout: function (fn, ms) { var id = ++seq; timers.set(id, { at: t + (ms || 0), fn }); return id },
    clearTimeout: function (id) { timers.delete(id) },
  }
}

// ============ R: reducer semantics ============
eq(PULSE_REDUCER_MS, 1300, 'reducer window is the spec 1.3s (1300ms)')

{
  const clk = fakeClock()
  var fired = 0
  const r = createPulseTickReducer(function () { fired += 1 },
    { ms: PULSE_REDUCER_MS, setTimeout: clk.setTimeout, clearTimeout: clk.clearTimeout })
  // R1: first tick opens the window; nothing fires inside it.
  r.onTick()
  ok(clk.pending() === 1, 'R1: first tick opens exactly one pending window')
  clk.advance(PULSE_REDUCER_MS - 1)
  eq(fired, 0, 'R1: no refresh before the window expires')
  clk.advance(1)
  eq(fired, 1, 'R2: exactly one refresh at window expiry')
  r.settle() // the targeted PULSE refresh finished (queryFn / invalidate promise)
  // R2b: more ticks inside the NEXT window never restart it.
  r.onTick()
  r.onTick()
  r.onTick()
  eq(clk.pending(), 1, 'R2: repeated ticks keep ONE window open (no trailing debounce)')
  clk.advance(PULSE_REDUCER_MS)
  eq(fired, 2, 'R2: one refresh for the whole tick burst')
  r.settle()
}
{
  // R3: a tick during an in-flight refresh is not lost — it marks
  // dirty and a fresh window opens after settle().
  const clk = fakeClock()
  var inFlight = false
  const r = createPulseTickReducer(function () {
    inFlight = true
  }, { ms: PULSE_REDUCER_MS, setTimeout: clk.setTimeout, clearTimeout: clk.clearTimeout })
  r.onTick()
  clk.advance(PULSE_REDUCER_MS) // refresh #1 starts
  ok(inFlight, 'R3: first window fired the refresh')
  r.onTick() // tick while in flight
  eq(clk.pending(), 0, 'R3: tick during flight does NOT fire an overlapping refresh')
  inFlight = false
  r.settle()
  ok(clk.pending() === 1, 'R3: settle re-opens a window for the dirty tick')
  clk.advance(PULSE_REDUCER_MS)
  eq(r.state().refreshes, 2, 'R3: the dirty tick produced exactly one follow-up refresh')
}
{
  // R4: cancel tears the pending window down.
  const clk = fakeClock()
  var fired = 0
  const r = createPulseTickReducer(function () { fired += 1 },
    { ms: PULSE_REDUCER_MS, setTimeout: clk.setTimeout, clearTimeout: clk.clearTimeout })
  r.onTick()
  r.cancel()
  eq(clk.pending(), 0, 'R4: cancel clears the pending window')
  clk.advance(10000)
  eq(fired, 0, 'R4: nothing fires after cancel')
}

// ============ A: coordinator activity ============
// Live-lane entry in the backend shape (activity + momentum envelope).
const liveEntry = (name, rank, act) => ({
  tool_id: 1, tool_name: name, rank,
  momentum: { value: 0.5, state: 'steady' },
  activity: Object.assign({ calls_started: 1, calls_completed: 0, errors: 0, last_seen: 1700000000 }, act || {}),
})
const ev = (id, tool) => ({
  id, tool_name: tool, tool_call_id: 'call_' + id, ts: 1700000000,
  session_id: 's', tool_input_summary: '', tool_output_summary: '',
})

{
  // A1: five calls of the same tool = five distinct FIFO bubbles,
  // each with its own evt|<id> identity — never a replace.
  const coord = createPulseBubbleCoordinator({ maxConcurrent: 5 })
  coord.ingest({ live: [liveEntry('editor', 1)], goal: [], good: [], friction: [] }, 5)
  var res = coord.activity([ev(1, 'editor'), ev(2, 'editor'), ev(3, 'editor'), ev(4, 'editor'), ev(5, 'editor')],
    { live: [liveEntry('editor', 1, { calls_started: 5 })], goal: [], good: [], friction: [] }, 5)
  eq(res.accepted, 5, 'A1: all five calls accepted')
  eq(coord.getState().active.length, 5, 'A1: five distinct bubbles in flight (cap allows it)')
  eq(coord.getState().active.map(function (t) { return t.semanticId }),
    ['evt|1', 'evt|2', 'evt|3', 'evt|4', 'evt|5'], 'A1: FIFO order, evt|<id> identities')
  eq(coord.getState().active.map(function (t) { return t.staggerMs }),
    [0, BUBBLE_STAGGER_MS, 2 * BUBBLE_STAGGER_MS, 3 * BUBBLE_STAGGER_MS, 4 * BUBBLE_STAGGER_MS],
    'A7: stable per-flight stagger, assigned once at dispatch')
  // A2: the same batch redelivered (refetch without cursor advance)
  // is fully de-duped by the per-surface watermark.
  var res2 = coord.activity([ev(1, 'editor'), ev(2, 'editor')],
    { live: [liveEntry('editor', 1, { calls_started: 5 })], goal: [], good: [], friction: [] }, 5)
  eq(res2.accepted, 0, 'A2: redelivered events below the watermark are ignored')
  eq(coord.getState().active.length, 5, 'A2: no duplicate bubbles')
}
{
  // A3: hidden tool (rank > capacity) holds FIFO and promotes when it
  // rises into the visible prefix (capacity growth here).
  const lanes = { live: [liveEntry('editor', 1), liveEntry('reader', 2), liveEntry('writer', 3)],
                  goal: [], good: [], friction: [] }
  const coord = createPulseBubbleCoordinator({ maxConcurrent: 3 })
  coord.ingest(lanes, 2)
  var res = coord.activity([ev(10, 'writer')], lanes, 2)
  eq(res.accepted, 1, 'A3: event accepted even though the tool is hidden')
  eq(coord.getState().active.length, 0, 'A3: hidden tool spawns no visible bubble yet')
  eq(coord.getState().held.length, 1, 'A3: event holds in the FIFO queue')
  var res2 = coord.activity([], lanes, 3) // capacity grows: writer now visible
  ok(res2.fresh.length === 1 && res2.fresh[0].semanticId === 'evt|10', 'A3: held event promotes when the tool becomes visible')
  eq(coord.getState().held.length, 0, 'A3: hold queue drained on promotion')
}
{
  // A4: tool gone from the lane entirely -> dropped (debug counter
  // only), never a PULSE error, never a foreign-cell animation.
  const lanes = { live: [liveEntry('editor', 1)], goal: [], good: [], friction: [] }
  const coord = createPulseBubbleCoordinator({ maxConcurrent: 3 })
  coord.ingest(lanes, 3)
  var res = coord.activity([ev(20, 'ghost')], lanes, 3)
  eq(res.accepted, 1, 'A4: the event is still accepted into the stream')
  eq(coord.getState().activityDropped, 1, 'A4: gone-from-lane event is dropped (counted)')
  eq(coord.getState().active.length, 0, 'A4: no bubble for a tool that no longer exists')
}
{
  // A5: V1/V2 — LIVE commits immediately on acceptance; semantic lanes
  // HOLD their in-flight values until their own bubbles arrive.
  const lanesBefore = {
    live: [liveEntry('editor', 1, { calls_started: 1 })],
    goal: [{ tool_id: 9, tool_name: 'g1', rank: 1, momentum: { value: 0.9, state: 'steady' }, level: 'strong' }],
    good: [], friction: [],
  }
  const lanesAfter = {
    live: [liveEntry('editor', 1, { calls_started: 9, calls_completed: 4 })],
    goal: [{ tool_id: 9, tool_name: 'g1', rank: 1, momentum: { value: 0.2, state: 'steady' }, level: 'weak' }],
    good: [], friction: [],
  }
  const coord = createPulseBubbleCoordinator({ maxConcurrent: 4 })
  coord.ingest(lanesBefore, 4)
  var res = coord.activity([ev(30, 'editor')], lanesAfter, 4)
  ok(res.accepted === 1, 'A5: activity accepted')
  var vis = coord.visibleLanes(4)
  eq(vis.live[0].activity.calls_started, 9, 'A5/V2: LIVE lane shows the NEW content immediately')
  eq(vis.goal[0].momentum.value, 0.9, 'A5/V1: GOAL lane still holds the pre-batch value')
}
{
  // A6: reduced motion — no travel at all; content commits immediately.
  const lanes = {
    live: [liveEntry('editor', 1, { calls_started: 1 })],
    goal: [], good: [], friction: [],
  }
  const lanes2 = {
    live: [liveEntry('editor', 1, { calls_started: 7, calls_completed: 3 })],
    goal: [], good: [], friction: [],
  }
  const coord = createPulseBubbleCoordinator({ maxConcurrent: 4 })
  coord.ingest(lanes, 4)
  var res = coord.activity([ev(40, 'editor')], lanes2, 4, { reducedMotion: true })
  eq(res.accepted, 1, 'A6: events still accepted')
  eq(res.fresh.length, 0, 'A6: no travel transitions under reduced motion')
  eq(coord.getState().active.length, 0, 'A6: nothing in flight')
  eq(coord.visibleLanes(4).live[0].activity.calls_started, 7, 'A6: LIVE content committed immediately')
}

// ============ S: semantic suppression (§5.3) ============
{
  // An activity-driven LIVE change must not ALSO emit live rank-
  // change bubbles from the semantic detector of the same batch.
  // The fixture gives the live entry a REAL semantic change too
  // (momentum 0.5 -> 0.8 crosses the quantized bucket) so the
  // baseline (no suppression) genuinely would animate live.
  var prev = pulseSnapshotFromLanes(
    { live: [liveEntry('editor', 1, { calls_started: 1 })], goal: [], good: [], friction: [] }, 4)
  var nextLive = liveEntry('editor', 1, { calls_started: 9, calls_completed: 4 })
  nextLive.momentum = { value: 0.8, state: 'rising' }
  var next = pulseSnapshotFromLanes(
    { live: [nextLive],
      goal: [{ tool_id: 9, tool_name: 'g1', rank: 1, momentum: { value: 0.9 } }],
      good: [], friction: [] }, 4)
  var withLive = detectPulseTransitions(prev, next, 0)
  var suppressed = detectPulseTransitions(prev, next, 0, { excludeMetrics: ['live'] })
  ok(withLive.some(function (t) { return t.metric === 'live' }),
    'S1: without suppression the LIVE value change WOULD animate (baseline sanity)')
  eq(suppressed.filter(function (t) { return t.metric === 'live' }).length, 0,
    'S1: with excludeMetrics live, the activity batch emits no live semantic bubbles')
  ok(suppressed.some(function (t) { return t.metric === 'goal' }),
    'S1: other lanes keep animating normally in the same batch')
}

// ============ K: LIVE key extension (§6) ============
{
  var base = {
    live: [liveEntry('editor', 1, { calls_started: 1, calls_completed: 0 })],
    goal: [], good: [], friction: [],
  }
  var completed = JSON.parse(JSON.stringify(base))
  completed.live[0].activity.calls_completed = 4 // completion, same rank + momentum bucket
  var k1 = pulseLanesKey(base, 4)
  var k2 = pulseLanesKey(completed, 4)
  var k3 = pulseLanesKey(base, 4)
  ok(k1 !== k2, 'K1: a completion (calls_completed up, rank/momentum unchanged) changes the LIVE key')
  eq(k3, k1, 'K2: identical lane content -> identical key (no false invalidation)')
  // errors and last_seen also ride the key.
  var errored = JSON.parse(JSON.stringify(base))
  errored.live[0].activity.errors = 1
  ok(pulseLanesKey(errored, 4) !== k1, 'K3: an error count change changes the LIVE key')
  var seen = JSON.parse(JSON.stringify(base))
  seen.live[0].activity.last_seen = 1700000999
  ok(pulseLanesKey(seen, 4) !== k1, 'K4: last_seen change changes the LIVE key (floored to seconds)')
}

// ============ cursor helpers (spec §4.4) ============
{
  eq(pulseActivityQuerySuffix(), '', 'no cursor -> plain /pulse (baseline high-water, no history)')
  rApi.pulseActivityAccept({ activity_events: { next_cursor: 42, events: [] } })
  eq(pulseActivityQuerySuffix(), '?after_event_id=42', 'accepted batch advances the cursor')
  rApi.pulseActivityAccept({ activity_events: { next_cursor: 57, events: [] } })
  eq(pulseActivityQuerySuffix(), '?after_event_id=57', 'the cursor is monotone per accepted batch')
  rApi.pulseActivityAccept(null) // no fault, no advance
  eq(pulseActivityQuerySuffix(), '?after_event_id=57', 'null batch never moves the cursor')
}

console.log('PASS pulse-tick-reducer: ' + n + ' assertions')
