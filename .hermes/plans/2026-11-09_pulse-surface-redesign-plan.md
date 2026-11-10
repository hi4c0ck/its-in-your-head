The key readiness correction is that thresholds must govern confidence, not whether a metric is allowed to show a result. GOAL must not wait for user feedback labels; GOOD, user FRICTION, and objective FRICTION need independent support counts; and scores below their readiness thresholds should remain visible as provisional rather than being suppressed. The bubble effect will be designed into the data and UI transition model from the start, but its animation comes after the new layout and ranking behavior are stable.

# It’s in Your Head — Unified PULSE & Surface Redesign Implementation Plan

**Status:** Implementation specification\
**Scope:** PULSE data correctness, learning/readiness, matrix redesign, responsive bottom/sidebar compositions, and preparation for the bubble-arrival animation\
**Compatibility target:** Existing Hermes plugin lifecycle, profile isolation, `/pulse` API consumers, PULSE/DRIFT expanded workspace, and portrait templates

---

## 1. Objective

Redesign PULSE as four independent, compact evaluation lanes, and make the desktop strip adapt to two genuinely different surfaces:

- **Bottom bar:** a shallow horizontal composition, with PORTRAIT on the left and PULSE occupying the remaining width.
- **Sidebar:** a vertical composition, with PORTRAIT in the upper half and PULSE in the lower half.

PULSE should no longer look like a conventional table with one tool-name rail and four metric columns. It should become four equally sized, independently ranked lanes. Every cell owns its tool name and its own compact dotted evaluation display.

The new layout must also correct an important backend problem: the current shared readiness state can suppress otherwise valid GOAL, GOOD, and FRICTION evaluations. The redesign must make evidence support and readiness metric-specific without turning low-sample results into falsely confident evaluations.

Finally, the UI and data model should be prepared for a small bubble travelling from the bottom of the PULSE surface to a cell whenever a meaningful evaluation changes. The bubble should become the visible transition between old and new information—not an animation pasted onto a table that immediately changes underneath it.

## 2. Non-negotiable constraints

1. **No memory writes or prompt injection.** This remains an observability and reflection instrument, not a memory system. Any memory-like data is read-only input.
2. **No raw content persistence.** Do not persist raw user prompts, raw reasoning, or tool output text. PULSE stores derived vectors, numeric evaluations, counts, timestamps, and other bounded metadata only.
3. **Profile isolation.** Hydrate and read data only from the active profile’s existing database. Never combine profiles or silently migrate one profile’s learned state into another.
4. **Do not manufacture confidence.** Tool failures are not user dissatisfaction. A weak sample count must not be disguised as a ready evaluation.
5. **Preserve existing behavior outside the redesign.** Keep PULSE and DRIFT available in the expanded workspace, preserve portrait template behavior, and maintain existing API consumers.
6. **Do not introduce speculative host integrations.** Verify the Hermes Desktop placement contract before claiming sidebar support. Do not invent a custom pane manager or assume that a bottom-docked plugin can choose its own host placement.
7. **Keep the desktop entry point self-contained.** The currently supported runtime imports are `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime`. The generated `desktop/plugin.js` must remain compatible with that constraint.

---

## 3. Current implementation problems to resolve

The redesign should explicitly address these existing conditions rather than treating the work as a cosmetic replacement.

### 3.1 PULSE layout and ranking

The current `PulseMatrix` uses a separate tool-name rail and a shared ordering for all four metrics. Its rows are selected globally by momentum, activity, semantic similarity, and original order.

That creates two problems:

- The name rail consumes a substantial part of the available width.
- A tool ranked highly for GOOD is forced into the same row position for GOAL, FRICTION, and LIVE, even when its relevance differs by metric.

The current compact UI also has a fixed four-row maximum. It does not intelligently recalculate its visible capacity from the actual available height.

### 3.2 Shared readiness suppresses independent evaluations

`pulse.matrix` currently obtains one shared readiness state from `prototypes.state(tool)`. The state depends on the combined GOOD and user FRICTION labeled sample count. `_level` then returns a neutral result unless the row is ready.

This inadvertently makes GOAL dependent on explicit user feedback, even though GOAL can be evaluated using the current goal vector and available reference capsules.

It also prevents weak-but-real evidence from being shown as provisional.

