# CONTRACTS — its-in-your-head (frozen P0.2; additive-only, parent-adjudicated)

Frozen 2026-11-06. Any contract change = parent session adjudication, recorded in
git history. Lanes NEVER silently mutate a pinned entry.

## C1 — REST routes (mounted at `/api/plugins/its-in-your-head/`)

Pinned now (pinned shapes, additive keys allowed; shape changes = adjudication):

| Method | Path      | Status                          |
| ------ | --------- | ----------------------------- |
| GET    | /metrics  | live — ticker batch data      |
| GET    | /heatmap  | live — pulse heatmap data     |
| GET    | /recent   | live — recent events          |

Planned additions (S5/S7 land them; S6 may poll before they exist → 404-tolerant):

| Method | Path            | Lane |
| ------ | --------------- | ---- |
| GET    | /pulse          | S5   |
| GET    | /portrait       | S5   |
| GET    | /drift          | S5   |
| GET    | /context        | S7   |
| GET    | /settings       | S5   |
| POST   | /settings       | S5   |
| POST   | /reset          | S5   |

Rule: a lane may ADD routes; it may not rename, re-shape, or remove a pinned one.

### C1 adjudication (2026-11-07, PORTRAIT perception D4 Option B): `/portrait` gains the additive key `perception` (additive-key rule; the four-key shape `dimensions, confidence, user, agent` is unchanged and byte-compatible).

`perception` is an object with keys `patterns`, `dim_signals`, `perception_available`.
- `patterns` = list of objects, each with keys: `name` (string), `prompt_affinity` (float or None), `agent_affinity` (float or None), `margin` (float or None), `amplification` (float or None), `evidence` (int, 0 allowed), `confidence` (float or None), `repeated` (bool).
- `dim_signals` = dict keyed by dimension name, each value an object with keys `prompt` (float or None) and `agent` (float or None).
- `perception_available` (bool).

Empty-shape rule: when there is no evidence, `patterns` is an empty list, `dim_signals` keeps every dimension key with None values, and `perception_available` is False. Never invent values; routes never 500 on empty.

Fallback selection rule (E1 + E3/v7): in the isolated-child fallback, `perception` is filled from `pattern_affinity_history` as the LATEST ROW PER `top_pattern` (deterministic per-name ts desc, id desc tie-break; `repeated` = that name also WON — i.e. appears in a rank-1 row — within the up-to-3 rows immediately before its own latest row). E3/v7: the engine persists up to THREE rows per turn (rank 1 = winner, ranks 2/3 = runner-ups carrying that pattern's own derived values; `turn_key` groups a turn's rows; pre-v7 rows default to rank 1), so per-name entries take that name's latest row at ANY rank while the strongest-card entry (first in the list) is the latest RANK-1 row and the repeated-window counts rank-1 rows only. No rows or an empty table = empty perception shape with `perception_available` False. The agent track stays None in fallback and is never invented.

## C2 — event wire names (`broadcast_plugin_event(plugin_id, event, payload)`)

Bare dotted event basenames only (the gateway prefixes `plugin.its-in-your-head.`):
- `tick` — batched pipeline flush (live)
- `pulse` — tool-event accelerator, throttled ≤ 1/s (S4/S5)
- `settings` — settings change (S5)
Pinned set = {tick, pulse, settings}. New event names = adjudication.

## C3 — DDL single source of truth

`metrics.py` → `SCHEMA` string (source of truth, marked "Keep the two copies in sync").
`dashboard/plugin_api.py` carries a marked copy (dashboard mount must NOT import the
worker module — doc pitfall). G5 diffs the two normalized.

## C4 — settings keys (`plugin.yaml` `config_schema`)

Keys (land with S5; `adaptation.enabled` defaults **false**):
`perception.enabled`, `observation.reasoning`, `observation.retain_days`,
`pulse.prototype_cap`, `pulse.min_labeled`, `pulse.thresholds.{goal,good,friction}`,
`adaptation.enabled`, `adaptation.rules[]`,
`perception.encoder` (S2 adjudication 2026-11-06: values `tier-0` | `tier-1-onnx`,
default `tier-0`; Tier-1 ONNX revision/sha256 pin pending vendor download),
`perception.custom_patterns[]` (D3 upscale route B, default `[]`; each entry
an object `{name, prompt[], agent[]}` — paraphrase lists, never raw content),
`perception.reinforce` (D3 upscale route A, default **true**),
`perception.reinforce_min_amplification` (default `0.15`).

