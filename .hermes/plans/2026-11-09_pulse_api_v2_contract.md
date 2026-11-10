# PULSE API v2 Contract (NORMATIVE — exact spec for backend + desktop consumption)

Source: owner-specified contract, 2026-11-09. This file is the exact spec the PULSE-contract stage worker implements. Additive v2 — `lanes` is authoritative; `rows`/`columns` stay unchanged for legacy consumers. Do NOT modify legacy semantics in the new UI path. Do NOT put animation-specific fields in the backend schema.

## 1. Top-level response

`GET /pulse`

```
{
  "schema_version": 2,
  "available": true,
  "lane_order": ["live", "goal", "good", "friction"],
  "lanes": { "live": [], "goal": [], "good": [], "friction": [] },
  "rows": [],
  "columns": ["goal", "good", "friction", "unlabeled"]
}
```

| Field | Type | Contract |
|---|---|---|
| `schema_version` | `2` | Identifies the new response contract. |
| `available` | `boolean` | `true` when at least one PULSE tool record is available. Does not mean all metrics have evidence. |
| `lane_order` | `string[4]` | Canonical lane order. |
| `lanes` | `PulseLanes` | Four independent, independently ranked arrays. |
| `rows` | `LegacyPulseRow[]` | Existing row-oriented response, preserved for compatibility. |
| `columns` | `string[]` | Existing legacy column order, unchanged. |

`available: true` does NOT imply GOAL/GOOD/FRICTION is ready — a tool can exist while every semantic evaluation is still learning. DB-unavailable or no-tools ⇒ same envelope with `available: false`, empty lane arrays, empty `rows`. NEVER omit `lanes`.

## 2. TypeScript contract (normative)

```ts
type PulseState = "empty" | "learning" | "provisional" | "ready";
type PulseLevel = "neutral" | "weak" | "strong";
type PulseMomentumState = "surging" | "steady" | "cooling";
type PulseLaneOrder = ["live", "goal", "good", "friction"];

interface PulseSupport {
  /** Number of usable, independent evidence vectors. */
  count: number;
  /** Required count for this evidence category to become ready. */
  required: number;
  /** min(count / required, 1), always in [0, 1]. */
  ratio: number;
}

interface PulseResponseV2 {
  schema_version: 2;
  available: boolean;
  lane_order: PulseLaneOrder;
  lanes: {
    live: PulseLiveEntry[];
    goal: PulseGoalEntry[];
    good: PulseGoodEntry[];
    friction: PulseFrictionEntry[];
  };
  /** Legacy compatibility projection. */
  rows: LegacyPulseRow[];
  /** Legacy compatibility metadata. */
  columns: ["goal", "good", "friction", "unlabeled"];
}

interface PulseEntryBase {
  /** Stable tool registry ID. */
  tool_id: number;
  /** Canonical tool name; never a display-truncated label. */
  tool_name: string;
  /** One-based position within this lane, independently assigned. */
  rank: number;
}

interface PulseLiveEntry extends PulseEntryBase {
  activity: {
    calls_started: number;
    calls_completed: number;
    errors: number;
    first_seen: number | null;
    last_seen: number | null;
    /** Recent usable activity capsules used by the momentum calculation. */
    recent_capsules: number;
  };
  momentum: {
    /** Signed recent-share minus cumulative-share delta. */
    momentum: number;
    /** Alias retained from the existing momentum contract. */
    surge: number;
    /** Existing normalized momentum value, in [0, 1]. */
    value: number;
    state: PulseMomentumState;
  };
}

interface PulseGoalEntry extends PulseEntryBase {
  /** Strongest goal-to-reference similarity, or null without a score. */
  value: number | null;
  /** Existing semantic score threshold, default 0.8. */
  threshold: number;
  /** Score classification; independent of readiness. */
  level: PulseLevel;
  /** Evidence availability/readiness, independent of score. */
  state: PulseState;
  /** GOAL-specific reference-vector support. */
  support: PulseSupport;
  evidence: {
    source: "prototype_banks" | "recent_capsules" | "none";
    /** Valid vectors from the prototype banks. */
    prototype_count: number;
    /** Valid recent capsule vectors available for fallback. */
    capsule_count: number;
  };
}

interface PulseGoodEntry extends PulseEntryBase {
  value: number | null;
  threshold: number;
  level: PulseLevel;
  state: PulseState;
  /** Counts GOOD prototypes only. */
  support: PulseSupport;
  evidence_source: "good_prototypes" | "none";
}

interface PulseFrictionPart {
  /** Similarity to this source's own prototype bank. */
  value: number | null;
  level: PulseLevel;
  state: PulseState;
  /** Support from this source alone. */
  support: PulseSupport;
}

interface PulseFrictionEntry extends PulseEntryBase {
  /** Maximum of user.value and objective.value. Null when neither source has a usable score. */
  value: number | null;
  /** Existing semantic score threshold, default 0.4. */
  threshold: number;
  /** Classification of the combined displayed score. */
  level: PulseLevel;
  /** Readiness of the source contributing the displayed maximum. Never calculated by adding user and objective sample counts. */
  state: PulseState;
  /** Which independent source contributes the displayed maximum. */
  dominant_source: "user" | "objective" | null;
  user: PulseFrictionPart;
  objective: PulseFrictionPart;
}

interface LegacyPulseRow { [key: string]: unknown; }
```

