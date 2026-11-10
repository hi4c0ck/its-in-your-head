# Stage 6 Acceptance Checklist — plan §15 (15 criteria)
Baseline: main @ 662b6aa. Backend via venv python, desktop via node tests. Marking: PASS-with-evidence (test id) or GAP.
After gap-fill + dead-code removal, re-verified in §FULL REGRESSION.

1. PULSE has four equal, independent lanes: LIVE, GOAL, GOOD, FRICTION.
PASS — backend: test_pulse_lanes.py::test_each_lane_ranks_independently (4 lanes, 4 distinct rank-1 tools); desktop: test_desktop_pulse.mjs §9 (PulseLanes repeat(4, minmax(0,1fr)), native lanes passthrough) + test_desktop_composition.mjs (lane_order verbatim, four lanes present).

2. Every cell carries its own tool name and dotted evaluation.
PASS — test_desktop_pulse.mjs §9 (slice(0,12) every visible cell owns truncated tool name; dotsFor 12 dots inline-grid repeat(4,4px)) + test_desktop_composition.mjs readability-floor (PulseLanes fontSize 9, dots 4px, no fontSize 7).

3. Each metric ranks tools independently.
PASS — test_pulse_lanes.py::test_each_lane_ranks_independently + ::test_all_eligible_tools_returned_with_one_based_ranks + ::test_lanes_from_rows_matches_build_lanes_ordering + ::test_hysteresis_sub_noise_never_reshuffles + ::test_deterministic_tie_break_equal_scores; desktop: test_desktop_pulse.mjs native passthrough (GOAL github rank1 independent of LIVE terminal).

4. GOAL does not wait for labeled user feedback.
PASS — test_pulse_readiness.py::test_goal_scores_with_references_zero_feedback (goal scores with references, zero feedback) + ::test_goal_provisional_score_keeps_level; plan contract v2 §3 GOAL evidence source.

5. GOOD, user FRICTION, and objective FRICTION have independent readiness counts.
PASS — test_pulse_readiness.py::test_good_sample_counts_provisional_to_ready + ::test_good_independent_of_user_friction + ::test_objective_failure_never_increments_user_friction + ::test_objective_ready_user_provisional_combined_follows_dominant; test_pulse_lanes.py::test_friction_user_objective_parts_independent + ::test_goal_and_good_reach_ready_independently.

6. Below-threshold scores remain visible as provisional, not falsely ready or silently neutral.
PASS — test_pulse_readiness.py::test_good_sample_counts_provisional_to_ready (1-2 samples provisional, 3 ready) + ::test_goal_provisional_score_keeps_level (level not forced neutral by provisional state); contract v2 §4 state/level separation.

7. Existing min_labeled configurations continue to work as backwards-compatible threshold defaults.
PASS — test_pulse_readiness.py::test_settings_keys_present_with_legacy_fallback_default + ::test_store_readiness_fallback_and_override + ::test_settings_load_carries_new_keys; engine wiring ::test_engine_passes_per_category_readiness_settings.

8. Tool errors cannot be mistaken for user dissatisfaction.
PASS — test_pulse_readiness.py::test_objective_failure_never_increments_user_friction + test_s4_pulse.py::test_tool_error_is_not_user_dissatisfaction + ::test_friction_only_from_labeled_or_objective.

9. PULSE evidence survives process restart within the same profile.
PASS — test_pulse_lifecycle.py::test_restart_restores_banks_capsules_goal + ::test_restart_via_separate_process + ::test_per_profile_isolation; bridge: test_s4_pulse_bridge.py::test_separate_processes_pulse_bridge.

10. The live view and DB fallback agree.
PASS — test_pulse_lifecycle.py::test_live_hydrated_view_equals_db_fallback + test_pulse_readiness.py::test_db_fallback_equals_live_view + test_s4_pulse_bridge.py::test_pulse_live_view_wins_over_fallback / ::test_pulse_fallback_serves_db_rows_when_engine_unavailable.

11. Bottom and sidebar use distinct compositions and adapt to actual allocated dimensions.
PASS (plus GAP-fill below for sidebar proportional flex pin) — test_desktop_composition.mjs: chooseComposition matrix (wide-shallow→bottom, tall-narrow→sidebar, narrow-shallow stays bottom, very-short readable, unknown→bottom) + visibleCapacity/capacityForComposition (resize recalculates, shared across lanes, reduce rows before shrinking text) + data-composition bottom/sidebar rendered + bottom registration verbatim (placement main dock bottom 96px) + right-zone pane (placement right 400px).

12. PORTRAIT and PULSE coexist in compact mode; activation does not make them mutually exclusive.
PASS — test_desktop_composition.mjs: nextFocusFor (pulse→portrait, portrait→pulse, invalid keeps current) + both modules always mounted (data-module pulse+portrait) + data-focus outline only tab effect + activateFocus header controls.

13. DRIFT and expanded inspection remain available.
GAP (pre-fill) → FILLED — existing: test_desktop_workspace.mjs driftSparkline + query counts (iyh-pulse/metrics/portrait/drift each 2 places), test_s5_backend.py::test_drift_empty_arrays; GAP was: no assertion that ExpandedPulse + ExpandedDrift remain wired. Filled in Stage 6 with assertions in test_desktop_workspace.mjs (jsx(ExpandedPulse), jsx(ExpandedDrift), function ExpandedPulse/ExpandedDrift presence).

14. The bubble transition is prepared around stable data identity and destination slots, then implemented without premature content replacement.
PASS — test_desktop_bubble.mjs (66 assertions): identities metric|tool / metric|slotIndex, first-load static, sub-threshold no-animate, one transition per meaningful change, rank change new slot old name until arrival, ordered bounded multi-change (cap/stagger), stale superseded, resize cancels, reduced-motion immediate commit, surface-relative bottom-edge spawn, no backend animation fields.

15. No raw prompts, reasoning, or tool outputs are persisted, and no memory files are written.
PASS — test_pulse_lifecycle.py::test_no_raw_prompt_or_tool_output_persisted + test_s4_pulse.py::test_capsule_schema_has_no_result_payload + test_v13_privacy_liveness.py::test_privacy_raw_prompt_never_persisted (user_message NULL); no memory writes: GLOBAL invariant 1 + test_no_hardcoded_tool_list_in_pulse_sources etc.

Additional Stage-6 gates (not §15 but GLOBAL): legacy rows/columns unchanged — PASS test_pulse_lanes.py::test_envelope_additive_rows_unchanged + ::test_no_animation_fields_in_lane_entries + ::test_db_down_envelope; desktop imports only SDK+react — PASS test_desktop_bubble.mjs imports invariant (3 import lines); contracts/temp/node-check/doctor/delta gates run in FULL REGRESSION.

Tuning (plan §7 readability floor): PASS — PulseLanes dots 4px, fontSize 9, equal widths repeat(4...), empty slots empty divs, no large numerals/rainbows (no hardcoded colors, theme vars only); animation constants BUBBLE_TRAVEL_MS 450 (350-550) / BUBBLE_ARRIVAL_MS 150 (120-180) within range. No constant changes needed.

Verdict: 15/15 PASS after gap-fill for criterion 13 (+ sidebar flex pin for criterion 11).
