// tests/test_pulse_tick_reducer.mjs — v0.1.10 Continuous Tool Activity.
// Spec §4 (reducer/cursor) + §5 (activity transitions) + §6 (key ext.):
//   R — createPulseTickReducer: fixed 1.3s window, never a trailing
//       debounce; ticks during an in-flight refresh are not lost; no
//       overlapping refreshes; cancel tears the window down.
//   A — coordinator.activity(): same tool called N times = N distinct
//       FIFO bubbles (identity evt|<eventId>); redelivery dedupe by
//       watermark; unroutable tools (hidden or off-lane) are ACCEPTED
//       but NOT buffered in renderer memory (v0.1.11 B, cursor model) —
//       they stay in the persistent events table and the tool's per-tool
//       consumed watermark (consumedByTool) is left behind; backlogTools()
//       + flushTool() fetch that tool's backlog and animate it FILO
//       (newest first) when its LIVE cell becomes visible; V1 hold of
//       semantic lanes + V2 immediate LIVE commit; reduced motion.
//   C — cold-start integration: baseline high-water 0 is a VALID
//       cursor (the query still sends after_event_id=0) -> the first
//       event (id 1) becomes a visible bubble.
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
  '\n;return { PULSE_REDUCER_MS, createPulseTickReducer, pulseActivityQuerySuffix, pulseActivityAccept, pulseActivityShouldAdopt }')()
const { PULSE_REDUCER_MS, createPulseTickReducer, pulseActivityQuerySuffix,
  pulseActivityAccept, pulseActivityShouldAdopt } = rApi

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
// C.2: the window dropped from the legacy 1.3s to the portrait cadence
// (850ms) so an activity burst reaches a bubble in the same breath the
// live expression decays. The semantic tests below drive it symbolically
// (PULSE_REDUCER_MS), so only this constant pin changes.
eq(PULSE_REDUCER_MS, 850, 'reducer window is the portrait cadence (850ms, C.2)')

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
  // A3 (v0.1.11 B): hidden tool (rank > capacity) — its event is accepted
  // but NOT buffered in renderer memory. When capacity grows, the tool's
  // backlog (fetched from the DB by the caller) is flushed FILO.
  const lanes = { live: [liveEntry('editor', 1), liveEntry('reader', 2), liveEntry('writer', 3)],
                  goal: [], good: [], friction: [] }
  const coord = createPulseBubbleCoordinator({ maxConcurrent: 3 })
  coord.ingest(lanes, 2)
  var res = coord.activity([ev(10, 'writer')], lanes, 2) // writer rank 3 > cap 2: hidden
  eq(res.accepted, 1, 'A3: event accepted even though the tool is hidden')
  eq(coord.getState().active.length, 0, 'A3: hidden tool spawns no visible bubble yet')
  eq(coord.getState().consumedByTool['writer'], undefined,
     'A3: hidden tool is NOT buffered in renderer memory (no consumed watermark)')
  ok(coord.backlogTools(lanes, 3, 10).indexOf('writer') >= 0,
     'A3: backlogTools flags the hidden tool once it would be visible (cap 3)')
  // The caller (React) fetches writer's DB page (ASC) and flushes with cap 3.
  var res2 = coord.flushTool('writer', [ev(10, 'writer')], 10, lanes, 3)
  ok(res2.fresh.length === 1 && res2.fresh[0].semanticId === 'evt|10',
     'A3: flushTool animates the backlog when the tool becomes visible')
  eq(coord.getState().consumedByTool['writer'], 10, 'A3: the consumed watermark advanced')
}
{
  // A4 (v0.1.11 B): tool not on the lane right now -> the event is
  // STILL ACCEPTED (cursor advances past it) but is NOT buffered in
  // renderer memory. When the tool's LIVE cell exists, flushTool()
  // animates its backlog. Never dropped, never foreign-routed, no
  // PULSE error.
  const lanes = { live: [liveEntry('editor', 1)], goal: [], good: [], friction: [] }
  const coord = createPulseBubbleCoordinator({ maxConcurrent: 3 })
  coord.ingest(lanes, 3)
  var res = coord.activity([ev(20, 'ghost')], lanes, 3) // ghost not on the lane
  eq(res.accepted, 1, 'A4: the event is still accepted into the stream')
  eq(coord.getState().consumedByTool['ghost'], undefined,
     'A4: off-lane event NOT buffered in renderer memory')
  eq(coord.getState().activityDropped, 0, 'A4: nothing is dropped (no foreign-cell routing)')
  eq(coord.getState().active.length, 0, 'A4: no bubble until the tool has a LIVE cell')
  var lanes2 = { live: [liveEntry('editor', 1), liveEntry('ghost', 2)], goal: [], good: [], friction: [] }
  ok(coord.backlogTools(lanes2, 3, 20).indexOf('ghost') >= 0,
     'A4: backlogTools flags the tool once it enters the visible prefix')
  var res2 = coord.flushTool('ghost', [ev(20, 'ghost')], 20, lanes2, 3)
  ok(res2.fresh.length === 1 && res2.fresh[0].semanticId === 'evt|20',
     'A4: flushTool animates when the tool enters the lane')
  eq(coord.getState().consumedByTool['ghost'], 20, 'A4: the consumed watermark advanced')
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
  // DEADLOCK regression (found in live smoke, v0.1.10+4): the fresh
  // pane's first fetch is the no-cursor BASELINE — the server answers
  // with empty events + the high-water mark. If the pane only adopted
  // the cursor on accepted events, the cursor stayed null forever,
  // every fetch stayed a baseline, and the first real batch was
  // unreachable (no bubbles, ever).
  eq(pulseActivityShouldAdopt({ accepted: 0 }, { events: [], next_cursor: 42 }),
    true, 'baseline (no events) ADOPTS the server high-water mark')
  eq(pulseActivityShouldAdopt({ accepted: 3 }, { events: [1, 2, 3], next_cursor: 9 }),
    true, 'an accepted batch advances the cursor')
  eq(pulseActivityShouldAdopt({ accepted: 0 }, { events: [1, 2], next_cursor: 9 }),
    false, 'a FULL REDELIVERY (zero accepted, non-empty) must NOT advance — refetch redelivers')
  eq(pulseActivityShouldAdopt({ accepted: 0 }, { events: [] }),
    false, 'a malformed batch (no next_cursor) must NOT advance')
  eq(pulseActivityShouldAdopt(null, { events: [], next_cursor: 5 }),
    true, 'a well-formed empty batch adopts even with no coordinator result (the cursor is module state; the effect always passes a real result)')
}

