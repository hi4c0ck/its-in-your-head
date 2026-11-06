# It's in Your Head — structure & owner slots

Briefing for the coding session. The manager (infra) laid down structure,
contracts, manifest, test harness, and install wiring; functional code is
owner work, per the slots below. Official references:
- https://hermes-agent.nousresearch.com/docs/developer-guide/plugins
- https://hermes-agent.nousresearch.com/docs/developer-guide/desktop-plugin-sdk

## Tree (what each file is)

```
its-in-your-head/               # unified package = one installable folder = OSS repo root
+-- plugin.yaml                 # manifest v2: id, version, provides_hooks (drift-checked vs register())
+-- __init__.py                 # register(ctx): wires the 3 observer hooks — BODIES ARE OWNER SLOTS
+-- embedder.py                 # embedding INTERFACE (sketch(text)->bytes); engine choice is an owner slot
+-- metrics.py                  # pipeline skeleton: queue + worker/storage placeholders; lazy-DB rule documented
+-- dashboard/
|   +-- manifest.json           # { "name": "its-in-your-head", "api": "plugin_api.py" } — the backend mount
|   +-- plugin_api.py           # FastAPI router stubs at /api/plugins/its-in-your-head/ (response shapes fixed)
+-- desktop/
|   +-- plugin.js               # desktop half: pane registration (bottom dock, 96px) + strip placeholder
+-- tests/                      # infra baseline (structure/contract tests, green) + owner placeholders
+-- pytest.ini                  # importlib import mode + pythonpath (repo root is a Python package — required)
+-- README.md / LICENSE (MIT) / .gitignore
+-- ARCHITECTURE.md             # this file
```

## Locked decisions (from owner — do not re-litigate)

1. Unified package, staged in the `plugin-studio` profile; final distribution =
   open-source repo `hi4c0ck/its-in-your-head` + `hermes://` install link.
2. Embedding: LIGHT, NO USER HARM — zero network per prompt; Tier-0 stdlib
   sketch first, Tier-1 local ONNX MiniLM-L6 as a later swap behind the same
   interface.
3. UI: small pane (NOT a statusbar indicator), fixed ~96px strip, tabbable with
   other user panes.
4. Hot-path rule: hooks capture/enqueue only; embedding + aggregation in a
   background worker.
5. Storage: WAL SQLite via `plugins.plugin_storage.plugin_db("its-in-your-head")`
   -> `plugin-data/its-in-your-head/data.db` — never in the plugin dir.

## Owner slots (defaults live in the code comments; override freely)

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
