// tests/test_desktop_themecompat.mjs — node test for desktop theme compatibility.
// 1. Every CSS var the pane references (var(--x) in desktop/plugin.js) either
//    exists in the installed Hermes desktop build's styles.css or is used with
//    an explicit fallback clause.
// 2. Heat palette contrast: hottest cell (full accent) + danger tick clear
//    >=3:1 on light and dark host surfaces.
// Run: node tests/test_desktop_themecompat.mjs
import { readFileSync, existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const dir = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(dir, '..', 'desktop', 'plugin.js'), 'utf8')

// Token source (hermetic): CI cannot read a machine-local Hermes install, so
// the build's defined-var set is pinned in tests/fixtures/desktop-theme-vars.txt
// (snapshot of apps/desktop/src/styles.css, taken 2026-11-06). To re-derive
// from a live build after a Hermes desktop update, run with:
//   HERMES_DESKTOP_STYLES=<path-to-src/styles.css> node tests/test_desktop_themecompat.mjs
// and regenerate the fixture when the build changes. Never fail open: no
// source -> empty set -> non-fallback vars are caught, not silently passed.
const FIXTURE = join(dir, 'fixtures', 'desktop-theme-vars.txt')
const STYLES = process.env.HERMES_DESKTOP_STYLES
let hostVars = new Set()
const styleSrc = STYLES && existsSync(STYLES) ? STYLES : null
if (styleSrc) {
  const css = readFileSync(styleSrc, 'utf8')
  for (const m of css.matchAll(/(--[a-zA-Z0-9-]+)\s*:/g)) hostVars.add(m[1])
} else {
  if (!existsSync(FIXTURE)) {
    assert.fail('theme token source unavailable: set HERMES_DESKTOP_STYLES or restore tests/fixtures/desktop-theme-vars.txt')
  }
  hostVars = new Set(readFileSync(FIXTURE, 'utf8').split(/\r?\n/).filter(Boolean))
}

// 1. var() scan: name + whether a fallback comma follows at the same level.
const varRe = /var\(\s*(--[a-zA-Z0-9-]+)\s*(,|\))/g
const usages = []
for (const m of src.matchAll(varRe)) {
  usages.push({ name: m[1], fallback: m[2] === ',' })
}
assert.ok(usages.length >= 10, 'plugin.js references CSS vars (found ' + usages.length + ')')
const missing = usages.filter((u) => !hostVars.has(u.name) && !u.fallback)
assert.deepEqual(missing, [], 'every var(--x) exists in the build OR has a fallback: ' +
  JSON.stringify(missing))

// Explicit: the old unverified tokens must not appear as var() usages.
assert.ok(!/var\(--ui-popover/.test(src), '--ui-popover no longer used as var()')
assert.ok(!/var\(--ui-surface\b/.test(src), '--ui-surface no longer used as var()')
assert.ok(src.includes('var(--dt-popover'), 'popover uses the real --dt-popover token')
assert.ok(src.includes('var(--ui-bg-elevated'), 'popover falls back to --ui-bg-elevated')
assert.ok(src.includes('color-mix(in srgb, var(--dt-card'), 'popover has translucent color-mix fallback')
assert.ok(src.includes('var(--dt-border'), 'popover border has --dt-border fallback')
assert.ok(src.includes('var(--dt-destructive, var(--ui-accent))'),
  'danger token governs friction cells / negative drift (--dt-destructive, accent fallback)')

// V_DANGER is the single danger token: ends with the accent fallback,
// never a hardcoded color.
assert.ok(src.includes("const V_DANGER = 'var(--dt-destructive, var(--ui-accent))'"),
  'danger falls back to accent (theme token, not a hardcoded color)')

// 2. Heat contrast: the hottest cell is the full accent over the host surface
// (alpha-blend encoding, surface-relative). Default build accent --theme-
// midground #0053fd; danger --dt-destructive #cf2d56. Surfaces: light card
// #fcfcfc (--theme-neutral-card), task light surface #EBE5DB, dark card
// #161618 (dark --theme-neutral-card).
function lum(hex) {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((v) => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)))
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
}
const contrast = (a, b) => {
  const [l1, l2] = [lum(a), lum(b)].sort((x, y) => y - x)
  return (l1 + 0.05) / (l2 + 0.05)
}
const LIGHT = ['#fcfcfc', '#EBE5DB']
const DARK = ['#161618']
for (const [fg, label] of [['#0053fd', 'hottest cell (accent)'], ['#cf2d56', 'danger tick']]) {
  for (const bg of [...LIGHT, ...DARK]) {
    const r = contrast(fg, bg)
    assert.ok(r >= 3, label + ' ' + fg + ' vs ' + bg + ' contrast ' + r.toFixed(2) + ' >= 3')
    console.log('ok - ' + label + ' vs ' + bg + ' = ' + r.toFixed(2) + ':1')
  }
}

// Alpha encoding stays surface-relative (DOM opacity on theme-token
// backgrounds — same 0.12..1.0 scale, host surface is the blend base).
assert.ok(src.includes('(0.12 + 0.88 * v)'), 'heat cells use accent alpha over the host surface')

// 3. Semantic rendering (P1 lane 3): the pane is driven by the semantic
// routes, not by the legacy hourly lane; it never invents values.
assert.ok(src.includes("rest('/pulse')"), 'pane fetches /pulse as its primary view')
assert.ok(!src.includes("'/heatmap"), 'legacy hourly lane is not the pane primary view')
assert.ok(src.includes("rest('/portrait')"), 'pane fetches /portrait')
assert.ok(src.includes("rest('/drift')"), 'pane fetches /drift')
assert.ok(src.includes('pulse.columns'), 'pulse renders the 4 semantic columns from the response')
for (const col of ["'goal'", "'good'", "'friction'", "'unlabeled'"]) {
  assert.ok(src.includes(col), 'semantic column ' + col + ' present')
}
assert.ok(src.includes('tool_name'), 'pulse rows are dynamic (tool_name from the response)')
assert.ok(src.includes('data.user'), 'portrait consumes the USER track')
assert.ok(src.includes('data.agent'), 'portrait consumes the INTERPRETED (agent) track')
assert.ok(src.includes('data.dimensions'), 'portrait consumes the six-dimension track')
assert.ok(src.includes('data.available'), 'portrait gates on the evidence flag')
assert.ok(src.includes('evidence empty'), 'portrait renders the honest placeholder when empty')
assert.ok(src.includes('data.series'), 'drift consumes per-axis series')
assert.ok(src.includes('driftDelta'), 'drift renders per-axis deltas vs last snapshot')
assert.ok(src.includes('V_DANGER'), 'danger token (V_DANGER) still governs negative drift + friction cells')
assert.ok(src.includes('gridTemplateRows'),
  'PULSE lane is a CSS-grid matrix structure (layout engine sizes it; the one root ResizeObserver only reads frame orientation)')
assert.ok(src.includes('refetchInterval: PULSE_POLL_MS'), 'polling fallback stays mandatory')
assert.ok(src.includes('refetchInterval: POLL_MS'), 'polling fallback stays on every route')

console.log('PASS themecompat: ' + usages.length + ' var() usages checked, heat contrast >=3:1 on light+dark')