## 3. Exact lane definitions

### LIVE
Operational activity, not learned semantic confidence. Example:
```json
{
  "tool_id": 12, "tool_name": "terminal", "rank": 1,
  "activity": { "calls_started": 34, "calls_completed": 32, "errors": 2,
                 "first_seen": 1794230100.0, "last_seen": 1794231600.0, "recent_capsules": 8 },
  "momentum": { "momentum": 0.18, "surge": 0.18, "value": 0.59, "state": "surging" }
}
```
LIVE ranking is independent of all semantic lanes (freshness, recent activity, completion, errors, momentum). `momentum.value` keeps its current meaning (normalized momentum, 0.5 = steady; NOT a probability). No learned-sample readiness threshold for LIVE.

### GOAL
```json
{
  "tool_id": 12, "tool_name": "terminal", "rank": 1,
  "value": 0.86, "threshold": 0.8, "level": "strong", "state": "provisional",
  "support": { "count": 2, "required": 3, "ratio": 0.6667 },
  "evidence": { "source": "prototype_banks", "prototype_count": 2, "capsule_count": 8 }
}
```
GOAL `value` = strongest cosine similarity (current goal vector vs selected reference vectors), floored to [0,1]. Reference selection: (1) valid vectors from GOOD, user-FRICTION and objective-FRICTION prototype banks; (2) no prototype references ⇒ recent capsule vectors; (3) no goal vector or no usable reference ⇒ `value: null`. Score threshold stays `pulse.thresholds.goal` (0.8). Readiness threshold is SEPARATE: `required` inherits legacy `pulse.min_labeled` (3) but counts REFERENCE VECTORS, not user feedback labels. Two usable references can yield a strong GOAL score while the cell stays provisional — never forced neutral for insufficient explicit feedback.

### GOOD
```json
{
  "tool_id": 17, "tool_name": "github", "rank": 1,
  "value": 0.73, "threshold": 0.6, "level": "strong", "state": "provisional",
  "support": { "count": 1, "required": 3, "ratio": 0.3333 },
  "evidence_source": "good_prototypes"
}
```
GOOD uses only the GOOD bank. `support.count` = usable GOOD vectors; `support.required` = `pulse.good_min_samples` (falls back to `pulse.min_labeled`). Score threshold stays 0.6. One sample ⇒ score + possibly `strong` level, but NOT `ready` state (3 required).

### FRICTION
One visible lane, two independently evaluated evidence sources:
```json
{
  "tool_id": 21, "tool_name": "browser", "rank": 1,
  "value": 0.91, "threshold": 0.4, "level": "strong", "state": "ready",
  "dominant_source": "objective",
  "user":      { "value": 0.72, "level": "strong", "state": "provisional", "support": { "count": 1, "required": 3, "ratio": 0.3333 } },
  "objective": { "value": 0.91, "level": "strong", "state": "ready",     "support": { "count": 3, "required": 3, "ratio": 1.0 } }
}
```
Combined `value` = max(user.value, objective.value) available. `dominant_source` determines the combined `state`; deterministic selection: (1) greater score wins; (2) tie ⇒ greater support ratio wins; (3) both equal ⇒ user wins. The two banks NEVER contribute to each other's readiness. Score threshold stays 0.4. Objective friction can be `ready` while user friction is `provisional`; operational failure must never be represented as negative user feedback.

## 4. Readiness semantics — MANDATORY

| State | Condition | `value` |
|---|---|---|
| `empty` | Required evaluation context unavailable (e.g. no current goal vector) | `null` |
| `learning` | Context exists, but no usable evidence vectors in the relevant bank | `null` |
| `provisional` | Valid score exists, own evidence count < own required count | numeric |
| `ready` | Valid score exists, own evidence count ≥ own required | numeric |

Key separation: `level` describes the SCORE; `state` describes the EVIDENCE supporting the score. All semantic lanes:

```
level: value None → "neutral"; value ≥ threshold → "strong"; else → "weak"
state: no eval context → "empty"; support_count == 0 → "learning";
       support_count < required → "provisional"; else → "ready"
```

NEVER make `level` neutral merely because `state` is not ready (that is the bug being corrected).

Settings: keep `pulse.min_labeled = 3`; add optional `pulse.goal_min_references`, `pulse.good_min_samples`, `pulse.user_friction_min_samples`, `pulse.objective_friction_min_samples` — each falls back to `pulse.min_labeled` when absent. Counts are INDEPENDENT; no combined GOOD+FRICTION+objective readiness calculation in the new lanes.

