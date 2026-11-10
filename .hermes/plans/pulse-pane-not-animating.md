# PULSE pane "not seeing animation" — diagnosis

## Symptom
User: "i not see em" (no PULSE activity bubbles in the live IYH pane).

## Root causes (in order of impact)
1. **Backend /pulse 502 (the real block).** From ~23:16 the isolated
   `plugin_host_child` 502ed every `/pulse` call:
   `ModuleNotFoundError: No module named 'pulse'`. The child's startup cwd is
   NOT the plugin root, and v0.1.10's authoritative path in
   `dashboard/plugin_api.py` hard-imports `pulse.matrix` / `pulse.prototypes`
   inside `_pulse_shape()`. No data reached the pane → nothing to animate.
   **Fix @`f41af9f`:** bootstrap the plugin root onto `sys.path` from
   `__file__` when the bare import fails (child-cwd-independent). Killed the
   stale child; gateway respawned it; 502s stopped 23:25:13.
   Backend suite green (485 passed, 1 skipped).

2. **`Gateway ticket bootstrap failed` = benign background noise.**
   Present since 2 days ago at a low rate (1–7/min intermittently); the
   desktop app retries and recovers. NOT a persistent block — do not chase it.

3. **Bubbles only fire for the live top-4 lane tools:**
   `delegate_task`, `session_search`, `memory`, `vision_analyze` (agent-side).
   The user's own `terminal` (~rank 26) and `execute_code` (~rank 23) are
   HELD, outside the cap-4 compact strip → **the user's own work never
   animates.** This is why, between agent top-4 events, the pane is silent.

## No publish step needed
`desktop-plugins/its-in-your-head/plugin.js` is byte-synced to v2 (`81f0d21`).
The pane loads the renderer via Blob-URL ESM import; the app watcher re-syncs
on content change. There is no separate "publish" action.

## If still invisible after a fresh top-4 event
The pane tab may be holding a **stale Blob-URL module instance**. One-off
refresh of the IYH pane tab (close/reopen, or reload) forces a re-import.
This is the only manual step; it is not a publish.

## Verify-now state (as of ~23:39)
- Pane's last confirmed poll cursor: `after_event_id=32141`.
- Top-4 tool_start events AFTER that cursor (should animate):
  `32151 session_search`, `32158 vision_analyze`,
  `32192 memory`, `32196 memory`, `32200 memory`.
- High-water mark: `32204`. Pane polls every 15s (PULSE_POLL_MS).
- Expect up to 4 concurrent bubbles (cap-4) with the rest held in the queue.