### 3.3 In-memory learning state is not restored

`pulse.persistence` writes prototype banks, capsules, and the current goal vector, but the current `Engine` initialization creates a new empty `PrototypeStore` and does not hydrate it from the database.

The dashboard can fall back to persisted data, but `/pulse` prefers a live engine view when that view contains rows. Since tool rows are restored independently, a freshly initialized engine can return live rows with empty prototype banks and thereby mask the more complete database fallback.

This is a real persistence-restoration defect, not just an appearance issue.

### 3.4 Feedback detection has two sources of truth

`pulse.feedback.FEEDBACK_RULES` is the active short-phrase detector, while `PrototypeStore.classify_feedback` maintains another phrase set.

The active detector can prevent phrases supported by the broader classifier from ever being applied. The two tables must be unified.

### 3.5 Existing animation is not the requested transition

The current UI can flash a cell when its discrete band changes, using a box-shadow effect. It does not animate the arrival of a new evaluation, coordinate changes with ranking, or delay the visible replacement of a tool name until the transition completes.

The new design needs an explicit transition lifecycle.

---

## 4. Target PULSE model: four independent lanes

The four lanes are:

| Lane         | Meaning                                       | Ranking basis                                                            |
| ------------ | --------------------------------------------- | ------------------------------------------------------------------------ |
| **LIVE**     | Recent tool activity and operational movement | Freshness, recent activity, completion and meaningful momentum           |
| **GOAL**     | Alignment with the current user goal          | Goal-vector similarity and available reference support                   |
| **GOOD**     | Evidence from positive user feedback          | Similarity to GOOD prototypes and their support                          |
| **FRICTION** | Evidence of friction                          | User-reported friction and objective tool failures, kept distinguishable |

### 4.1 Independent ranking

Each lane must select and rank its own tools.

A tool may be first in GOOD and lower in GOAL. Another tool may be first in GOAL but absent from the visible LIVE leaders. This is expected behavior, not inconsistency.

Use a stable, lane-specific ordering. Avoid reshuffling cells because of negligible floating-point changes. Introduce a minimum meaningful change or ranking hysteresis so nearly identical scores do not produce constant movement.

LIVE should remain grounded in operational activity. GOAL, GOOD, and FRICTION should use their respective evidence sources rather than borrowing a global row ranking.

### 4.2 Equal lane geometry

Use four equal columns:

```
grid-template-columns: repeat(4, minmax(0, 1fr));
```

Do not reserve a separate tool-name column.

Each visible cell contains:

1. A tool name, truncated to fit.
2. A small dotted evaluation pattern.
3. Optional compact support/confidence treatment.
4. A tooltip or equivalent detail surface with the full tool name, value, readiness, sample count, and evidence source.

Cells should have consistent dimensions within a surface. Empty slots remain empty; they must not cause neighboring lanes to expand or change width.

### 4.3 Dot representation

Use a small, disciplined dot matrix—for example, 3×4 or 4×4—rather than cards, large badges, or filled progress bars.

The dot pattern represents evaluation magnitude or level. Support and confidence are secondary signals and must not be confused with the score itself.

The visual language should remain quiet and legible at both compact sizes. Avoid turning each lane into a miniature dashboard card.

### 4.4 FRICTION must preserve source distinction

FRICTION contains two distinct evidence streams:

- **User friction:** feedback indicating dissatisfaction or difficulty.
- **Objective friction:** tool failure, error, timeout, or other operational failure.

Keep separate scores, counts, and source metadata. A combined display may emphasize the stronger friction signal, but it must preserve which source produced that signal.

An objective failure must never be stored as user dissatisfaction or used as a substitute for a negative user-feedback sample.

---

## 5. Readiness and threshold redesign

This is a core backend task, not a UI-only workaround.

The goal is to preserve the distinction between **having a score** and **having enough evidence to trust that score**.

### 5.1 Replace the shared row readiness with per-cell readiness

Each metric cell should expose its own state:

| State         | Meaning                                                                 | Presentation                                               |
| ------------- | ----------------------------------------------------------------------- | ---------------------------------------------------------- |
| `empty`       | No usable evaluation or comparison is available                         | Empty/minimal indicator; no fabricated score               |
| `learning`    | The tool exists, but this metric has no usable evidence yet             | Quiet learning treatment                                   |
| `provisional` | A score can be calculated, but support is below its readiness threshold | Show the tentative score with subdued confidence treatment |
| `ready`       | The metric-specific support threshold is met                            | Normal evaluation treatment                                |

