# Directional GOOD / FRICTION Reinforcement — implementation plan

Goal: independent positive/negative PULSE feedback channels for the adaptation
instruction (`adaptation/`), without breaking `perception.patterns.reinforce()`
or the 4-lane PULSE UI. The PULSE calc (`pulse/matrix.py`) is untouched.

## What changes
- `adaptation/rules.py` — the single `domain-friction-prefer-good` rule is
  replaced by THREE independently-evaluated channels + directional templates +
  conflict suppression + vector-space compatibility.
- `core/collector.py` — the seam passes the prompt vector encoded by the ACTIVE
  encoder (+ `vector_space`), so rules never Tier-0-encode into a foreign space.
- `tests/test_s7_adaptation.py` — updated to the new contract + new directional /
  conflict / compat / GOAL-independence / per-category-readiness cases.

What does NOT change: `perception/patterns.py` (reinforce), `pulse/matrix.py`,
`pulse/prototypes.py` banks/learning, the PULSE UI lanes, the injection contract
shape (`{"context","meta"}`), `adaptation.enabled` master gate + dedupe.

## The 3 channels (per tool T, using T's own bank only)
| channel | similarity | evidence min | threshold | direction | template |
|---|---|---|---|---|---|
| GOOD | good bank | good_min_samples | pulse.thresholds.good (0.6) | positive | `Prefer {tool} for this domain.` |
| USER FRICTION | friction bank | user_friction_min_samples | pulse.thresholds.friction (0.4) | negative | `Do not repeat the previous {tool} approach for this domain.` |
| OBJECTIVE FRICTION | objective bank | objective_friction_min_samples | pulse.thresholds.friction (0.4) | negative | `Verify the result of {tool} before proceeding in this domain.` |

A channel qualifies iff: similarity is not None AND >= threshold AND count >=
its own category min AND the vector space is compatible (dim + provenance).

## Selection + conflict (§6)
1. Compute qualifying candidates per channel.
2. `positive_set` = tools whose GOOD qualifies; `negative_set` = tools whose
   USER or OBJECTIVE FRICTION qualifies.
3. If BOTH sets are non-empty -> contradictory, cannot establish a clear
   direction -> suppress (return []), even though the banks keep accumulating.
4. Else: positive-only -> pick best GOOD (sim desc, tool asc).
   negative-only -> pick best friction (sim desc, user before objective on ties,
   tool asc). One instruction max. Source identity in meta (`rule_id`).

## GOAL independence (§4)
- `GOAL.value` / GOAL.ready is NOT a gate. The channel similarity IS the
  relevance check. No combined `pulse.min_labeled` gate. The category min is the
  only reliability gate, and it must not block prototype recording (that stays
  in PrototypeStore, unchanged).

## Vector-space compatibility (§3)
- rules use `prompt_features["vector"]` (active-encoder output from the seam).
- STORE shape: check `len(vector)==len(bank vec)` + provenance
  (`store.bank_meta(T)[label]` vs `prompt_features["vector_space"]`); a known
  conflict -> channel does not fire.
- No unconditional Tier-0 fallback when prototypes carry a known foreign
  provenance. No LLM / no new model — the seam encodes with the existing active
  backend (`engine._get_backend()`).

## rule ids (metadata distinguishes the 3)
`pulse-good`, `pulse-user-friction`, `pulse-objective-friction`.

## Stages
1. rules.py: `_good_similarity` / `_user_friction_similarity` /
   `_objective_friction_similarity` + counts + category mins (store + dict +
   matrix-row shapes); the 3 templates; `evaluate_rules` -> candidate sets +
   conflict + deterministic pick; vector-space gate; `_encode_text` provenance
   guard. `list_rule_definitions` -> 3 defs.
2. collector.py: encode message with active backend, pass `vector` +
   `vector_space` in prompt_features. Fault-isolated (never breaks the hook).
3. tests/test_s7_adaptation.py: rewrite firing cases to the 3 channels; add
   GOOD-only / user-friction-only / objective-friction-only; conflict -> None;
   two-tools-one; GOAL-not-a-gate; per-category min (no cross-borrow);
   encoder compat (dim mismatch / foreign provenance -> None); no prompt echo;
   dedupe.
4. Run: test_s7_adaptation, test_reasoning_feedback, test_s4_pulse_bridge,
   test_pulse_lanes, test_engine_wiring, test_s10_reinforce_dirty_set; then
   full Python suite + desktop PULSE contract (tests/*.mjs) + G5.

## Acceptance mapping (§11)
1-3: 3 channels fire independently. 4: GOAL not a prerequisite. 5: banks stay
independent + persistent (untouched PrototypeStore). 6: patterns.reinforce
untouched. 7: at most one bounded instruction. 8-9: UI 4 lanes + schema_v2
unchanged. 10: no new data collection / model / migration.