// ============ A8/A9: cursor-model backlog (v0.1.11 B) ============
{
  // A8 (v0.1.11 B): a large off-lane backlog is NOT buffered in renderer
  // memory (the cursor model) — no unbounded queue. 70 off-lane events,
  // each its own tool. When a tool enters the lane, its backlog (the DB
  // page the caller fetches) is flushed FILO.
  const coord = createPulseBubbleCoordinator({ maxConcurrent: 2 })
  const offLane = { live: [liveEntry('editor', 1)], goal: [], good: [], friction: [] }
  coord.ingest(offLane, 2)
  var evs = []
  for (var i = 1; i <= 70; i++) evs.push(ev(i, 'tool' + i))
  var res = coord.activity(evs, offLane, 2)
  eq(res.accepted, 70, 'A8: all 70 events accepted (cursor advanced)')
  eq(coord.getState().activityDropped, 0, 'A8: nothing dropped')
  eq(Object.keys(coord.getState().consumedByTool).length, 0,
     'A8: none of the 70 off-lane events are buffered in renderer memory (cursor model)')
  // tool1 enters the lane -> flush its backlog (its DB page is just evt|1).
  var lanes2 = { live: [liveEntry('editor', 1), liveEntry('tool1', 2)], goal: [], good: [], friction: [] }
  ok(coord.backlogTools(lanes2, 2, 70).indexOf('tool1') >= 0,
     'A8: backlogTools flags tool1 once it enters the visible prefix')
  var res2 = coord.flushTool('tool1', [ev(1, 'tool1')], 1, lanes2, 2)
  eq(res2.fresh.map(function (t) { return t.semanticId }), ['evt|1'],
     'A8: flushTool animates tool1 backlog (its single event)')
  eq(coord.getState().consumedByTool['tool1'], 1, 'A8: the tool1 consumed watermark advanced')
}
{
  // A9 (v0.1.11 B): capacity growth makes a hidden tool's backlog
  // flushable — flushTool() animates it with NO new batch (the pane's
  // lane-key/capacity effect path, review fix 2).
  const lanes = { live: [liveEntry('a', 1), liveEntry('b', 2), liveEntry('c', 3)],
                  goal: [], good: [], friction: [] }
  const coord = createPulseBubbleCoordinator({ maxConcurrent: 3 })
  coord.ingest(lanes, 2)
  coord.activity([ev(50, 'c')], lanes, 2) // c rank 3 > cap 2: hidden -> not buffered
  eq(coord.getState().consumedByTool['c'], undefined,
     'A9: the hidden tool\u2019s event is NOT buffered in renderer memory')
  ok(coord.backlogTools(lanes, 3, 50).indexOf('c') >= 0,
     'A9: backlogTools flags c once cap grows to 3')
  var res = coord.flushTool('c', [ev(50, 'c')], 50, lanes, 3) // cap 3: c now visible
  ok(res.fresh.length === 1 && res.fresh[0].semanticId === 'evt|50',
     'A9: the backlog flushes on the capacity change alone')
  eq(coord.getState().consumedByTool['c'], 50, 'A9: the consumed watermark advanced')
}

// ============ C: cold-start integration — baseline 0 -> first event -> bubble ============
{
  // The path a fresh install walks: fresh DB (no events) -> baseline
  // response (events: [], next_cursor: 0) -> the pane adopts 0 -> the
  // NEXT request must carry after_event_id=0 (0 is a valid cursor, not
  // a falsy "no cursor") -> the server returns the first event (id 1)
  // -> it becomes a visible bubble.
  rApi.pulseActivityAccept({ activity_events: { events: [], next_cursor: 0 } })
  eq(pulseActivityQuerySuffix(), '?after_event_id=0',
     'C1: the adopted baseline 0 still sends a cursor (0 is valid, not null)')
  const coord = createPulseBubbleCoordinator({ maxConcurrent: 3 })
  coord.ingest({ live: [liveEntry('terminal', 1)], goal: [], good: [], friction: [] }, 3)
  var res = coord.activity([ev(1, 'terminal')],
    { live: [liveEntry('terminal', 1, { calls_started: 1 })], goal: [], good: [], friction: [] }, 3)
  eq(res.accepted, 1, 'C2: the first event is accepted')
  ok(res.fresh.length === 1 && res.fresh[0].semanticId === 'evt|1',
     'C3: the first event becomes the visible bubble')
  rApi.pulseActivityAccept({ activity_events: { events: [ev(1, 'terminal')], next_cursor: 1 } })
  eq(pulseActivityQuerySuffix(), '?after_event_id=1', 'C4: the cursor advanced 0 -> 1')
}

console.log('PASS pulse-tick-reducer: ' + n + ' assertions')
