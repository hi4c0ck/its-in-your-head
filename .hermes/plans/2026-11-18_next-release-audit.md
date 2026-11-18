# v0.1.31 next-release audit — evidence and grill

Date: 2026-11-18. Scope: repo state, docs/code contract, GOAL trajectory,
PULSE semantics, privacy, reset/migration, live endpoints, tests/CI,
packaging. No implementation changes were made during this audit.

## Verdict

**Update 2026-11-18 (post-fix): code is PASS-WARN; release still BLOCK until
a gateway restart and live production replay are observed.** Both semantic blockers were fixed
in code and validated locally. The live profile DB still shows old-style
descriptors because `core/events.py` is agent-side and is loaded at gateway
process start; a gateway restart is required before live replay is valid. A
new release tag has NOT been created.

**Update 2026-11-18 (sanitizer second pass): the descriptor privacy hole is
fixed in code + regression-tested; release still BLOCK.** The first pass
kept unrestricted value tokenization behind a deny-token filter, which
rejects `--password` but not the value after it (`login --password hunter2`
leaked `verb:hunter2`). Value extraction is now removed outright (see
"Sanitizer privacy hardening" below). CI is green on HEAD `ad3a5fa`. Still
pending: gateway restart + live replay and the tag decision. No tag created.

**Update 2026-11-19 (live evidence): ALL code blockers are fixed AND
live-verified; the release is ready for the owner's tag decision.** The
"gateway restart required" assumption was WRONG: agent-side plugin code
(hooks, `core/events.py`) runs in the isolated `plugin_host_child`, not the
gateway process — a child restart (gateway auto-respawns) loads the new
code, and the warm-pool rotation had already refreshed part of the write
side. Live canary replay passed through the real hook -> persistence path;
the portrait-selection persistence bug was found, fixed, and live-verified
in the same pass. No tag created (owner: not yet).

**Update 2026-11-19 (trust gate, third pass): the passthrough prefix-only
check is replaced by a strict canonical-schema parse (`c2d6d31`).** Forged
descriptors with approved prefixes (`arg:password:hunter2`,
`kind:alice_smith`) were accepted by the old check; the engine's trajectory
intake also persisted `ev["operation"]` directly, bypassing the passthrough.
Both seams now apply `core/events.py::is_canonical_operation` (closed
action/kind/type sets, hygienic tool/arg-key fields, bounded shape, strict
charset — reject, never clean). 27 forged-prefix canaries + persistence
regression added; pytest 787 passed / 8 skipped; child pool restarted and
live-smoked (canonical descriptors, 0 legacy rows). The portrait INK
toggle was flipped by the owner in the live UI and persisted durably
(`portrait.ink: true` in the user-data doc) — live user-driven proof of the
persistence fix. No tag created (owner: not yet).

## Hard blockers

1. **D4 glitch gate is wrong in the shipped pane.**
   - Docs: `2026-11-12_user-upset-accumulator.md` — D4 locked at
     `0.30` lane entry, `0.65` glitch active. Backend `pulse/upset.py::_tier`
     matches (`UPSET_GLITCH_TIER = 0.65`, tier 2).
   - Pane: `desktop/plugin.js:2349` uses
     `tier >= 1 && upVal >= IYH_UPSET_BALL_TIER (0.30)`.
   - Gate: `tests/test_desktop_glitch_friction.mjs` fails exactly here.
   - User impact: sustained glitch fires at lane-entry level, not at the
     locked sustained-upset level.
   - Decision needed: restore the locked D4 gate, or explicitly change the
     contract and update docs + backend + test.

2. **Live global GOAL signal is not yet useful.**
   - Live `/pulse` showed `goal_signal.value = 0.0862`,
     `category = drifting`, `drift_streak = 9`, `version = 1`,
     `history_count = 0`.
   - `pulse_operations` rows are dominated by narrow labels like
     `terminal(sem:plugin.js,command:str,timeout:int)`; measured cosine to
     the objective is `0.015–0.17`. The guard correctly prevents a prompt
     blend rescue, but the result is that normal work is classified drift.
   - The board looks healthy only because the harness injects a
     prompt-derived `sem:` token; production labels are weaker.
   - Decision needed: derive pre-tool intention from a transient reasoning
     vector (allowed as derived evidence, no raw storage), or accept that
     the signal is a drift detector only.