The state belongs to a **tool × metric**, not to the tool row as a whole.

LIVE does not need a learned-feedback readiness threshold. Its activity is operational data and should be displayed when available.

### 5.2 Keep threshold semantics separate from score semantics

The current `_level` behavior effectively hides evaluations until a shared readiness condition is met. Change this.

A valid candidate score should be calculated and returned even when the evidence is provisional. The readiness state should control how confidently the UI presents it, not erase the score.

For example:

- One GOOD sample can produce a tentative similarity score.
- It must not be presented as a fully established GOOD profile.
- Three independent GOOD samples may satisfy the default readiness threshold.
- A goal match can be shown using available reference capsules even if the user has not yet supplied any explicit GOOD or FRICTION feedback.

Keep the existing score thresholds—GOAL `0.8`, GOOD `0.6`, and FRICTION `0.4`—unchanged in the initial implementation. Do not mix changes to score interpretation with changes to readiness.

### 5.3 Metric-specific evidence and thresholds

Use independent support counts:

| Metric               | Evidence count                       | Readiness rule                        |
| -------------------- | ------------------------------------ | ------------------------------------- |
| LIVE                 | Recent operational events            | No learned-sample threshold           |
| GOAL                 | Usable comparison/reference vectors  | Own reference-support threshold       |
| GOOD                 | GOOD prototype samples               | GOOD-specific sample threshold        |
| FRICTION — user      | User-friction prototype samples      | User-friction-specific threshold      |
| FRICTION — objective | Objective-friction prototype samples | Objective-friction-specific threshold |

For FRICTION, do not add user and objective counts together to reach readiness. They represent different phenomena.

When a combined FRICTION cell is displayed, its dominant evidence source determines the displayed evaluation's readiness. Keep both source counts available in the cell detail.

### 5.4 Backwards-compatible threshold migration

Retain the existing `pulse.min_labeled` setting so old configurations continue to work.

Introduce metric-specific settings, for example:

```
pulse.goal_min_references
pulse.good_min_samples
pulse.user_friction_min_samples
pulse.objective_friction_min_samples
```

When a new setting is absent, use the legacy `min_labeled` value as its numeric default. The legacy setting must no longer create one shared evidence pool or one shared row state.

With the current default of `min_labeled = 3`, the initial migration can therefore use three as the default readiness count for each independent learned-evidence category. The count is evaluated separately for each category; it is never the sum of GOOD, user FRICTION, and objective FRICTION.

For GOAL, the fallback value is a reference-support count, not a labeled-feedback count. A GOAL score can still be shown provisionally from the first usable reference vector.

This provides a practical workaround without weakening the old threshold globally or pretending that one type of evidence is another.

### 5.5 Do not solve the problem by lowering `min_labeled`

Avoid these tempting but incorrect workarounds:

- Do not globally change `min_labeled` from 3 to 1 just to make the interface light up.
- Do not count objective tool failures as negative user feedback.
- Do not combine GOOD and FRICTION support to make unrelated cells ready.
- Do not make GOAL wait for user feedback.
- Do not display a strong/ready result when there is only one weak reference sample.

Instead, make the provisional state visible and keep the evidence count explicit.

### 5.6 Support and confidence in the API

Return, per cell:

- `value` or the existing score field.
- `level` or the existing band.
- `state`: `empty`, `learning`, `provisional`, or `ready`.
- `support_count`.
- The applicable `readiness_threshold`.
- A confidence/support indicator suitable for UI presentation.
- Evidence source where relevant.
- For FRICTION, separate user and objective scores and counts.

Do not invent a statistical confidence interval if the underlying scoring model does not provide one. A normalized support indicator may be used, but it must be identified as support—not statistical certainty.

---

## 6. Persistence restoration and engine lifecycle

Implement this before relying on the new readiness calculations.

### 6.1 Add one-time lazy hydration

On the first operation requiring PULSE learning state, restore from the active profile's existing database:

