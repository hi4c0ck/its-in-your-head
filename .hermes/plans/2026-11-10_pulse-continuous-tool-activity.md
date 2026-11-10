# PULSE — Continuous Tool Activity (v0.1.11 implementation plan)

Source spec: user-provided plan ("preserve every tool event, batch refreshes,
animate each event independently"). This file records the code-verified
anchors and fills the spec's open points with concrete decisions.
Status: PREPARED — awaiting start command.

## 0. Code-verified anchors (spec claims vs actual main @ 3fe0b6b)

| Spec claim | Verified | Anchor |
|---|---|---|
| worker order persist → tick → engine | CONFIRMED | `metrics.py:287-299` (`_persist_batch` → `_flush_tick` → `_engine.on_events`) |
| three distinguishable log points | CONFIRMED | `metrics.py:243` "tick broadcast failed", `:317` "batch persist failed (events dropped)", `:299` "engine feed failed (state dropped)" |
| events table carries id/ts/type/tool_name/tool_call_id | CONFIRMED | `storage/migration.py:64,88`; `metrics.py:68` `_COLUMNS` |
| `events.tool_start(tool_name, tool_call_id, ...)` | CONFIRMED | `core/events.py::tool_start` |
| `/pulse` takes no query params today | CONFIRMED | `dashboard/plugin_api.py:1301` `async def pulse():` |
| broad invalidation on tick | CONFIRMED | `desktop/plugin.js:5370` `invalidateQueries({queryKey:[ctx.source]})` inside `host.onEvent('plugin.its-in-your-head.tick', ...)` |
| PULSE query key + 15s poll | CONFIRMED | `:2167 PULSE_POLL_MS=15000`, `:4472 [source,'iyh-pulse']` |
| `pulseLanesKey` omits LIVE activity | CONFIRMED | `:~3456` key = semanticId+quantizedValue+level+state only; LIVE entries carry `activity{}` not in key |
| constants 450/150/4/60 | CONFIRMED | `:2946-2947 MAX_CONCURRENT_BUBBLES=4, BUBBLE_STAGGER_MS=60`, `:2772-2773` |
| stable staggerMs (no restart bug) | SHIPPED | `3fe0b6b` — build on it, do not regress |
| coordinator superseding by semanticId | CONFIRMED | `ingest` filters active/queued by `semanticId` — activity ids `evt|<eventId>` never collide with `metric|tool_id` ✓ |
| PULSE data into PulseLanes | CONFIRMED | `PulseLanes({lanes: rawLanes, capacity, surfaceBox, available})` `:3292`; `coord.ingest(rawLanes, cap, …)` `:3393`; `rest('/pulse')` in queryFn `:4473` |
| DB access pattern | CONFIRMED | `storage/database.py:40` fresh WAL connection per call, caller closes; NO busy_timeout today (add locally in helper only) |
| plugin version | `plugin.yaml: 0.1.9` — bump to 0.1.11 with the release commit |

## 1. Vision decisions (spec gaps, closed)

**V1 — Lane partition.** When a PULSE refresh carries accepted activity
events, LIVE-lane visual ownership moves to the event path:
`detectPulseTransitions(prev, next, seq, { excludeMetrics: ['live'] })`
— no LIVE rank-change/value bubbles while activity drives LIVE. GOAL/
GOOD/FRICTION keep semantic detection unchanged. When a refresh carries
NO activity events (quiet window / fallback path), the LIVE semantic
path stays active (graceful degradation to today's behavior).

**V2 — LIVE content is immediate, semantic lanes hold.** Ingest with
`activityDriven` sets `committed.live = next.live` immediately (server
ranking is authoritative; counts just changed). goal/good/friction keep
holding their old content until their own semantic bubble arrives.
This is what makes "no duplicate rank-change bubble" truthful: the
cell simply re-renders, no second bubble.

**V3 — Activity transition identity.** `kind: 'activity'`,
`semanticId: 'evt|' + eventId`, `metric: 'live'`, tool, destinationSlot
resolved from the tool's LIVE rank (rank-1). Unique per event → five
calls = five bubbles, FIFO, cap 4 shared with semantic bubbles,
overflow queued, promotion assigns staggerMs (existing `3fe0b6b`
mechanism, unchanged).

**V4 — Held events (spec §10).** At dispatch:
- tool in visible LIVE prefix → immediate transition;
- tool in LIVE lane but rank > capacity → HELD in a bounded
  FIFO `held[]` (max 64, drop-oldest + counter); re-checked on every
  subsequent `coord.activity()` call (capacity grows / tool rises);
- tool absent from the LIVE lane entirely (registry wiped) → dropped,
  counted, one-line log. Never animated to a foreign cell.

**V5 — Hydration (spec §3.3 rule 1, §7.1 case 1).** No cursor →
server returns `events: []` + `next_cursor = MAX(id)` (high-water
mark). Desktop stores it as `lastAccepted`; nothing animates. All
animation is strictly post-cursor.

**V6 — Cursor ownership (spec §4.4 rule 6).** Module-level
`PULSE_ACTIVITY = { lastAccepted, accepted: Set (LRU-capped 512), }`.
Cursor advances ONLY after events are pushed into the queue. A
refetch without acceptance re-delivers the same ids; the accepted
Set de-dupes → no double bubbles. Two surfaces (bottom + sidebar)
share the query + the module state; each surface's own coordinator
animates its own visible bubbles (consistent with existing
per-surface semantic behavior).

**V7 — Reducer.** Module singleton `createPulseTickReducer(refresh, {ms: PULSE_REDUCER_MS=1300, timers})`:
first tick → timer starts; later ticks → dirty flag only (NO timer
restart — fixed window, not trailing debounce); window fire →
`refresh()` unless in-flight, then dirty + reschedule on settle.
`settle()` is called from the PULSE queryFn success wrapper. 15s poll
remains the recovery path (unchanged). Both surface tick listeners
feed the ONE reducer; refresh is a targeted
`invalidateQueries({ queryKey: [ctx.source, 'iyh-pulse'] })`
(replaces the broad `[ctx.source]` invalidation — spec §4.3).

**V8 — Pagination (spec §4.4 rule 5).** queryFn loops `has_more`
pages up to 8 per refresh, then leaves `PULSE_ACTIVITY.dirty` so the
next window drains the rest (no unbounded burst).

**V9 — pulseLanesKey LIVE extension (spec §6).** LIVE segment gains
`calls_started:calls_completed:errors:last_seen` (last_seen floored to
1s). Key churn on activity is intentional: the ingest effect re-runs,
`detectPulseTransitions` with V1's exclusion returns nothing semantic,
the activity effect owns the visuals.

**V10 — Worker reorder (spec §3.2).** New order:
`_persist_batch → _prune (every N) → engine feed (try/except kept)
→ _flush_tick → privacy scrub (last, invariant kept: engine embeds
before scrub)`. The scrub MUST stay after the engine feed.

## 2. File-level change map

### Backend
1. `metrics.py` `_worker_main`: move the engine-feed block ABOVE
   `_flush_tick(len(batch))`; keep isolation; scrub stays last.
2. `dashboard/plugin_api.py`:
   - `_pulse_activity_events(after_event_id=None, limit=256)` —
     short-lived `database.connect()` + `PRAGMA busy_timeout=2000`
     locally; `SELECT MAX(id) FROM events` for the high-water mark;
     `WHERE id > ? AND type='tool_start' ORDER BY id ASC LIMIT ?+1`;
     fault-isolated: any DB error → `{"events": [], "next_cursor":
     after_event_id or 0, "has_more": False}` (route never 500s).
   - `GET /pulse` gains `after_event_id: int | None = Query(default=None)`;
     both paths (live view + `_pulse_shape` fallback) get
     `view["activity_events"] = _pulse_activity_events(after_event_id)`.
     Additive only: `rows`, `lanes`, `lane_order`, `schema_version`
     untouched (v2 envelope + one new key).

### Desktop (`desktop/plugin.js`)
3. Constants: `PULSE_REDUCER_MS = 1300` (near PULSE_POLL_MS).
4. `createPulseTickReducer(refresh, { ms, timers })` — pure factory,
   injectable timers/clock (unit-testable without DOM).
5. `PULSE_ACTIVITY` module state (V6) + `pulseActivityQuery(cursor)`
   helper building `/pulse?after_event_id=N`.
6. `detectPulseTransitions(prev, next, seq, opts)` — optional
   `opts.excludeMetrics` (default none → zero behavior change for
   existing callers/tests).
7. `createPulseBubbleCoordinator`:
   - `ingest(lanes, cap, { reducedMotion, activityDriven })` → V2 live
     immediate-commit when `activityDriven`;
   - `activity(events, live, cap)` → V3/V4 dispatch (held[], dropped
     counter, transitions into the same active/queued with cap +
     staggerMs + sequence + `kind:'activity'`);
   - arrival of activity transitions: NO `pulseCommitLane` for live
     (content already immediate); flash on destination cell still
     plays (per-slot ownership guard unchanged).
8. `pulseLanesKey` — V9 LIVE extension.
9. `PulseLanes`: new effect for the activity batch (props:
   `activityEvents` passed from Strip; module-level dedupe; call
   `coord.activity(...)`; schedule travel timers via the same
   `scheduleArrival` — stable staggerMs from V3).
10. Strip/register: PULSE queryFn → `pulseActivityQuery` + settle
    wrapper; tick listener → `reducer.onTick(ctx.source)` + targeted
    invalidation on window fire (replace line-5370 broad call).

### Tests
11. NEW `tests/test_pulse_activity_events.py` (backend):
    - no-cursor baseline (empty + high-water mark),
    - one new tool_start → one event,
    - five calls same tool → five ascending,
    - multi-tool ascending event-id order,
    - repeated cursor → no duplicates,
    - pagination (limit, has_more, no skips),
    - non-tool types excluded (user_turn / tool_end / api rows),
    - worker-order: call-log monkeypatch of `_persist_batch` /
      engine feed / `_flush_tick` → assert persist→engine→tick
      (via the flush-barrier API, no thread races).
12. NEW `tests/test_desktop_pulse_reducer.mjs`: fixed-window
    semantics with fake timers — multi-tick → one refresh; tick does
    not restart the window; tick during in-flight is not lost;
    settle reschedules when dirty; refresh target is the PULSE key
    only (reducer emits the key, not the broad one); 15s poll path
    independent.
13. `tests/test_desktop_bubble.mjs` +10 cases (spec §7.3):
    hydration no-replay; 1 call → 1 bubble; 5 same-tool → 5 bubbles
    (THE regression for today's behavior); no semantic superseding of
    activity events; 4 simultaneous = cap; >4 overflow queued FIFO;
    multi-tool destination resolution; LIVE rank change in an active
    window → no duplicate rank-change bubble; queued event resolves
    destination from LATEST live ordering at promotion; repeated
    fetch accepted-ids never re-animate.
14. Regression gates: composition, perception (SHA), bubble,
    lanes/matrix/lifecycle, metrics/collector, dashboard API,
    `tools/check_contracts.py`, strict ESM check
    (`node --input-type=module --check` — never bare `node --check`).

## 3. Hard constraints (spec §1, kept)
No argument/result capture, no reasoning/feedback changes, no
embedding/threshold/GOOD-FRICTION changes, no lane-ranking algorithm
changes, no styling changes, no new LLM/IO. Tool-start hook stays a
no-op enqueue (no timers/debounce in `collector.py`).

## 4. Order of work (isolated, failures stay local)
1. metrics worker reorder + order test (green before anything else).
2. cursor helper + `/pulse` field + backend cursor tests.
3. desktop: reducer + PULSE_ACTIVITY + queryFn/invalidation + key
   extension + reducer tests.
4. coordinator: `activity()` + `activityDriven` ingest + semantic
   exclusion + the 10 bubble cases.
5. ESM check, full regression, pane copy sync
   (`desktop-plugins/its-in-your-head/plugin.js` md5 parity).
6. live smoke A–H (spec §8) on the installed host; log inspection for
   the three distinguishable failure lines.
7. `plugin.yaml` version 0.1.9 → 0.1.11, release commit, push.

## 5. Risks / open verifications
- Cross-process WAL reads: short-lived conn + local busy_timeout=2000
  (do NOT touch global `connect()`).
- Two surfaces both animating: accepted (existing per-surface model).
- Event burst > cap: held[] bounded 64 drop-oldest + counter — PULSE
  can't OOM; counter visible in debug logs only (no UI surface yet).
- `last_seen` in the lanes key: intentional churn; verify no
  animation-loop when activity is idle (key stable → no effect).
- Hermes host version: spec targets v0.23.0 — verify installed host
  build id before smoke; tick event name
  `plugin.its-in-your-head.tick` unchanged.