## High-risk inconsistencies

3. **Docs still contradict the shipped value model.**
   - Skill `pulse-meter-model.md:9`: value is stateless, no time decay.
   - Code `pulse/matrix.py:61`: `PULSE_VALUE_HALF_LIFE_S = 3600`; channel
     evidence is time-decayed.
   - `2026-11-11_pulse-accumulate-model.md:75-79` still lists driver and
     quality-gate as open knobs, though code chose channel-gated decay plus
     automatic GOOD from successful calls.
   - API contract was updated in prose to capsule-only per-tool GOAL, but
     `2026-11-09_pulse_api_v2_contract.md:114` still says prototype banks.

4. **Hysteresis is misnamed.**
   - `HYSTERESIS = 0.01` in `pulse/matrix.py`, dashboard and pane is a 1%
     quantization bucket, not dual-threshold enter/exit hysteresis.
   - Release notes admit this, but docs/spec still call it hysteresis.

5. **Migration coverage gap.**
   - Schema is v12 and live DB migrated cleanly, but tests only pin current
     schema version; there is no explicit v11 -> v12 fixture that queries
     the new columns on a legacy-shaped DB.
   - Live DB backfill looks healthy: 19 operations, 0 null
     `historical_category`, 0 null `ended_ts`, 0 null `result_assessment`.

6. **CI flake observed, not reproduced locally.**
   - First CI run failed `test_flush_failed_persist_honest_accounting`
     (`failed == 0` despite logged batch persist error). Rerun green.
   - Six local isolated repeats passed. This is an unrelated race in the
     metrics flush accounting, but it will block releases intermittently.

## PASS evidence

- HEAD `e2efc5d`, pushed to `origin/main`; CI latest green.
- `pytest -q`: **688 passed, 8 skipped**.
- `hermes plugins doctor . --ci`: OK.
- Pane source and installed pane hash match:
  `027c0ca5c00d80b013b278b03edde3b1`.
- Live DB: schema 12, `integrity_check = ok`; no raw `user_message` or
  `error_message` persisted.
- Three-story board regenerated with meaningful categories:
  aligned, exploring, drifting, recovering, productive_detour.
- 100-turn soak: operation window stays capped at 32.
- Reset E2E test covers DB rows + engine memory + pending calls + no
  rehydration resurrection.
- Live/DB-fallback parity test asserts identical `goal_signal` fields.

## Release packaging status

- `plugin.yaml` is `0.1.31`; latest tag is still `v0.1.30`.
- No PR, tag or release created for 0.1.31.
- Release notes exist at
  `.hermes/plans/2026-11-18_goal-trajectory-release-notes.md`.
- No catalog file is tracked in this repo; install remains deep-link based.
- Untracked local files remain in the worktree and must not be swept into
  a release commit with `git add .`.

## Grill list for the owner

1. Should D4 be restored to locked tier 2 / 0.65, or is the 0.30 glitch a
   deliberate new contract? Docs and backend currently disagree with pane.
2. Is a global GOAL signal that reports drift on normal work acceptable?
   If not, what is the authoritative pre-tool intention source: sanitized
   operation label, reasoning-derived vector, or a bounded combination?
3. Are basename/verb tokens (`plugin.js`, `test_...mjs`) allowed in
   persistent operation metadata, or only arg key/type tags?
4. Which value driver is authoritative: time-decayed channel accumulation,
   the no-decay skill text, or the still-open accumulate-model knob?
5. Is 0.01 quantization acceptable to name `HYSTERESIS`, or should the spec
   use `SCORE_BUCKET` and reserve hysteresis for real enter/exit thresholds?
6. Should v0.1.31 be tagged only after blockers 1-2 are resolved?


## Post-fix resolution

- D4: `desktop/plugin.js` now uses tier 2 / `IYH_UPSET_GLITCH_TIER`;
  `test_desktop_glitch_friction.mjs` passes.
