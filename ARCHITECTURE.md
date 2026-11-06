# It's in Your Head — structure, contracts, implementation map

Briefing for the coding session, kept current. The manager (infra) laid down
structure, contracts, manifest, test harness, and install wiring; the
functional slots below are IMPLEMENTED as of v0.1.0 (committed per slot; see
git log) — the "owner slot" framing is retained only as a change-ownership
map, not as a TODO list. Official references:
- https://hermes-agent.nousresearch.com/docs/developer-guide/plugins
- https://hermes-agent.nousresearch.com/docs/developer-guide/desktop-plugin-sdk
- https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks
  (streaming-output-hooks section: bounded-queue delivery +
  `plugins.stream_reasoning_deltas` opt-in)

## Tree (what each file is)

```
its-in-your-head/               # unified package = one installable folder = OSS repo root
+-- plugin.yaml                 # manifest v2: id, version, provides_hooks (drift-checked vs register())
+-- __init__.py                 # register(ctx): wires the observer hooks (9-hook matrix, implemented)
+-- embedder.py                 # embedding INTERFACE (sketch(text)->bytes) + Tier-0 implementation (implemented)
+-- metrics.py                  # event pipeline: bounded queue + worker thread + storage (implemented)
+-- engine.py                   # process-level perception engine facade (portraits/pulse/drift views)
+-- adaptation/                 # deterministic rule engine + bounded injection builder (opt-in, off by default)
+-- dashboard/
|   +-- manifest.json           # { "name": "its-in-your-head", "api": "plugin_api.py" } — the backend mount
|   +-- plugin_api.py           # FastAPI router at /api/plugins/its-in-your-head/ (implemented, response shapes fixed)
+-- perception/                 # Tier-0 sketch + resemblance calibration + ONNX Tier-1 worker client/encoder
+-- onnx/ + onnx-worker/        # pinned Tier-1 manifest (revision + sha256 + sizes) and the isolated worker project
+-- desktop/
|   +-- plugin.js               # desktop half: 96px strip pane, semantic PULSE/PORTRAIT/DRIFT views (implemented)
+-- pulse/ + storage/           # prototype store + WAL SQLite layer (migration-aware since v0.1.1)
+-- tests/                      # full suite (contract + behavior + desktop node checks), CI-gated
+-- pytest.ini                  # importlib import mode + pythonpath + repo-local basetemp + temp-budget retention
+-- README.md / LICENSE (MIT) / .gitignore
+-- ARCHITECTURE.md             # this file
```

## Locked decisions (from owner — do not re-litigate)

1. Unified package, staged in the `plugin-studio` profile; final distribution =
   open-source repo `hi4c0ck/its-in-your-head` + `hermes://` install link.
2. Embedding: LIGHT, NO USER HARM — zero network per prompt; Tier-0 stdlib
   sketch first, Tier-1 local ONNX as a swap behind the same interface
   (IMPLEMENTED: isolated PM-managed worker, pinned manifest, size-capped
   atomic downloads, automatic Tier-0 fallback on any failure).
3. UI: small pane (NOT a statusbar indicator), fixed ~96px strip, tabbable with
   other user panes.
4. Hot-path rule: hooks capture/enqueue only; embedding + aggregation in a
   background worker.
5. Storage: WAL SQLite via `plugins.plugin_storage.plugin_db("its-in-your-head")`
   -> `plugin-data/its-in-your-head/data.db` — never in the plugin dir.
6. Streaming reasoning deltas ON (owner: "we 100% use this feature") —
   `plugins.stream_reasoning_deltas: true` is required in every profile config
   that runs this plugin; raw model thinking then reaches the plugin, so the
   README privacy section MUST disclose it. Enforced by config guard gate 4.

## Functional slots (IMPLEMENTED as of v0.1.0 — ownership map, change notes below)