- Prototype banks, including GOOD, user FRICTION, and objective FRICTION.
- Recent capsules needed for comparison and feedback association.
- The most recent goal vector and its relevant metadata.

Use the existing profile-scoped persistence source. Do not add a second state store or a cross-profile cache.

### 6.2 Hydrate before both display and feedback association

Hydration must happen before:

1. `pulse_view` builds the matrix.
2. A feedback event attempts to associate a user reaction with a recent capsule.

Loading only when `pulse_view` is called is insufficient. Feedback may arrive before the first dashboard read.

Add a public engine ensure/restore method or an equivalent controlled entry point. The collector's feedback path must invoke it before applying feedback.

### 6.3 Concurrency and hot-path behavior

The load should be:

- Lazy.
- Once per engine process and active profile.
- Guarded against concurrent duplicate loads.
- Performed through a bounded, predictable database access path.
- Absent from the synchronous tool-start hot path.

Do not add database I/O to every tool-start event or every PULSE poll.

### 6.4 Reset semantics

`Engine.reset` is an intentional wipe/reset flow. It must not be followed by an automatic hydration that silently restores the state just deleted.

Distinguish process startup restoration from an explicit reset. Once reset has completed, the engine's in-memory restoration state must reflect that intentional empty state.

### 6.5 Dashboard fallback parity

The database fallback in `dashboard/plugin_api.py` must follow the same cell-specific readiness and support rules as the live engine path.

The fallback must not reintroduce the shared labeled-count readiness bug. A live view with restored state and a DB fallback view should produce equivalent evaluations from the same persisted evidence.

### 6.6 Optional optimization

The current `_on_user_turn` path can encode goal text more than once. Once hydration and behavior are correct, consider reusing the already-computed goal vector when safe.

Treat that as a contained optimization, not a prerequisite for the persistence correction.

---

## 7. Feedback detector unification

Make one phrase table the source of truth for active feedback detection and prototype classification.

Preserve the conservative behavior of the active detector:

- Short feedback only, retaining the current 12-word maximum unless tests justify a deliberate change.
- Negative matching takes precedence over positive matching.
- Associate feedback only with the most recent eligible capsule in the current bounded recent window.
- Unmatched language remains unlabeled.
- Do not classify arbitrary conversational text as feedback.

The broader prototype phrase classifier must not have a separate phrase inventory that the active detector can silently block.

Add coverage tests for phrases that currently diverge, including simple standalone positive phrases such as `great`, `perfect`, and `excellent`, alongside the existing negative, ambiguous, and no-match cases.

Most importantly, retain the rule that tool errors and timeouts create objective friction—not user dissatisfaction.

---

## 8. Responsive surface architecture

The bottom bar and sidebar must be treated as two different compositions, not as one layout with a narrower breakpoint.

### 8.1 Detect orientation using the actual surface

The current strip infers its compact/sidebar behavior mainly from width. That is not enough: a narrow but shallow bottom bar can incorrectly look like a sidebar.

Use a single `ResizeObserver` to measure both width and height of the actual plugin surface.

Prefer an explicit host orientation/layout signal if Hermes exposes one. Otherwise, derive the composition from the measured container shape, using width, height, and aspect ratio.

A wide shallow surface remains a bottom composition even when its width falls below the old narrow breakpoint. A genuinely tall, narrow surface uses the sidebar composition.

### 8.2 Bottom-bar composition

Target arrangement:

```
┌──────────────┬──────────────────────────────────────────────┐
│   PORTRAIT   │   LIVE   │   GOAL   │   GOOD   │   FRICTION   │
└──────────────┴──────────────────────────────────────────────┘
```

- PORTRAIT sits at the lower-left, as a compact visual module.
- PULSE begins immediately to its right and receives the remaining width.
- The four PULSE lanes have equal width.
- The header remains minimal; the operational pet remains a small separate indicator.
- The number of visible PULSE cells is recalculated from the actual available height.
- Bottom mode should normally show one to three cells per lane, selecting capacity based on the readability of tool names and dots.

Do not preserve four rows at the expense of legibility.

### 8.3 Sidebar composition

Target arrangement:

```
┌──────────────────────────┐
│                          │
│         PORTRAIT         │
│                          │
├──────────────────────────┤
│                          │
│   LIVE   GOAL   GOOD     │
│                          │
│       FRICTION           │
│                          │
└──────────────────────────┘
```