- GOAL descriptors: `core/events.py` emits deterministic bounded descriptors
  (action class + tool identity + approved `kind:` categories + arg key/type
  tags). NO value-derived free text: command/query/pattern values contribute
  no tokens; path basenames/parent dirs and URL hosts/paths/credentials are
  never tokenized. Ambiguous descriptors with no semantic token are
  unmeasured.
- Engine no longer blends prompt context into operation assessment; tool
  success is execution evidence only.
- Production replay fixture: `tests/fixtures/production_operation_descriptors.json`
  stores REAL tool args; the test GENERATES descriptors via
  `core_events.sanitize_operation(tool, args)` (no preconstructed strings)
  and asserts relevant > unrelated by margin and ambiguous is `None`.
- Synthetic board is explicitly labelled as synthetic in `board.html`.
- Docs reconciled: one-hour half-life, locked channel-gated driver, capsule-only
  TS comment, quantization wording.
- Added real v11 -> v12 migration fixture.
- Local gates: pytest 689 passed / 8 skipped; all `.mjs` tests pass; ESM OK;
  doctor OK; pane hash synced; DB integrity OK; raw text columns empty.
- CI for commit `6f47e80` is green: https://github.com/hi4c0ck/its-in-your-head/actions/runs/44369943457
- Do not tag until a gateway restart confirms the new descriptors in live events.

## Sanitizer privacy hardening (second pass, 2026-11-18)

Owner finding: the first-pass deny-token filter rejects `--password` but not
the value following it; `login --password hunter2` produced
`verb:hunter2` in a persistable descriptor. Query terms and path components
had the same broader problem (personal/confidential fragments).

Implementation (value extraction removed, not filtered):
- `core/events.py`: command/cmd/query/q/pattern values contribute NO tokens
  (removed from `_SEMANTIC_ARG_KEYS`; `_safe_tokens`/`_path_semantics`
  deleted). Path keys emit only the allowlisted extension class
  (`kind:code|data|config|doc|style|image|file`); basenames and parent dirs
  are never tokenized. URL keys emit only the categorical `kind:url` role;
  hosts, paths, queries and embedded credentials are never parsed.
  Descriptor shape: `action:<class> tool:<tool> [kind:<category>]
  [arg:<key>:<type> ...]`, capped at 160 chars. `_DENY_TOKENS` survives only
  as identifier hygiene for tool names/arg keys, never as a value filter.
- `pulse/activity_encoder.py`: the second, rawer extractor (80-char
  basename / first command token / host incl. userinfo / first query token)
  is deleted; raw args delegate to the SINGLE `core.events` policy. The
  collector `operation` passthrough is accepted only when every token
  belongs to the approved descriptor vocabulary — a free-text "operation"
  from a future producer is rejected, never persisted.
- `has_operation_semantics` keeps the legacy markers so pre-hardening rows
  stay scoreable; current descriptors emit only `kind:`.
- The earlier branch `fix/operation-descriptor-privacy` (`428feda`, green
  CI but never merged to main) is SUPERSEDED: it mapped an executable
  allowlist to command classes, kept basename/dir tokens, and did not
  cover the `pulse/activity_encoder.py` extractor or persistence.

Regression evidence (actual runs, not predictions):
- `tests/test_operation_sanitizer_privacy.py` (new, 24 tests): 14 canaries
  (password flag/attached/short-flag values, Bearer + code + query API keys,
  email in query/pattern/URL path, URL userinfo credentials, personal names
  in path/dir/query, git credential URL) run through the REAL
  `sanitize_operation()`; positive category contract; capsule delegation +
  hostile-passthrough rejection; metrics row boundary; and a production
  persistence roundtrip (`engine.on_events` -> `pulse_operations.operation`
  read back via raw SQL) asserting the persisted column equals the
  sanitized descriptor and carries no canary fragment.
- `tests/test_goal_trajectory.py`: the production fixture now stores real
  tool args and GENERATES descriptors via `sanitize_operation` at test
  time; relevant/unrelated margin > 0.05 holds at the action/category
  level; terminal/search-only cases are ambiguous (`None`).
