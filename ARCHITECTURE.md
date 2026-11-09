# It's in Your Head — structure, contracts, implementation map

Briefing for the coding session, kept current. The manager (infra) laid down
structure, contracts, manifest, test harness, and install wiring; the
functional slots below are IMPLEMENTED — v0.1.0 skeleton, v0.1.1 hardening
(P0 migration, P1 perception wiring + desktop semantic views + ONNX variant
split + context detection), v0.1.2 maintenance (PULSE LIVE column, drift
durability, explicit ONNX repin, in-flight-aware flush), v0.1.9 PULSE &
Surface Redesign (restart hydration, per-cell readiness, four independent
lanes + additive v2 envelope, measured bottom-bar/sidebar compositions,
bubble-arrival transition). See git log per slot
— the "owner slot" framing is retained only as a change-ownership map, not a
TODO list. Official references:
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
+-- metrics.py                  # event pipeline: bounded queue + worker thread + storage
|                                 # + in-flight-aware flush (FlushResult: drained/pending/failed)
+-- engine.py                   # process-level perception engine facade (portraits/pulse/drift views)
+-- adaptation/                 # deterministic rule engine + bounded injection builder (opt-in, off by default)
+-- dashboard/
|   +-- manifest.json           # { "name": "its-in-your-head", "api": "plugin_api.py" } — the backend mount
|   +-- plugin_api.py           # FastAPI router at /api/plugins/its-in-your-head/ (implemented, response shapes fixed)
+-- perception/                 # Tier-0 sketch + resemblance calibration + ONNX Tier-1 worker client/encoder
+-- onnx/ + onnx-worker/        # pinned Tier-1 VARIANT manifest (schema v2: int8 default ~129 MB,
|                                 # fp32 optional; revision + sha256 + sizes) and the isolated worker project
+-- desktop/
|   +-- plugin.js               # desktop half: 96px strip pane, semantic PULSE/PORTRAIT/DRIFT views
|                                 # (PULSE: four independent lanes in measured bottom-bar/sidebar
|                                 # compositions; v2 bubble transition; theme vars; petstate; polling + onEvent)
+-- pulse/ + storage/           # prototype store + deterministic feedback detector (pulse/feedback.py)
|                                 # + WAL SQLite layer (schema v3: idempotent migrate(), portrait_history)
+-- tests/                      # full suite (contract + behavior + desktop node checks), CI-gated
+-- tools/                      # maintainer tooling: fetch_onnx_model (verify-only / --repin atomic
|                                 # promotion), verify_onnx_model, check_contracts (G5), temp_budget_check
+-- pytest.ini                  # importlib import mode + pythonpath + repo-local basetemp + temp-budget retention
+-- README.md / LICENSE (MIT) / .gitignore
+-- ARCHITECTURE.md             # this file
+-- MOVEMENT-CONTRACT.md        # portrait motion: template-owned movement contract + 5-stage tracker
```

## Locked decisions (from owner — do not re-litigate)

1. Unified package, staged in the `plugin-studio` profile; final distribution =
   open-source repo `hi4c0ck/its-in-your-head` + `hermes://` install link.