- PORTRAIT occupies the upper section.
- PULSE occupies the lower section.
- Both sections start with a balanced allocation, approximately 50/50.
- Both are flexible and shrink proportionally when the host reduces the available height.
- The four PULSE lanes remain horizontal columns, even in the narrow sidebar.
- The compact portrait rendering adapts without forcing the rest of the pane to scroll.
- Do not impose large minimum heights that push other plugins out of the shared sidebar.

Use flexible sizing (`flex`, `min-height: 0`, and equivalent constraints) so the host can allocate less space without one module refusing to shrink.

### 8.4 Host integration gate

The current plugin registration uses a bottom dock placement and a 96px height. Before implementing or documenting sidebar placement as supported, inspect the installed Hermes Desktop SDK/host contract:

- Can the host actually place this plugin in a sidebar?
- Does the host provide a placement or orientation signal?
- How are available dimensions allocated when multiple plugins share the surface?
- Is the declared `height` fixed, preferred, or only a bottom-dock hint?

If the host does not support a sidebar placement for this plugin, implement the responsive composition behind a verified host integration path rather than pretending that width detection alone creates sidebar support.

Keep the current registered placement behavior intact until that integration is confirmed.

---

## 9. Activation controls, not exclusive tabs

PULSE and PORTRAIT should no longer be mutually exclusive tabs that replace each other's content.

Both modules are visible in the compact composition by default. Their controls become activation/focus controls:

- Activating PORTRAIT focuses or expands the portrait module.
- Activating PULSE focuses or expands PULSE.
- Activating one must not silently hide the other.
- The compact surface maintains the bottom/sidebar composition rather than swapping the entire content area.

DRIFT remains in the expanded workspace for detailed inspection. The expanded workspace can retain its detailed PULSE, PORTRAIT, and DRIFT navigation where appropriate; the change here concerns the compact strip's composition and controls.

Do not remove the existing expanded workspace or its query/cache behavior as part of this redesign.

---

## 10. Dynamic capacity and resize behavior

Calculate visible capacity from the actual measured space rather than a fixed row count.

A useful starting point is:

```
capacity = floor((availableHeight + gap) / minimumCellPitch)
```

Then clamp the result to the mode's sensible limits:

- Bottom: normally 1–3 visible cells per lane.
- Sidebar: use the available height up to a readable maximum.

Apply these rules:

1. Keep all four lanes aligned to the same visible capacity.
2. Preserve equal lane widths and equal cell dimensions.
3. Reduce row capacity before shrinking text or dots below their readability floor.
4. Recalculate after every meaningful resize.
5. Use flexible gaps and margins so cells occupy the available space without stretching into oversized cards.
6. Keep empty slots stable rather than allowing the remaining cells to redistribute unpredictably.

Use one observer for the surface dimensions and derive the layout from that measurement. Avoid separate competing observers for individual breakpoint decisions.

---

## 11. Bubble-arrival transition: prepare the architecture now

The bubble is an important part of the intended visual language. It should be designed into the implementation now, even though the final animation should follow the stable data and layout work.

### 11.1 Intended interaction

When a meaningful evaluation changes:

1. Detect the changed metric/tool evaluation.
2. Identify its destination lane and destination slot.
3. Create a small bubble at the bottom edge of the PULSE surface.
4. Animate it upward toward the destination cell.
5. Keep the current cell contents visible during travel.
6. At arrival, update the cell's evaluation and, if ranking changed, its tool name.
7. Trigger a short arrival flash or sparkle.
8. Return the dots and cell to their normal quiet appearance.

The visual metaphor is that a new evaluation has travelled into the matrix and has just been absorbed by its destination cell.

It should feel precise and restrained—not like a game reward, notification explosion, or continuous particle effect.

### 11.2 Establish stable identities

Use two different identities:

- **Semantic transition identity:** `metric|tool`, which identifies the evaluation whose data changed.
- **Visual slot identity:** `metric|slotIndex`, which identifies the stable position in the lane.

This distinction is important. A tool can change its rank and therefore move to a different slot. The UI must be able to animate the update to the new destination without immediately replacing the existing visible content.

### 11.3 Use staged state, not immediate replacement