## 5. Ranking and array behavior

Each lane returns its own independently ordered array (illustration: LIVE: terminal,browser,github,filesystem / GOAL: github,filesystem,terminal,browser / GOOD: filesystem,... / FRICTION: browser,... — illustrative, not fixed). Rules: `rank` starts at 1 per lane; stable `tool_id` + canonical `tool_name`; return ALL eligible tools (backend does not guess desktop capacity — desktop picks the visible prefix from measured height); deterministic tie-breaking; no ranking jitter on negligible score changes (hysteresis); empty/learning entries remain valid entries (lack of evidence never removes a tool from the registry). The API owns semantic calculation + ranking; the desktop owns visible capacity + presentation.

## 6. Bubble transition compatibility

The bubble is a UI transition — no persisted animation/event log. Lane entries supply everything for snapshot comparison: `tool_id` (semantic tool), `tool_name` (display), `rank` (lane position), `value`/`level`/`state` (semantic changes), LIVE activity+momentum (operational changes). Desktop derives:

```ts
type PulseMetric = "live" | "goal" | "good" | "friction";
interface PulseTransition {
  sequence: number;
  metric: PulseMetric;
  tool_id: number;
  tool_name: string;
  previous_rank: number | null;
  next_rank: number;
  previous_value: number | null;
  next_value: number | null;
  previous_level: string | null;
  next_level: string | null;
  previous_state: string | null;
  next_state: string | null;
  reason: "value-change" | "level-change" | "state-change" | "rank-change";
}
```

Destination slot is resolved by the DESKTOP from `next_rank` + current responsive capacity — do NOT add `destination_slot` to backend data (same tool moves to different visible positions after resize). The UI retains the committed cell until bubble arrival, then updates name + evaluation together; initial hydration does not animate. NO backend bubble events — snapshot comparison belongs to the desktop (it knows surface geometry, visible capacity, animation state, reduced-motion).

## 7. Legacy compatibility contract

Existing top-level legacy fields (`rows[]` with `tool_name`, `tool_id`, `state`, `labeled`, `live{...}`, `goal{value,state,level}`, `good{...}`, `friction{value,state,level,user,objective}`, `momentum{...}`; `columns: ["goal","good","friction","unlabeled"]`) — structural illustration, not promised values. Compatibility rules: (1) keep `rows`+`columns`; (2) keep existing field names + types; (3) keep legacy row readiness semantics in this projection during migration; (4) the new desktop UI must NOT read `row.state`/`row.labeled` for semantic cell readiness; (5) `lanes` is the authoritative v2 contract; (6) remove the legacy projection only in a separately versioned breaking change after consumers migrate. The `rows` shape is built by `pulse/matrix.py::build_matrix()` + `engine.py::pulse_view()`, mirrored in the DB fallback of `dashboard/plugin_api.py` — the three places that must stay consistent.

## 8. Implementation ownership

| Component | Responsibility |
|---|---|
| `pulse/matrix.py` | Calculate independent metric values, evidence, states, levels |
| `pulse/prototypes.py` | Expose independent bank counts; remove shared readiness as the semantic source of truth |
| `pulse/momentum.py` | Preserve the existing momentum calculation |
| `engine.py` | Build the new lanes and retain the legacy projection |
| `dashboard/plugin_api.py` | Mirror the contract in DB fallback; preserve empty response + fallback parity |
| `core/settings.py` | Add readiness settings with legacy fallback |
| `desktop/plugin.js` | Consume `lanes`, render independent ranking, calculate visible capacity, coordinate bubble transitions |

Live engine and isolated dashboard fallback must return the SAME v2 schema incl. state definitions and null behavior — no subtly different readiness logic.

## 9. Required contract tests (mandatory before the UI consumes v2)

| Test | Expected |
|---|---|
| No tools | `available: false`; all arrays empty |
| Tool exists, no goal vector | GOAL `empty`, `value: null` |
| Goal vector, no references | GOAL `learning`, `value: null` |
| One GOAL reference, required 3 | GOAL `provisional`, numeric value retained |
| Three GOAL references | GOAL `ready` |
| One GOOD sample | GOOD `provisional`, numeric retained |
| Three GOOD samples | GOOD `ready` |
| One user-friction sample | user FRICTION `provisional` |
| Three objective-friction samples | objective FRICTION `ready` |
| Three objective failures, zero user samples | user FRICTION stays `learning` |
| Different GOOD vs GOAL leaders | each lane has its own rank 1 |
| Equal scores | deterministic tie ordering |
| DB fallback vs live engine | same v2 structure, state semantics, values |
| Old client | `rows`/`columns` remain available |
| Desktop resize | no API refetch needed to change visible capacity |

Bottom line: `schema_version: 2` + `lanes` = the new official interface; `rows` = compatibility projection; no animation fields in the backend; no legacy shared-readiness leakage into the new UI. This is the PROPOSED v2 contract to implement (repo has NOT implemented it yet).
