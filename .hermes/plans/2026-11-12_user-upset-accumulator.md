# User-Upset Friction Accumulator (PULSE, session lane)

## Purpose
Session-level "user is upset" friction signal: a decaying window over LLM
reasoning, driving a friction-lane entry + the friction glitch. Language-agnostic
(signal read from LLM reasoning, not user raw text).

## Evidence (why)
- Last 50 user turns: 47/50 skipped by the 12-word `MAX_WORDS` gate; the 3 that
  passed yielded 1 good label, 0 friction. User-friction bank: 0 rows ever.
- PULSE friction is deterministic-by-design (no LLM); `reasoning_seg` events
  (captured, 18 in this session) fold only into the PORTRAIT interpreted track
  (`engine.py:841`) — never into PULSE. That is the missing seam.
- Good lane bubbles today come from stale `good` prototypes (skill_view x2,
  11-11 21:10), i.e. the only semantic lane with evidence.

## Decisions (locked by owner)
- **D1** Signal source: `reasoning_seg` text only (aggregated per turn/turn_id),
  not final text. Rationale: reasoning is where the model narrates user state.
- **D2** Decay 0.73 per step, effective window 6 turns.
- **D3** Per-session, in-memory accumulator. Reset on `session_end`; also
  resets to 0 on backend-child restart (it is a "now" signal — no DB rows).

## Decisions (LOCKED — all vetted by owner)
- **D4** Two tiers on F' (0..1): 0.30 = lane entry + bubble; 0.65 = glitch
  active (intensity proportional to F'); exit on downward crossing of 0.30.
- **D5** Good-side drain, **F' only**: a short good reaction (existing
  good-phrase table, <=12 words — the table's one retained job) hard-resets
  F' to 0. "great!" answers "no friction NOW". Does NOT touch M.
- **D6** Two state variables, one input s_t (per-turn upset score):
  - **F'** — fast "friction now": D2 decay window (6 x 0.73). Drives the lane
    tiers (D4) and the glitch. Per-session, in-memory (D3).
  - **M** — slow mood, the prognostic number: `M_t = M_{t-1}*(1-beta) +
    beta*g_t`, **beta = 0.1** (simple default; good never erases strain, it
    pulls M toward good at the same slow rate).
    - M = **NEW TRAIT in the portrait interpreted track** (the slow/persistent
      layer). No new table, no new write-path state. Hydrate on session
      start, fold per turn, persist via portrait_history on session_end.
    - **Prognosis = read-time query**: linear trend over the last ~5
      session-level M values from portrait_history. Nothing computed at
      write time; the forecast exists as soon as M persists. Revisit only
      when a real dive needs a dedicated store/horizon.
  - Same physics, two time constants: F' = awareness now, M = relationship
    state. The upset signal feeds both.

## Mechanics
- **Anchors**: two fixed phrase families (upset ~8, good ~6, e.g. "user is
  upset/annoyed/irritated with me", "user's tone is harsh", "user is losing
  patience", "user is satisfied", "user thanks the agent"). Embedded at engine
  hydrate with the ACTIVE encoder, re-embedded on encoder change. No LLM call,
  no network — guardrail "PULSE never invokes an LLM" intact.
- **Per turn** (turn boundary = LLM response completed):
  - concat that turn's reasoning_seg texts (cap ~2000 chars), embed with the
    active encoder.
  - `s = max_j cos(emb, anchor_j)` over the UPSET family, clamped to [0,1].
  - `F_raw = sum_{i=0..5} 0.73^i * s_{t-i}` (last 6 turns, missing = 0).
  - `F' = F_raw / 3.14` (3.14 = sum of decay powers 0..5).
  - if the turn's user message is a good-table hit -> `F_raw = 0`.
- **Lane payload**: engine exposes F' + tier state; `pulse/matrix.py` adds a
  pseudo-tool entry `user` to the friction lane when F' >= 0.30 (value = F').
- **Pane** (`desktop/plugin.js`): the friction lane renders the entry like any
  tool row; entry bubble lands through the existing `IYH_FRICTION_TRIGGER`
  path (friction-driven glitch, c5532fc) — glitch gates on F' >= 0.65,
  intensity scaled by F'.

## Retained / retired
- `MAX_WORDS` gate survives **only** on the phrase-table capsule-labeling path
  (per-tool objective attribution). Table is NOT extended or weighted
  (owner veto: saturation -> false-positive swamp).
- Friction-phrase table hits keep labeling the last-used tool's capsule
  (existing `apply_feedback` path, unchanged).

## Emergent calibration (from the math)
- 1 upset turn: F' peak ~0.32 (lane flickers on).
- 2 consecutive: ~0.55 (lane solid).
- 3 consecutive: ~0.73 (glitch). Sustained rages -> glitch; one-offs -> soft
  indicator.

## Files
- `pulse/upset.py` (new): anchor embed + accumulator + turn-step API.
- `engine.py`: aggregate reasoning per turn, step accumulator on turn
  boundary, expose F'/tier on lane payload, reset on session_end.
- `pulse/matrix.py`: `user` pseudo-entry in friction lane at F' >= 0.30.
- `desktop/plugin.js`: glitch gate at 0.65 + intensity ~ F' (small diff on
  the c5532fc friction trigger).
- tests: unit (trajectory build/decay/good-reset/tier crossings/restart
  reset), mjs pane (entry render + glitch gate).

## Gates
- Full suite: 20 mjs + pytest green.
- Syntax gate: `node --input-type=module --check` on plugin.js.
- No deploy to live pane without owner permission (standing constraint).

## Status
All decisions locked by owner (D1-D6). Plan is implementation-ready:
"go" = build against the frozen repo (HEAD c5532fc), then run the gates.
No live-pane deploy without owner permission (standing constraint).

## Addendum 2026-11-12 (owner live rule, supersedes D2 dilution)
Owner directive (live session, after deployment): "react instantly when I'm
very upset — no complex multi-turn ladder." Two changes:
1. **Immediacy feed**: `on_user_message` — the user's OWN message is scored
   at `user_turn` (active encoder is multilingual: language-agnostic),
   stepping F' immediately. No waiting for the next turn's reasoning to
   finalize. D5 good-resets still clear the window; the feed is skipped on
   them. Raw text stays in memory only (C6).
2. **f_prime normalizes by the window ACTUALLY PRESENT** (not the full
   6-turn 3.1432 constant): one strong turn (s≥0.65) reads at full
   strength -> single harsh message can clear BOTH tiers. The 0.73
   per-step decay (D2) is unchanged; it now governs how the window
   accumulates/decays, not whether a single turn can count.
Net effect: harsh message NOW -> lane entry + (s ≥ 0.65) sustained
glitch on the next /pulse poll. Decay over ~6 neutral steps. M (D6)
unchanged.