2. Embedding: LIGHT, NO USER HARM — zero network per prompt; Tier-0 stdlib
   sketch first, Tier-1 local ONNX as a swap behind the same interface
   (IMPLEMENTED: isolated PM-managed worker, pinned VARIANT manifest —
   int8-only default install ~129 MB, fp32 optional variant, size-capped
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
   Sensitivity is privacy-only: no LLM executes against this data (deterministic
   cosine/EMA scoring); a model's own reasoning can nudge its interpreted track
   (self-steering), which is bounded — evidence-gated confidence, interpretation
   is evidence not proof, and adaptation (the only downstream consumer) is
   off by default.

## Functional slots (IMPLEMENTED as of v0.1.0 — ownership map, change notes below)

- **Hooks** (`__init__.py`): `pre_llm_call` (prompt capture + the only hook that
  can inject `{"context": ...}` — the "proxy" power), `post_api_request`
  (per-call metrics: usage/duration/finish_reason + correlation fields),
  `api_request_error` (error ticks).
- **Metrics** (`metrics.py`): event schema, worker thread, aggregation, optional
  `broadcast_plugin_event("its-in-your-head","tick",...)` push (desktop
  subscribes via `host.onEvent('plugin.its-in-your-head.tick', ...)`; the
  polling fallback stays mandatory).
- **Backend** (`dashboard/plugin_api.py`): route set pinned to fixed response
  shapes — GET /pulse, /portrait, /drift (persisted portrait_history, probe
  available only with data), /context (source rows detected from the actual
  profile at request time: present/absent/unknown, never "present" by
  /settings, POST /settings, POST /reset, plus /metrics, /heatmap,
  /recent legacy reads; bootstrap the schema in-route via the idempotent
  `storage.migration.migrate()` (v5; the dashboard mount can't rely on the
  worker). /portrait carries the additive `perception` key (pattern
  affinities, empty shape when no evidence — see CONTRACTS.md C1).
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
  plugin.yaml (drift check). Downstream wiring (v0.1.1): assembled
  reasoning segments flow through `core.collector` into the engine, which
  embeds them through the CONFIGURED backend (tier-following: same vector
  space as prompts — Tier-0 sketch stays deterministic/zero-network, Tier-1
  improves both signals); deterministic explicit-feedback phrases
  (`pulse/feedback.py`) nudge prototypes via `update_prototype` when
  evidence-backed. Pattern scoring (`perception/patterns.py`, v0.1.x): a
  zero-network preset pattern bank (prompt-side anchors reuse the
  six-dimension positive banks; agent-side anchors are authored first-person
  agent-voice lists) scores every turn — prompt_affinity vs agent_affinity
  kept separate, plus competitor margin, amplification, and an
  evidence-fused confidence; persisted as derived numbers only
  (`pattern_affinity_history`, schema v5 — no free-text, no raw content);
  exposed on /portrait via the additive `perception` key and rendered as the
  Perception section inside the expanded portrait view (no new surface).
- **Desktop** (`desktop/plugin.js`): 96px strip pane with semantic views —
  PULSE (v0.1.9 redesign): four INDEPENDENT lanes — LIVE (freshness /
  activity / momentum: surging / steady / cooling), GOAL (goal-vector
  similarity + reference support), GOOD (GOOD-prototype similarity +
  support), FRICTION (user + objective evidence, sources kept distinct) —
  each lane ranks its OWN tools (no global row ordering, no separate tool
  name rail); equal-width lanes (repeat(4, minmax(0,1fr))). Every visible
  cell owns its own truncated tool name + a compact dot-matrix evaluation
  (quiet, no large score numerals, no rainbow gradients; support is a
  secondary treatment, never the score); empty slots stay empty. Cells
  carry per-cell readiness (empty / learning / provisional / ready —
  thresholds gate confidence, not visibility; low-sample results stay
  visible as provisional). A meaningful evaluation change (value/level/rank
  above the backend hysteresis threshold) runs the bubble-arrival
  transition: two-identity model (semantic metric|tool + visual
  metric|slotIndex), staged commits (old name stays in the slot until
  arrival, then a short arrival flash), bounded transition coordinator
  (supersede / retarget / coalescing under a named cap, re-measure or
  cancel on resize, reduced-motion commits immediately, first hydration is
  always static). Composition is measured, not fixed: the 96px bottom strip
  keeps the current registration (user-resizable initial claim); at narrow
  viewport a right-zone sidebar pane is auto-selected (measured pane box +
  host.state.viewport atom, feature-detected for older hosts). The /pulse
  API serves an ADDITIVE v2 envelope (schema_version 2, lane_order,
  lanes{live,goal,good,friction}) next to the legacy rows/columns —
  byte-compatible, never omitted. The expanded inspection keeps
  `buildPulseVerdict` (word-only verdict, no digits) and `pulseRecency`
  (pure — takes `nowSec` in, never reads the clock; now / recent / today /
  stale from `last_seen`); the legacy band-ramp helpers (`pulseBand` et
  al.) remain for the legacy-rows fallback path and the expanded
  projection. The PULSE/DRIFT split is explicit: PULSE owns the NOW
  snapshot (activity + similarity + freshness + momentum); DRIFT owns
  multi-point history/trend (portrait-tab sparklines) and PULSE gains no
  sparkline/delta.
  PORTRAIT (six dimensions + trend), DRIFT (history; honestly "no drift data
  yet" when empty). Theme vars resolved once via getComputedStyle, canvas
  ResizeObserver, `host.onEvent` live path + mandatory polling fallback.
  (The legacy heat-canvas + streaming-capture description is obsolete — the
  strip now renders the semantic views above.)

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
2. `<hermes venv python> -m pytest -q` -> green (267 as of v0.1.2 tree;
   desktop node suite: `node tests/test_desktop_petstate.mjs`,
   `tests/test_desktop_pulse.mjs`, `tests/test_desktop_themecompat.mjs`,
   `node --check desktop/plugin.js`)
3. Desktop: save -> hot reload (linked junction) or Rescan -> pane visible, no
   error toast
4. Config guard (reasoning-delta feature, locked ON):
   `hermes -p plugin-studio config get plugins.stream_reasoning_deltas` -> `true`.
   Missing/absent = `on_stream_delta` kind="reasoning" SILENTLY no-ops (no error,
   no data) — re-apply with `hermes -p plugin-studio config set
   plugins.stream_reasoning_deltas true`.
5. `python tools/check_contracts.py` -> G5 CLEAN (C1-C4 contracts).
6. `python tools/temp_budget_check.py .pytest-basetemp` -> OK (100 MB total /
   50 MB single-file hard gate; guards against test-temp disk growth).
7. CI (`.github/workflows/ci.yml`): 6-matrix (win/linux x py 3.11-3.13) —
   contracts + suite + temp-budget + node checks on every push.

## Maintenance notes carried from reviews

- P3 (open decision): `register()` costs ~500 ms, of which 477 ms is the
  `import fastapi` pulled in via the commands/dashboard chain inside
  register(); module import itself is ~143 ms. Deferred fix = lazy command
  imports (~480 ms off the load path, zero-I/O rule intact).
- ONNX repin contract: no-flag `tools/fetch_onnx_model.py` is VERIFY-ONLY
  against the pinned manifest (zero network); `--repin` stages
  `manifest.staged.json` + a staged model dir, and promotes ATOMICALLY
  (single same-volume rename) only after verify + smoke pass; any failure
  leaves the pinned manifest byte-identical.
- `metrics.flush()` returns a FlushResult (drained/pending/in_flight/failed);
  `drained` is true only when everything enqueued in the flush window is
  committed — failed batches are reported, dropped, never retried.
- One observed order-sensitive flake after the F2+F4 merge (4 consecutive
  full-suite greens since); CI monitors, root-cause if it reappears.