Maintain a committed lane snapshot and a pending next snapshot.

When a transition starts:

- Keep the currently committed cell content visible.
- Resolve the destination slot using the new lane ranking.
- Measure the destination cell relative to the PULSE surface.
- Animate the bubble overlay to that position.
- Commit the pending cell content only when the bubble arrives.
- Flash the destination briefly after the commit.

This is what allows the tool name to update at the moment of arrival instead of changing before the bubble reaches the cell.

If the ranking changes again while a transition is active, supersede or retarget the pending transition using the newest evaluation rather than allowing stale animations to commit out of order.

### 11.4 Transition event contract

Prepare an explicit transition object, for example:

```
type PulseTransition = {
  sequence: number;
  metric: 'live' | 'goal' | 'good' | 'friction';
  tool: string;
  destinationSlot: number;
  previousValue: number | null;
  nextValue: number | null;
  previousLevel: string | null;
  nextLevel: string | null;
  reason: 'value-change' | 'level-change' | 'rank-change';
};
```

This is a UI transition contract, not a new persistent event history. It can be derived from successive PULSE snapshots.

The event must be tied to a real meaningful change. Do not emit one for every poll or every insignificant floating-point fluctuation.

### 11.5 Animation lifecycle and safeguards

Prepare for:

- Initial hydration without animation.
- Meaningful score or level changes.
- Lane-specific ranking changes.
- Multiple simultaneous changes.
- Coalescing repeated updates.
- A bounded number of concurrent bubbles.
- Cancellation or supersession of stale transitions.
- Re-measurement or cancellation after a resize.
- Immediate commit when reduced-motion preference is active.

A sensible initial target after visual tuning is approximately 350–550ms for travel, followed by a brief 120–180ms arrival treatment. These are tuning values, not rigid requirements.

Use a small number of simultaneous particles. If several evaluations change together, stagger them slightly or coalesce them; do not flood the surface.

The animation should be driven by the PULSE surface coordinate system, so it works in both bottom and sidebar compositions. The origin remains the bottom edge of the surface, while the measured destination is the target cell.

### 11.6 What to implement before the animation itself

During the earlier stages, prepare:

- Stable metric/tool identities.
- Stable visual slot identities.
- Previous/next snapshot comparison.
- Destination-slot resolution.
- A surface-relative coordinate model.
- A bounded transition queue/coordinator.
- A commit-after-arrival path.

The actual bubble rendering and travel animation should be implemented only after independent lane ordering, dynamic capacity, and both surface compositions work correctly.

This avoids building animation around unstable row indices or a layout that will immediately change.

---

## 12. API and data compatibility

Preserve the existing `/pulse` response fields and semantics wherever possible for existing consumers.

Add the new lane-oriented data additively. A suitable conceptual shape is:

```
{
  "lanes": {
    "live": [],
    "goal": [],
    "good": [],
    "friction": []
  }
}
```

Each lane entry should contain the tool identity, score/value, level, state, support metadata, and any relevant source distinction.

For FRICTION, retain separate user and objective values/counts even if the compact UI displays one primary intensity.

Keep existing `rows` and `columns` fields available during migration. The new desktop UI can consume `lanes`, while older consumers continue to use the established shape.

Do not persist animation particles or raw transition history. The UI derives transitions from snapshots and stable identities.

Keep the existing API column conventions compatible; the desktop may continue to render LIVE in addition to the current semantic columns.

---

## 13. Implementation sequence

The following order is intentional. Each stage should leave the project in a coherent, testable state.

### Stage 0 — Confirm runtime and host constraints

- Verify the current Hermes Desktop placement capabilities and orientation signals.
- Confirm actual sizing behavior for the existing bottom-dock registration.
- Confirm the supported plugin runtime imports and workspace-opening contract.
- Record the supported path for the sidebar composition, or document the host limitation if one exists.

**Exit condition:** No sidebar or host behavior is assumed without evidence. The current bottom placement still works.

### Stage 1 — Correct persistence restoration

- Add profile-scoped lazy restoration for prototype banks, capsules, and the current goal vector.
- Ensure the restoration path runs before PULSE view generation and before feedback association.
- Add one-time concurrency protection.
- Keep database work out of the synchronous tool-start hot path.
- Ensure reset does not trigger unwanted rehydration.
- Make live and DB fallback views consistent.

