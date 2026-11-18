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
  (action + tool + allowlisted file/verb/term/host/path/kind tokens + arg
  types). Ambiguous descriptors with no semantic token are unmeasured.
- Engine no longer blends prompt context into operation assessment; tool
  success is execution evidence only.
- Production replay fixture: `tests/fixtures/production_operation_descriptors.json`;
  test asserts relevant > unrelated by margin and ambiguous is `None`.
- Synthetic board is explicitly labelled as synthetic in `board.html`.
- Docs reconciled: one-hour half-life, locked channel-gated driver, capsule-only
  TS comment, quantization wording.
- Added real v11 -> v12 migration fixture.
- Local gates: pytest 689 passed / 8 skipped; all `.mjs` tests pass; ESM OK;
  doctor OK; pane hash synced; DB integrity OK; raw text columns empty.
- CI for commit `6f47e80` is green: https://github.com/hi4c0ck/its-in-your-head/actions/runs/44369943457
- Do not tag until a gateway restart confirms the new descriptors in live events.