- **Hooks** (`__init__.py`): `pre_llm_call` (prompt capture + the only hook that
  can inject `{"context": ...}` — the "proxy" power), `post_api_request`
  (per-call metrics: usage/duration/finish_reason + correlation fields),
  `api_request_error` (error ticks).
- **Metrics** (`metrics.py`): event schema, worker thread, aggregation, optional
  `broadcast_plugin_event("its-in-your-head","tick",...)` push (desktop
  subscribes via `host.onEvent('plugin.its-in-your-head.tick', ...)`; the
  polling fallback stays mandatory).
- **Backend** (`dashboard/plugin_api.py`): implement the three routes to their
  fixed response shapes; bootstrap the schema in-route (the dashboard mount
  can't rely on the worker).
- **Embedding** (`embedder.py`): pick Tier-0 sketch params (n, DIM, norm) or
  Tier-1.
- **Streaming output hooks (IMPLEMENTED — reasoning capture live)**: `on_stream_start` /
  `on_stream_delta` / `on_stream_end` (+ `on_interim_message`) — the live-tick
  source for the strip. Doc semantics (verified 2026-11-06,
  /docs/user-guide/features/hooks "Streaming output hooks"): observer-only,
  delivered through host-owned bounded queues (one worker per callback, never
  inline on the token path), returns ignored, exceptions logged. Capture shape:
  `on_stream_delta` = light counters only (tick the strip; do NOT store delta
  streams); `on_stream_end` = the assembled final text (the store point).
  **Reasoning deltas are NOT exposed to plugins by default**: `on_stream_delta`
  only carries `kind="reasoning"` when the user opts in via
  `plugins: stream_reasoning_deltas: true`. LOCKED (decision 6): we use this feature 100% — the config guard
  (verify loop gate 4) enforces `plugins.stream_reasoning_deltas: true` in the
  running profile; raw model thinking now reaches the plugin, so the README
  privacy section must disclose it. Registering any streaming hook
  requires BOTH: register in `__init__.py` AND add to `provides_hooks` in
  plugin.yaml (drift check).
- **Desktop** (`desktop/plugin.js`): heat canvas (ResizeObserver + theme vars,
  resolved once via getComputedStyle) + I/O tick frames; wire the `host.onEvent`
  live path.

## Hard contracts (doc-pinned; doctor/tests enforce some of these)

- `register(ctx)` runs once; crash = plugin disabled, Hermes continues.
- Hook callbacks: `**kwargs` (dispatcher injects only named context keys);
  observer returns are ignored.
- Tool handlers (if any later): return `json.dumps(...)`, never dicts; schema
  `description` = routing text.
- `ctx.state` <= 10 MiB, atomic, profile-scoped — cursors only; time-series ->
  SQLite.
- `ctx.rest` is namespace-scoped + traversal-rejected; `ctx.socket`
  auto-reconnects but is a no-op on OAuth remotes -> every consumer keeps its
  polling fallback.
- Desktop JS: only `@hermes/plugin-sdk` + `react`/`jsx-runtime` imports;
  `jsx()` calls, never JSX syntax (the file loads uncompiled); no hardcoded
  colors (theme vars); canvas ResizeObserver.
- `provides_hooks` in plugin.yaml must exactly match what `register()`
  registers (catalog admission + doctor drift check).
- Never open the plugin DB at `register()` time (doctor's temp-home cleanup;
  WinError 145 on Windows).

## Verify loop (manager-owned gates — run before hand-back)

1. `hermes -p plugin-studio plugins doctor its-in-your-head --ci` -> exit 0
2. `<hermes venv python> -m pytest -q` -> green
3. Desktop: save -> hot reload (linked junction) or Rescan -> pane visible, no
   error toast
4. Config guard (reasoning-delta feature, locked ON):
   `hermes -p plugin-studio config get plugins.stream_reasoning_deltas` -> `true`.
   Missing/absent = `on_stream_delta` kind="reasoning" SILENTLY no-ops (no error,
   no data) — re-apply with `hermes -p plugin-studio config set
   plugins.stream_reasoning_deltas true`.
