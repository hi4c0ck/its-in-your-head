# Stage 0 — Hermes Desktop Host/Placement Contract (IiYH)

Status: COMPLETE (read-only evidence pass; executed 2026-11-09 on the installed
Nov 6 build). Sources: SDK doc (`website/docs/developer-guide/desktop-plugin-sdk.md`),
installed bundle (`.../win-unpacked/resources/app.asar.unpacked/dist/assets/` —
`main-BCbglDVj.js`, `capabilities-BScBjxv6.js`), and the plugin's own
registration (`desktop/plugin.js`). All bundle hits quoted from the installed
build, not the repo docs.

## Q1 — Can the host place a plugin pane in a SIDEBAR (vs bottom dock only)?

**Answer: YES — as a pane in the left/right PANEL ZONE (tabbed alongside
files/review/terminal). Separately, plugins can also add rows to the LEFT NAV
RAIL via `SIDEBAR_NAV_AREA` (route + label + codicon).**

- `placement` is a 5-way semantic role: `'main' | 'left' | 'right' | 'top' |
  'bottom'` (SDK doc, "Panes": "the pane stacks (as tabs) with existing panes
  of that role; the user can drag it anywhere afterward").
- Bundle proves role bucketing: `e.placement===`main`?0:e.placement?1:2` then
  groups pane ids per placement (`main-BCbglDVj.js`).
- Zone orientation is first-class in the layout: `j=m?`right`:`bottom`` with
  `sidebarGroup` / `narrow` computed flags over the pane set
  (`main-BCbglDVj.js`).
- IiYH already registers a second pane on the right zone today:
  `data: { placement: 'right', width: '400px' }` (`desktop/plugin.js:4491`) —
  i.e. the vertical-composition slot exists and is in use by this plugin.
- Confidence: **proven** (doc + bundle + the plugin's own right-placement
  registration).

Terminology guard: "sidebar" in this app means two different surfaces — the
left NAV RAIL (core rows + plugin `SIDEBAR_NAV_AREA` rows, no pane content)
and the right/left PANEL ZONES (tabbed panes). PULSE's vertical composition is
a **right-zone pane**, not a nav rail.

## Q2 — Does the host expose a placement/orientation signal a plugin can read?

**Answer: YES — `host.state.viewport` (`{ width, height, narrow }`,
ReadableAtom), present in the installed build. No "which zone owns my pane"
atom exists; orientation is inferred, not declared.**

- Installed build, `capabilities-BScBjxv6.js`: the plugin host namespace is
  `state:{activeSessionId…, viewport:bU(wA)}` — `viewport` is materialized in
  this exact build (not docs-ahead).
- `host.paneVisibility(paneId)` is also present and feature-detect is live in
  the build: `typeof Qn.paneVisibility==`function`` (`main-BCbglDVj.js`).
- What the host does NOT give: an orientation/placement enum for your own
  pane. A plugin picks its composition from `viewport.narrow` / measured pane
  box (a pane rendered in the right zone is ~400px wide × tall; in the bottom
  dock it is wide × 96px) — the detection is the plugin's, the host only
  supplies the viewport atom.
- Confidence: **proven** (atom existence in build + doc); the
  "viewport ⇒ composition" inference is **inferred** (design claim, not a host
  guarantee — per plan §8.4, this is exactly why the verdict below keeps the
  registered placement intact).

## Q3 — When multiple plugins share a surface, how are dimensions allocated?

**Answer: tabbing, not splitting — by default; splits only after a user drag,
allocated by normalized weights.**

- Same-placement panes stack as TABS in one zone slot (SDK doc "Panes";
  bundle: `newTab` / `tabMenuPrefix` / active-tab group state in
  `main-BCbglDVj.js`). Two plugins both docked under `workspace` bottom =
  one tab strip, one active pane — no parallel sizing.
- When the user drags a pane into a split, the zone becomes a split tree:
  children render `flex:${weights[i]} ${weights[i]} 0px` with normalized
  shares `weights[t]/(N||1)` (bundle `main-BCbglDVj.js`). Allocation =
  proportional weights; the declared pane size seeds the resting weight
  (`Math.abs(i.height-(e??r??NT))>IPe` resting clamp, `NT` = default when no
  size declared — the "don't claim half the zone" case from the docs).
- Confidence: **proven** for tabs + weighted-flex split rendering;
  **inferred** that declared width/height is the weight seed (resting-clamp
  code path).

## Q4 — Is a declared pane `height` fixed, preferred, or a bottom-dock hint?

**Answer: PREFERRED (initial) size, not fixed. User resizers override it;
undocked/floating panes use it as an absolute box.**

- Bottom zone: the declared `height` seeds the initial/resting size; the
  splitter mutates it (weighted flex, Q3). No min/fixed enforcement in the
  pane path (the build-only `minWidth` field exists on core pane
  registrations — `data:{…minWidth:e.minWidth,…}` — but is undocumented and
  not what height uses).
- Floating panes: `class "a-floating-pane"` renders
  `style:{left,top,width,height}` — absolute, user-dragged position
  (`main-BCbglDVj.js`).
- SDK doc wording corroborates: "Declare a `width`/`height` so the pane
  doesn't claim half the zone" — i.e. it controls the *initial* claim.
- Confidence: **proven**.

## Supported path — verdict

**Both compositions are supported and may be claimed:**

1. **Bottom bar (shallow horizontal):** KEEP the current registration exactly
   as-is — `placement: 'main'`, `dock: { pane: 'workspace', pos: 'bottom' }`,
   `height: '96px'` (`desktop/plugin.js:4475-4478`). The 96px is an initial
   claim the user can resize; that is the contract, not a bug.
2. **Sidebar / vertical (PORTRAIT top half, PULSE bottom half):** use a
   right-zone pane — `placement: 'right'` + `width` (the pattern this plugin
   already ships for Mosaic at `width: '400px'`, `plugin.js:4491`). This is a
   verified host path, not width-detection theater: the host places it in the
   panel zone, tabs it with files/review, and the pane's own rendered box
   (via `host.state.viewport` + a measured ref) tells the content which
   composition to render.

Rules carried into Stages 4–5:

- No invented `placement` values — the enum is `main|left|right|top|bottom`.
- The "sidebar" composition is a **right-zone pane**, never the nav rail.
- Keep the registered bottom-dock behavior intact (plan §8.4 last line) until
  Stage 4 ships the dual composition; composition switching is plugin-side
  inference (viewport atom + pane box), host-side it is just two placements.
- `enforce` / `minWidth` are build-internal (core uses them; docs don't list
  them) — do not depend on them in plugin data.
- Feature-detect `host.paneVisibility` on registration (the build's own
  pattern) so an older host degrades gracefully.
