# PULSE — LIVE event-rate velocity + cursor hold-model + fast cadence

Status: **SPEC (owner-reviewed, not yet implemented).** On "go", implement the
three coupled changes below. The confirmed delivery defect (pane 502,
`f41af9f`) is already fixed and is *not* in scope here.

## 0. The three confirmed model flaws (why this change)

1. **LIVE momentum is capped-32, not time-decayed** → high-activity tools cool.
   `momentum.value = 0.5 + 0.5*(recent_share − cum_share)` where `recent_share`
   comes from the **capped 32-capsule feed** (`matrix.py:553`
   `activity["recent_capsules"]`). Every active tool reads the same capped 32,
   so `recent_share` is uniform (~0.064) and the rank collapses to a function of
   *cumulative* share: `terminal` (cum 2574) sinks below the cap-4 line → its
   events are **HELD**, not animated. No time-decay exists anywhere (grep
   `half_life/decay/exp/window` in `momentum.py`+`matrix.py` → empty).
2. **`state.held` is an unbounded in-memory FIFO doing two jobs** — the raw
   arriving stream *and* "wait until the cell is visible" (`plugin.js:3553,
   3754`). It only shrinks when a tool's LIVE cell turns visible (`rePromoteHeld`
   `:3801`); it is never evicted (the 64-entry cap was removed in review). A long
   session with the tool at rank>4 grows it without bound.
3. **Cadence vestiges** — `PULSE_REDUCER_MS=1300` (`:2344`) is the real
   activity→bubble latency; `PULSE_POLL_MS=15000` (`:2338`) is a **legacy
   hourly-heat-lane vestige** (comment `:2480`), only the idle safety-net, not
   the bubble cadence. Portrait already runs at `PORTRAIT_POLL_MS=850` (`:2479`).

## 1. Owner-locked decisions (from this session)

- **C.1** = LIVE momentum becomes **event-rate velocity** computed from
  persisted `events` timestamps (free, native). A massive ungraded batch
  (e.g. 80× `compute_use`) spikes that tool's velocity → rises in LIVE; when the
  batch stops, events roll out of the window → it cools. No new mechanism.
- **B** = drop the in-memory `state.held` FIFO; **delayed promotion = a cursor
  into the persisted `events` table** (the "external toolkit"), flushed
  **FILO** (newest-first) when the tool's cell turns visible.
- **C.2** = `PULSE_REDUCER_MS` → 850 (match portrait); retire the 15s vestige.

## 2. Data already present (no schema change → C3 zero-risk)

- `events` table (`storage/migration.py:64`): `id` (PK, the cursor), `ts` REAL,
  `type`, `tool_name`, `status`, `error_type`/`error_message`.
  Index **`idx_events_tool (tool_name, ts)`** (`:102`) already supports a
  per-tool time-windowed count — the exact shape C.1 needs.
- Activity events are `type='tool_start'` (`_pulse_activity_events:294`).

## 3. Change C.1 — LIVE event-rate velocity (server)

**3.1 New shared function** `event_velocity(conn, now_ts, window_s) -> dict`
(new file `pulse/velocity.py`, or `matrix.py`):
```
SELECT tool_name, COUNT(*) FROM events
 WHERE type='tool_start' AND ts >= ? - ?
 GROUP BY tool_name            -- uses idx_events_tool
```
Returns `{tool: {"count": n, "per_s": n/window_s}}` for tools with ≥1 event in
the window (others → 0). `now_ts` = `time.time()`; `window_s` = module constant
**`PULSE_VELOCITY_WINDOW_S = 60`** (tunable; expose as a settings key later,
additive). Pure/DB-fault-tolerant: on fault → `{}` (velocity neutral, never 500).

**3.2 Additive input to the lane builders.**
- `build_lanes(...)` (`matrix.py:422`) and `build_matrix(...)` (`matrix.py:188`)
  gain an **additive optional kwarg** `event_rates: dict | None = None`.
  `None` → byte-identical to today (backward compat; the legacy
  `_pulse_lanes_from_rows` mirror and its tests stay green).
- **LIVE sort changes** when `event_rates` is provided: `_live_sort_key`
  (`matrix.py:401`) becomes
  `(has_activity DESC, velocity_count DESC, total_calls DESC, last_seen DESC,
   tool_name, tool_id)` — i.e. **velocity is the primary dynamic key** (fastest
  riser first), cumulative/last_seen are tie-breakers. When `event_rates` is
  `None`, fall back to the current `momentum.value`-primary key (unchanged).