**Exit condition:** After a process restart, the same profile reconstructs its prior PULSE evidence and evaluations without requiring new tool calls or new user feedback. A deliberate reset remains empty.

### Stage 2 — Correct readiness and feedback classification

- Replace shared row readiness with metric-specific readiness.
- Add per-metric support counts and thresholds.
- Retain `min_labeled` as a backwards-compatible numeric default, not a shared evidence pool.
- Make GOAL independent of explicit user-feedback labels.
- Keep user and objective FRICTION separate.
- Allow provisional scores to remain visible.
- Unify the feedback phrase table.
- Add tests for positive, negative, ambiguous, unlabeled, and objective-error paths.

**Exit condition:** Each cell reports a correct score, support count, and readiness state. Weak evidence is visible but not promoted to ready. Tool errors never become user dissatisfaction.

### Stage 3 — Build independent PULSE lanes

- Replace the global matrix ordering with four lane-specific selectors.
- Add stable lane-specific sorting and ranking hysteresis.
- Put tool names inside their own cells.
- Implement equal-width lanes and equal-size cells.
- Add dotted evaluation marks and restrained support treatment.
- Preserve full tool names and evidence details in a tooltip/detail surface.
- Keep existing expanded PULSE inspection functional.

**Exit condition:** Each lane can rank a different tool first without affecting the ordering of the others. No separate name rail remains.

### Stage 4 — Add measured capacity and surface compositions

- Replace width-only layout inference with measured width-and-height composition detection.
- Add the bottom composition: compact PORTRAIT left, four PULSE lanes right.
- Add the sidebar composition: PORTRAIT top, PULSE bottom.
- Implement symmetric flexible shrinking for sidebar sections.
- Compute visible capacity from available space.
- Preserve readability under narrow, short, and collapsed allocations.
- Replace mutually exclusive compact tabs with activation/focus controls.
- Retain DRIFT in the expanded workspace.

**Exit condition:** Bottom and sidebar are recognizably distinct layouts. Resizing changes capacity smoothly without breaking lane equality, hiding one module, or forcing the host to sacrifice space needed by other plugins.

### Stage 5 — Add the bubble transition

- Implement snapshot diffing and meaningful-change detection.
- Add transition objects with sequence numbers and stable identities.
- Keep committed cell content visible during travel.
- Measure destination cell coordinates in the current surface.
- Animate the bubble from the bottom edge to the destination.
- Commit new tool names and values at arrival.
- Add the short arrival flash/sparkle.
- Implement cancellation, coalescing, resize handling, and reduced-motion behavior.

**Exit condition:** A meaningful evaluation update visibly arrives at the right cell. Ranking changes update the correct destination without stale names, flicker, or out-of-order commits.

### Stage 6 — Regression and polish

- Run the full relevant backend and desktop regression suite.
- Test both live and restored data.
- Test the expanded workspace and portrait templates.
- Tune dot density, typography, margins, and animation timing.
- Remove dead table-layout code only after the new path is verified.
- Verify runtime packaging and the generated self-contained `desktop/plugin.js`.

**Exit condition:** The redesigned strip remains stable during actual tool activity, restarts, profile changes, resize events, and expanded-workspace use.

---

## 14. Required test matrix

### Backend and learning

- Fresh profile with no PULSE evidence.
- One GOOD sample: provisional score visible.
- Two GOOD samples: still provisional.
- Threshold number of GOOD samples: ready.
- User FRICTION samples counted independently.
- Objective failures counted independently.
- Objective failure does not increment user FRICTION support.
- GOAL score available with reference capsules but no labeled feedback.
- GOAL provisional/ready transition follows reference support, not labeled-feedback count.
- Empty reference bank produces no fabricated GOAL score.
- Positive, negative, mixed, ambiguous, and unmatched feedback phrases.
- Feedback attaches only to an eligible recent capsule.
- Database fallback and live matrix agree.

### Persistence and lifecycle

- Restart and restore prototype banks.
- Restart and restore recent capsules.
- Restart and restore goal vector.
- Feedback before the first dashboard poll.
- Concurrent first reads do not duplicate hydration.
- Explicit reset does not restore deleted state.
- Separate profiles do not share learned evidence.
- Raw user prompts and tool outputs are not added to persistence.

