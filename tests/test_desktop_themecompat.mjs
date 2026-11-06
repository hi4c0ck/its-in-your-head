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

// Installed build: verified against
// C:/Users/user/AppData/Local/hermes/hermes-agent/apps/desktop/src/styles.css
// Findings: --ui-popover and --ui-surface are ABSENT (no definition anywhere
// in src/); the real popover token is --dt-popover. Bare --border/--card/
// --destructive/--ui-font-mono are likewise absent; --dt-border/--dt-card/
// --dt-destructive/--dt-font-mono exist. This test re-derives the set from
// that file when present so it stays honest if the build changes.
const STYLES = 'C:/Users/user/AppData/Local/hermes/hermes-agent/apps/desktop/src/styles.css'
let hostVars = new Set()
if (existsSync(STYLES)) {
  const css = readFileSync(STYLES, 'utf8')
  for (const m of css.matchAll(/(--[a-zA-Z0-9-]+)\s*:/g)) hostVars.add(m[1])
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
assert.ok(src.includes("'--dt-destructive'"), 'danger probe prefers the real --dt-destructive')

// resolveColors probe: ends with the accent fallback, never a hardcoded color.
assert.ok(src.includes("danger: danger || accent"), 'danger falls back to accent')

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

// Alpha-blend encoding stays surface-relative (unchanged behavior).
assert.ok(src.includes('globalAlpha = 0.12 + 0.88'), 'heat cells use accent alpha over cleared canvas')

console.log('PASS themecompat: ' + usages.length + ' var() usages checked, heat contrast >=3:1 on light+dark')