- Local gates: `pytest -q` 713 passed / 8 skipped; `check_contracts.py`
  G5 CLEAN; temp budget 2.3 MB OK; ESM check OK; all 19
  `tests/test_desktop_*.mjs` PASS; `hermes plugins doctor . --ci` OK;
  installed pane hash matches `desktop/plugin.js`
  (`0af085acc924363c7e75c7d31419755f`).
- CI for commit `ad3a5fa` is green (6/6 matrix, win/linux x py 3.11-3.13):
  https://github.com/hi4c0ck/its-in-your-head/actions/runs/44397211291

Known consequence (accepted trade-off, owner-directed privacy-first):
- Tier-0 GOAL separation is now at the action/category level, not the
  filename level; terminal commands and text queries are unmeasured
  (ambiguous), never a drift claim. Absolute alignment scores for code
  work stay below `ALIGN_THRESHOLD` at Tier-0; the fixture asserts
  relative separation only.
- Legacy `pulse_operations` rows persisted before this pass still carry
  old-style descriptors until retention/reset; the gateway must restart
  to load the new `core/events.py` before live replay is valid.

Release checks after this pass:
| Check | Result |
|---|---|
| D4 threshold | Fixed (6f47e80) |
| GOAL prompt-blend removal | Present |
| Sanitizer privacy | Fixed in code + canary/persistence regression tests |
| CI | Green on HEAD `c2d6d31` (6/6 matrix): runs/44408742840 |
| Gateway restart + live replay | EVIDENCED 2026-11-19 via plugin_host_child restart + live canary (see below) |
| Manifest | 0.1.31 |
| Tag v0.1.31 | Does not exist — owner tag decision pending |

## Live restart + replay evidence (2026-11-19)

Hosting-model correction: the plugin uses ISOLATED hosting — hooks and
`core/events.py` execute in `plugin_host_child` (the child imports the
plugin and serves callbacks to the gateway by reference). Killing the
children refreshes the WRITE side; the gateway process itself never loads
the plugin code, so no gateway restart is needed (and none was performed —
the live agent session survived).

- Pre-restart state: live DB `pulse_operations` already showed new-style
  descriptors (pool rotation had spawned post-fix children); 0 legacy
  `verb:/term:/file:/host:` rows in the 32-row window.
- Killed children 28212/8756 (16144 had already rotated out); gateway
  respawned 26116/3856 at 00:48:58/59 — both after every fix commit.
- LIVE CANARY: a real `terminal` tool call carrying
  `--password hunter2 --api-key sk-live-CANARY123` flowed through the
  production pre/post_tool_call hooks; the persisted descriptor is
  `action:execute tool:terminal arg:command:str arg:timeout:int` —
  0 canary fragments, 0 legacy tokens in the whole window.
- CI for `0c8cdbe` (portrait persistence fix) green, 6/6 matrix:
  https://github.com/hi4c0ck/its-in-your-head/actions/runs/44405009594

## Portrait selections persistence bug (owner-reported, fixed 0c8cdbe)

Symptom: DRAWING template + INK selections did not survive restarts or
session changes.

Root causes (forensic):
- INK (`iyhInvertSource`) was a module-level `let` — NO persistence door.
- DRAWING persisted only to renderer localStorage (`ctx.storage`); the
  desktop app QUARANTINES that store on multi-instance lock contention.
  The user's `soft-flow-study` selection was found in
  `Local Storage.stuck2/leveldb` while the live store held the default
  `neutral-pencil` (two quarantine dirs: 2026-06-05, 2026-11-07).

Fix (two-door persistence):
- `portrait.template` / `portrait.ink` are settings keys; source of truth
  = profile-DB user-data store (`meta.plugin_settings` via userstore).
  POST /settings writes durably; GET /settings adds the raw override
  document as `user`. Child fallback literal updated in lockstep.
- Pane: INK persists via ctx.storage + backend mirror; template mirrors
  every set; register() hydrates from durable overrides (backend wins)
  and seeds the backend from local values when no override exists.
  Feature-detected degrade for hosts without ctx.rest.