- **Additive lane fields** on each LIVE entry (new keys only, C1-safe):
  `velocity` (count-in-window), `velocity_per_s`, `velocity_window_s` (the window
  used). Existing keys (`rank`, `momentum`, `surge`, `value`, `state`, …) are
  untouched → the semantic GOAL/GOOD/FRICTION lanes are unaffected.

**3.3 Injection at both /pulse producers** (single scoring path preserved):
- **Child/DB path** `_pulse_shape` (`plugin_api.py:747`): inside the `conn` try
  block (before `conn.close()` at `:815`), compute
  `event_rates = event_velocity(conn, time.time(), PULSE_VELOCITY_WINDOW_S)` and
  add `event_rates=event_rates` to `common` (`:912-918`).
- **Gateway in-process path** `engine.pulse_view` (`engine.py:1211`): the agent
  half has the same profile DB (it writes `events`); compute `event_rates` the
  same way and add it to `common` (`:1219-1232`).

## 4. Change B — cursor hold-model + FILO flush (server + pane)

**4.1 Server — additive per-tool backlog fetch (no new route → C1-safe; `/pulse`
is PLANNED).**
- `_pulse_activity_events(after_event_id, limit, tool_name=None,
  newest_first=False)` (`plugin_api.py:258`): when `tool_name` is set add
  `AND tool_name = ?`; when `newest_first` use `ORDER BY id DESC`. Response
  shape unchanged (`{events, next_cursor, has_more}`); the cursor math is
  identical. Defaults (`tool_name=None, newest_first=False`) = today's global
  ASC page (byte-compatible).
- `/pulse` route (`plugin_api.py:1370`) gains additive optional
  `tool_name: str | None = Query(default=None)` and `newest_first:
  bool = Query(default=False)`, forwarded into `_pulse_activity_events`.

**4.2 Pane — replace the unbounded `state.held` with a bounded per-tool
cursor map + FILO flush.**
- **State** (`plugin.js:3549-3554`): drop `held: []`; add
  `consumedByTool: {}` (tool → last-flushed event id; bounded by the number of
  tools, ~dozens). Keep `queued` (cap 4) and `lastActivityEventId` (per-surface
  dedup watermark).
- **Batch loop** (`:3717-3763`): for a **hidden** tool (`idx>=cap` or `idx===-1`)
  *stop* pushing the event object into a held array (delete the `:3754` push);
  instead simply **do not advance `consumedByTool[tool]`** — the event stays in
  the DB. The global dedup watermark (`lastActivityEventId`) and the module
  `PULSE_ACTIVITY.cursor` still advance as today (server-side delivery).
- **FILO flush on visibility** (`rePromoteHeld` `:3801`, and the lane-key/
  capacity effect that calls it): when a tool's LIVE cell becomes visible
  (rank ≤ cap), fire a **targeted** fetch
  `/pulse?tool_name=T&after_event_id=consumedByTool[T]&newest_first=1` →
  animate the returned events **newest-first** (bounded: ≤`MAX_CONCURRENT_BUBBLES`
  immediate, overflow to `queued`, staggered by `BUBBLE_STAGGER_MS`) → set
  `consumedByTool[T] = next_cursor`. **Remove** the old in-memory `state.held`
  re-resolve loops (`:3707-3715` and `:3816-3834`).