### Matrix and layout

- Each lane produces a different ranking when its evidence requires it.
- Tool name appears inside every non-empty cell.
- All lanes retain equal width.
- Empty cells do not stretch other cells.
- Bottom surface shows one, two, or three rows as space permits.
- Tall sidebar maintains four horizontal lanes.
- Sidebar PORTRAIT and PULSE shrink proportionally.
- Narrow but shallow surface remains bottom mode.
- Very short/collapsed allocation remains stable and readable.
- Activation controls do not hide the other compact module.
- Expanded PULSE and DRIFT still work.

### Bubble transition

- Initial load does not animate.
- Small value changes below the meaningful-change threshold do not animate.
- A meaningful value/level change creates one transition.
- A rank change targets the new slot.
- The old tool name remains until arrival.
- Multiple changes are bounded and ordered.
- A newer event supersedes stale pending work.
- Resize during travel cannot commit to an obsolete coordinate.
- Reduced-motion mode commits immediately.
- Tool identity and metric identity remain correct throughout.

---

## 15. Acceptance criteria

The redesign is complete when all of the following are true:

1. PULSE has four equal, independent lanes: LIVE, GOAL, GOOD, FRICTION.
2. Every cell carries its own tool name and dotted evaluation.
3. Each metric ranks tools independently.
4. GOAL does not wait for labeled user feedback.
5. GOOD, user FRICTION, and objective FRICTION have independent readiness counts.
6. Below-threshold scores remain visible as provisional, not falsely ready or silently neutral.
7. Existing `min_labeled` configurations continue to work as backwards-compatible threshold defaults.
8. Tool errors cannot be mistaken for user dissatisfaction.
9. PULSE evidence survives process restart within the same profile.
10. The live view and DB fallback agree.
11. Bottom and sidebar use distinct compositions and adapt to actual allocated dimensions.
12. PORTRAIT and PULSE coexist in compact mode; activation does not make them mutually exclusive.
13. DRIFT and expanded inspection remain available.
14. The bubble transition is prepared around stable data identity and destination slots, then implemented without premature content replacement.
15. No raw prompts, reasoning, or tool outputs are persisted, and no memory files are written.

---

## 16. Relevant implementation entry points

| Area                | Current entry point                 | Main responsibility                                        |
| ------------------- | ----------------------------------- | ---------------------------------------------------------- |
| Prototype state     | `pulse/prototypes.py`               | Independent evidence banks and readiness                   |
| Feedback detection  | `pulse/feedback.py`                 | Canonical conservative phrase detector                     |
| Matrix calculations | `pulse/matrix.py`                   | Per-metric score, support, state, and ranking input        |
| Persistence         | `pulse/persistence.py`              | Load/restore and persist bounded derived state             |
| Event lifecycle     | `core/collector.py`                 | Feedback association and event handling                    |
| Engine lifecycle    | `engine.py`                         | Hydration, reset semantics, PULSE view                     |
| API                 | `dashboard/plugin_api.py`           | Live/fallback parity and additive response shape           |
| Settings            | `core/settings.py`                  | Backwards-compatible threshold defaults                    |
| Desktop strip       | `desktop/plugin.js`                 | Lane rendering, responsive compositions, transitions       |
| Placement contract  | Hermes Desktop plugin SDK/host docs | Verify bottom/sidebar support and available sizing signals |

Useful source files:

- `pulse/prototypes.py`
- `pulse/feedback.py`
- `pulse/matrix.py`
- `pulse/persistence.py`
- `core/collector.py`
- `core/settings.py`
- `dashboard/plugin_api.py`
- `engine.py`
- `desktop/plugin.js`

---

## 17. Recommended delivery order

Do not begin with the bubble animation.

The safest sequence is:

**Restore evidence → correct readiness → expose independent lane data → build lane UI → establish bottom/sidebar compositions → add bubble transitions.**

This order fixes the underlying truth of the PULSE display before introducing more expressive presentation. It also gives the bubble a stable event model, destination geometry, and content-commit lifecycle to work with.

The result should feel like a compact instrument that is quietly observing and updating—not a conventional dashboard squeezed into a 96px strip.

Define the metric scoring contractsSpecify the exact PULSE response schema