- Tests: `tests/test_portrait_preferences_persistence.py` (roundtrip,
  restart simulation, false-as-override, unknown-key 400, isolated-child
  acceptance) + portrait-engine mjs section 12 (storage+mirror, full
  module-realm reload restore, hydration adopt/seed, legacy degrade).
- Gates: pytest 717 passed / 8 skipped (one load-flake in
  test_reasoning_capture_fires_once_per_turn on the first run, green on
  isolated + full re-run); contracts CLEAN; temp budget OK; ESM OK;
  19/19 mjs; doctor OK.

Live verification + recovery:
- Recovered the lost `soft-flow-study` selection into the durable doc via
  the plugin's own userstore.save (merge — existing settings preserved).
- Renderer remount (kill renderer child, app respawns): the pane hydrated
  from the LIVE child, adopted `soft-flow-study` into localStorage
  (verified in the active LevelDB) and seeded `portrait.ink: false` via
  POST /settings — accepted by the live isolated child (durable doc
  verified). INK's historical value was never persisted anywhere, so it
  starts at the honest default; the next toggle persists.
- Known host boundary: a future multi-instance localStorage quarantine
  can still wipe the renderer cache — the durable backend override now
  restores the selections on the next pane register (the structural fix).
- Pre-existing latent test-order issue (NOT introduced by this pass):
  `test_settings_fallback_mirror.py::test_child_settings_fallback_is_stable`
  deletes `dashboard*` sys.modules entries without restoring them; any
  later test holding an old module reference and calling importlib.reload
  fails. Natural discovery order is green (s5 runs before the mirror
  test); explicit command-line ordering mirror-before-s5 reproduces it.

## Canonical-schema trust gate (third pass, c2d6d31)

Owner finding: `_valid_operation_descriptor()` checked token PREFIXES only,
so forged strings with approved prefixes (`arg:password:hunter2`,
`kind:alice_smith`) passed the passthrough as trusted descriptors.

Grammar (producer + gate share the closed sets in `core/events.py`):
`action:<class> tool:<name> [kind:<category>] [arg:<key>:<type> ...]`
- action: closed class set AND must equal `_action_for_tool(tool)`;
- tool: identifier hygiene identical to the producer (`_safe_token`);
- kind: closed set (extension classes + file + url), at most one, before
  any arg tag;
- arg: hygienic key, closed type-name set (exotic types -> `other`),
  <= 6 unique keys;
- <= 160 chars, charset alnum + `_-.:` + single spaces; a string needing
  cleanup is REJECTED, never repaired.

Sealed seams:
1. `pulse/activity_encoder.py` passthrough — accepts only canonically-valid
   strings, unchanged; everything else re-sanitized from raw args. Legacy
   verb:/term:/file:/host:/dir:/path: formats are no longer valid INPUT.
2. `engine.py` tool_start trajectory intake — persists `ev["operation"]`
   DIRECTLY (bypasses the capsule passthrough); now applies the same gate,
   re-deriving the identity-only descriptor for non-canonical input.
3. Producer canonicality — whole-token 160-char budget (no mid-token
   truncation), single kind token, deduped arg fallback keys, closed type
   names; `is_canonical_operation(sanitize_operation(...))` is pinned for
   the whole canary battery + exotic/mixed/oversize shapes.

Evidence: 27 forged-prefix canaries x (canonical parse + passthrough
re-sanitization) + forged-operation persistence regression through
`engine.on_events` -> raw SQL on `pulse_operations`; pytest 787 passed /
8 skipped; contracts CLEAN; temp budget OK; ESM OK; 19/19 mjs. Child pool
restarted (9828/26580 at 01:09); live smoke: canonical descriptors, 0
legacy/forged rows. CI green on c2d6d31 (6/6):
https://github.com/hi4c0ck/its-in-your-head/actions/runs/44408742840
Read-side note: legacy persisted ROWS remain scoreable via
`has_operation_semantics` (scoring never re-emits their text); they age out
via retention/reset.
