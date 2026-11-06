# It's in Your Head

Local prompt-analytics + response-signal plugin for Hermes Agent (desktop).

- **Agent half:** observer hooks (`pre_llm_call`, `post_api_request`, `api_request_error`)
  capture turns and provider calls; a background worker sketches prompts
  (Tier-0 zero-network 3-gram hash embedder) and writes WAL SQLite metrics.
- **Backend:** `dashboard/plugin_api.py` → `GET /metrics | /heatmap | /recent`
  under `/api/plugins/its-in-your-head/`.
- **Desktop half:** a small bottom-docked strip pane (~96 px) with heat cells
  and recent I/O ticks; live via `plugin.its-in-your-head.tick` events,
  5 s polling fallback.

## Privacy

Everything stays on your machine: prompt text and metrics land in
`plugin-data/its-in-your-head/data.db` (WAL SQLite). The embedder makes
**zero network calls**. Nothing is uploaded.

## Install (manual)

Copy this folder into `~/.hermes/plugins/`, add `its-in-your-head` to
`plugins.enabled` in `config.yaml`, then enable the desktop half in
Capabilities → Plugins. Verify with `hermes plugins doctor its-in-your-head`.

## Dev install

`hermes://plugin/install?repo=hi4c0ck/its-in-your-head&enable=1`