- **Invariants preserved:** accepted events are never dropped (they persist in
  `events`; `consumedByTool` is the only consumption marker); no foreign-cell
  routing (flush only when the tool's *own* cell is visible); idempotent dedup
  (per-tool cursor is monotonic, a re-promotion can't double-bubble).
- `activityDropped` counter (`:3554`): the model no longer drops — repurpose as
  a debug "flush overflow" counter or remove.

## 5. Change C.2 — fast cadence (pane constants)

- `PULSE_REDUCER_MS` `1300` → **`850`** (`plugin.js:2344`) — the real
  activity→bubble latency now matches the portrait cadence.
- `PULSE_POLL_MS` `15000` → **`5000`** (= `POLL_MS`, the mandatory fallback;
  retires the hourly-heat vestige). The **primary** path is still the ~850ms
  tick push (`broadcast_plugin_event` is live in the gateway tree —
  `hermes-agent/hermes_cli/plugin_events.py:66`); the poll is only the idle
  safety-net.

## 6. Contract surface (C1/C2/C3) — all clean

- **C1** (routes): `/pulse` is PLANNED; adding **additive Query params**
  (`tool_name`, `newest_first`) is fine — **no new route, no pinned-route
  removal**. Additive response keys (`velocity`, `velocity_per_s`,
  `velocity_window_s` on LIVE entries) = new keys only → OK. No existing key's
  shape changes.
- **C2** (events): no new `broadcast_plugin_event` basenames (still
  `{tick, pulse, settings}`) → OK.
- **C3** (DDL): **zero schema change** — velocity is computed from existing
  `events` rows; no new table/column → no DDL copy → OK.

## 7. Test gates + verification

- **Backend** (`uv run --with pytest --with fastapi`, currently 485 passed):
  - `test_event_velocity_*`: a 80-call ungraded `compute_use` batch in-window
    spikes that tool to LIVE rank 1; after the window elapses (now_ts advanced)
    it cools to 0 → falls out. `idx_events_tool` used.
  - `test_pulse_tool_name_filter_newest_first`: per-tool DESC page, cursor
    semantics, no-op baseline when no history; global ASC path byte-unchanged.
  - `test_build_lanes_event_rates_none_is_legacy`: `event_rates=None` →
    byte-identical to current output (keeps the legacy-mirror test green).
  - Fault-tolerance: `event_velocity` DB fault → `{}` (no 500).
- **Pane** (`node --input-type=module --check desktop/plugin.js`; desktop suite
  currently 18/18): add a coordinator test for FILO flush + `consumedByTool`
  boundedness (flush-on-visibility, no double-bubble, overflow to `queued`);
  verify live with the `desktop/pulse-bubble-sample.html` harness.
- **Contracts**: `python tools/check_contracts.py` (C1/C2/C3/C4/C7) must stay
  green.
- **Recycle** the `plugin_host_child` after backend changes (gateway
  auto-respawns ~30s; post-kill 502s are the startup window).

## 8. Risks / open items to confirm on "go"

1. **Gateway `pulse_view` DB access** — confirm the agent half can open the
   profile DB for `event_velocity` (it writes `events`, so it should; verify
   the handle).
2. **Per-tool flush = one extra round-trip** on each visibility promotion
   (bounded, only on promotion — acceptable).
3. **`PULSE_REDUCER_MS=850`** → more frequent `/pulse` refetches (bounded page,
   low load).
4. **Velocity window = 60s** is a starting constant; expose as an additive
   settings key if the user wants to tune it.
5. **FILO backlog cap** — flush ≤`MAX_CONCURRENT_BUBBLES` at a time; the rest
   stays behind the per-tool cursor (still in DB), so a later re-promotion
   flushes the remainder. No unbounded JS state.

## 9. File/line seam index (where each edit lands)

| # | File:line | Edit |
|---|---|---|
| C.1 | new `pulse/velocity.py` | `event_velocity(conn, now_ts, window_s)` + `PULSE_VELOCITY_WINDOW_S` |
| C.1 | `pulse/matrix.py:188,422` | additive `event_rates` kwarg to `build_matrix`/`build_lanes` |
| C.1 | `pulse/matrix.py:401` | `_live_sort_key` velocity-primary (when `event_rates` present) |
| C.1 | `pulse/matrix.py` (LIVE entry) | additive `velocity`/`velocity_per_s`/`velocity_window_s` |
| C.1 | `dashboard/plugin_api.py:~810,912` | compute + pass `event_rates` in `_pulse_shape` |
| C.1 | `engine.py:1219` | compute + pass `event_rates` in `pulse_view` |
| B   | `dashboard/plugin_api.py:258` | `_pulse_activity_events` + `tool_name`/`newest_first` |
| B   | `dashboard/plugin_api.py:1370` | `/pulse` additive Query params |
| B   | `desktop/plugin.js:3549` | drop `held:[]`, add `consumedByTool:{}` |
| B   | `desktop/plugin.js:3754,3707-3715,3816-3834` | stop in-memory hold; FILO flush-on-visibility |
| C.2 | `desktop/plugin.js:2344` | `PULSE_REDUCER_MS` 1300→850 |
| C.2 | `desktop/plugin.js:2338` | `PULSE_POLL_MS` 15000→5000 |