### D3 upscale adjudication (2026-11-07, L5): schema v6 `pattern_bank`

Schema v6 adds `pattern_bank` — one row per reinforced pattern name:
`pattern_name TEXT PRIMARY KEY`, `prompt_anchor BLOB NOT NULL`,
`agent_anchor BLOB NOT NULL`, `evidence INTEGER NOT NULL DEFAULT 0`,
`updated_at REAL NOT NULL`. Anchors are derived vectors only (float32
struct-packed BLOBs); `pattern_name` is a bank name (preset or custom),
never free text; no raw-content column exists. Reinforcement is an EMA
(`REINFORCE_ALPHA = 0.05`) on the AGENT anchor only — the prompt anchor
is authored ground truth and never drifts. The pattern-bank cache key is
`(model_version, PATTERN_BANK_VERSION, sha256(sorted custom+reinforced
inputs))`; `PATTERN_BANK_VERSION` stays `"1"` (preset content untouched,
custom/reinforced are additive inputs).

## C5 — hook matrix (live: pre_llm_call, post_api_request, api_request_error)

| Hook                    | Captures / role                                                        | Hot-path rule                     |
| ----------------------- | ---------------------------------------------------------------------- | --------------------------------- |
| pre_llm_call            | prompt capture + adaptation injection point (S7)                       | capture/enqueue only; `{"context"}` only from S7 injection |
| on_stream_delta         | first reasoning segment per turn ONLY (bounded buffer; async, drop-oldest) | embed in worker, never on delta path |
| on_stream_end           | stream completion + error signals                                      | enqueue                           |
| pre_tool_call           | LIVE row create/increment (synchronous, < 0.5 ms, no inference)        | immediate counters                |
| post_tool_call          | status/duration/result-type → activity capsule → queue                 | enqueue                          |
| post_api_request        | turn telemetry close (live)                                            | enqueue                          |
| api_request_error       | error signal (live)                                                   | enqueue                          |
| on_session_start        | restore analytical state                                               | enqueue                          |
| on_session_end          | flush queue                                                            | enqueue                          |

Manifest `provides_hooks` must equal the registered set (doctor drift gate);
`plugin.yaml` is parent-owned — lanes report deltas as contract flags.
Note: `plugins.stream_reasoning_deltas: true` is already set in this profile
(config.yaml line 459); the plugin's `observation.reasoning` key is the
plugin-side capture switch on top.

## C6 — enqueued event schema (worker flattens to rows)

`user_turn | tool_start | tool_end | reasoning_seg | stream_end |
session_start | session_end` dicts. Keys pinned per event type; the worker
accepts unknown types (no-op). Never persisted: raw reasoning text, raw tool
output (schemas audit in G7).

## C7 — file-ownership map (lanes touch ONLY their set)

- **Lane A (S1):** `core/` (new), `storage/` (new), `__init__.py`,
  `metrics.py` (pipeline base — extends, no rewrites), `tests/test_s1_*.py`
- **Lane B (S2/S3):** `perception/` (new: encoder, anchors, resemblance,
  portrait, transitions, svg renderer), `tests/test_s2_*.py`,
  `tests/test_s3_*.py`
- **Lane C (S4/S5/S7):** `pulse/`, `adaptation/` (new), `commands.py`,
  `dashboard/plugin_api.py` (route additions), `tests/test_s4_*.py`,
  `tests/test_s5_*.py`, `tests/test_s7_*.py`
- **Lane D (S6):** `desktop/`, `tests/test_desktop_*.py` (node checks)
- **Parent:** `plugin.yaml`, `CONTRACTS.md`, `tools/check_contracts.py`,
  `tests/test_structure.py`, `tests/acceptance/`, infra (junction/enable)

## C8 — desktop rules

Import surface: exactly `react`, `@hermes/plugin-sdk`, `react/jsx-runtime`.
Theme vars only (canvas: `getComputedStyle` resolve; CSS: `var(--…)`;
`transparent`/`currentColor` allowed; no `#hex`/`rgb(`/black/white literals).
96px pane layout budget: 22 (header/tabs) / 48 (main) / 18 (ticker) / 8 (seps).
Canvas sizing via ResizeObserver (width/height attributes, never CSS).
`useQuery` polling (5 s) = MANDATORY fallback; `host.onEvent` live
invalidation = optional accelerator. Pet state machine: 7 deterministic
states.

## C9 — commits

One commit per stage, conventional subject (`feat(scope): …`), parent-only
merges. Lanes report diffs; no lane commits or merges.
