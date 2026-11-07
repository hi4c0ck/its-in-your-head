// tests/test_desktop_petstate.mjs — node test for the desktop pet state machine.
// Extracts the pure functions between the PETSTATE markers in desktop/plugin.js
// and asserts the 7-state deterministic behaviour. Run: node tests/test_desktop_petstate.mjs
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const dir = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(dir, '..', 'desktop', 'plugin.js'), 'utf8')
const block = src.slice(src.indexOf('// PETSTATE-START'), src.indexOf('// PETSTATE-END'))
assert.ok(block.length > 0, 'PETSTATE markers found in plugin.js')
const consts = ['STALE_S', 'OK_WINDOW_S'].map((name) => {
  const m = src.match(new RegExp('const ' + name + ' = \\d+'))
  assert.ok(m, name + ' defined in plugin.js')
  return m[0]
}).join('\n')
const { petState, petIconName, tickerPhrase, PET_ICONS } =
  new Function(consts + '\n' + block + '\n;return { petState, petIconName, tickerPhrase, PET_ICONS };')()

// The tintable icon atlas (module level): pull its defs to check the pet
// icon names actually exist as symbols, and that the set is color-free.
const iconBlock = src.slice(src.indexOf('// IYH-ICONS-START'), src.indexOf('// IYH-ICONS-END'))
assert.ok(iconBlock.length > 0, 'IYH-ICONS markers found in plugin.js')
const { IYH_ICON_DEFS } = new Function(
  iconBlock + '\n;return { IYH_ICON_DEFS };')()

const NOW = 1000000
let n = 0
const eq = (actual, expected, label) => {
  assert.equal(actual, expected, label + ' (got ' + actual + ')')
  console.log('ok ' + (++n) + ' - ' + label)
}

// 1. No events -> idle, icon is the idle pet symbol.
eq(petState(null, NOW), 'idle', 'no event -> idle')
eq(petIconName('idle'), PET_ICONS.idle, 'idle icon resolves')

// 2. Fresh turn / api event -> processing.
eq(petState({ type: 'turn', ts: NOW - 5 }, NOW), 'processing', 'fresh turn -> processing')
eq(petState({ type: 'api', ts: NOW - 5 }, NOW), 'processing', 'fresh api no finish -> processing')

// 3. Fresh api with stop finish -> successful; after the window -> idle.
eq(petState({ type: 'api', ts: NOW - 5, finish_reason: 'stop' }, NOW), 'successful', 'fresh api stop -> successful')
eq(petState({ type: 'api', ts: NOW - 45, finish_reason: 'stop' }, NOW), 'idle', 'old api stop -> idle')

// 4. Errors -> friction.
eq(petState({ type: 'error', ts: NOW - 5 }, NOW), 'friction', 'error event -> friction')
eq(petState({ type: 'api', ts: NOW - 5, status_code: 500 }, NOW), 'friction', 'api 500 -> friction')

// 5. Tool-name driven states.
eq(petState({ type: 'api', ts: NOW - 5, tool: 'read_file' }, NOW), 'reading', 'read_file -> reading')
eq(petState({ type: 'api', ts: NOW - 5, tool: 'write_file' }, NOW), 'writing', 'write_file -> writing')
eq(petState({ type: 'api', ts: NOW - 5, tool_name: 'run_tests' }, NOW), 'verifying', 'run_tests -> verifying')

// 6. Stale anything -> idle.
eq(petState({ type: 'error', ts: NOW - 3600 }, NOW), 'idle', 'stale error -> idle')

// 7. All 7 states map to distinct pet icon names, every one a real symbol
// in the tintable atlas — and the atlas carries no hardcoded colors
// (currentColor only; tinting happens at the use site via CSS color).
const iconNames = new Set(Object.values(PET_ICONS))
eq(Object.keys(PET_ICONS).length, 7, 'exactly 7 pet states')
eq(iconNames.size, 7, '7 distinct pet icon names')
for (const name of iconNames) {
  assert.ok(IYH_ICON_DEFS[name], 'atlas symbol exists: ' + name)
}
eq(Object.keys(IYH_ICON_DEFS).length >= 11, true,
  'atlas covers the 7 pet states + UI chrome (help, gear, expand, close)')
assert.ok(!/#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(/.test(JSON.stringify(IYH_ICON_DEFS)),
  'atlas defs are color-free (no hex/rgb values — currentColor only)')

// 8. Ticker: public phrasing only, never payload text.
eq(tickerPhrase(null, NOW), 'no events yet', 'ticker empty')
eq(tickerPhrase({ type: 'api', ts: NOW - 5, tool: 'read_file' }, NOW), 'read_file ok · 5s', 'ticker tool ok')
eq(tickerPhrase({ type: 'api', ts: NOW - 5, tool: 'read_file', status_code: 500 }, NOW), 'read_file fail · 5s', 'ticker tool fail')
eq(tickerPhrase({ type: 'turn', ts: NOW - 90 }, NOW), 'turn ok · 1m', 'ticker turn')
eq(tickerPhrase({ type: 'error', ts: NOW - 5, status_code: 429 }, NOW), 'error 429 · 5s', 'ticker error')
const leak = tickerPhrase(
  { type: 'api', ts: NOW, tool: 'read_file', user_message: 'SECRET REASONING', reasoning: 'THINK' }, NOW)
assert.ok(!leak.includes('SECRET') && !leak.includes('THINK'), 'ticker never leaks payload fields')
console.log('ok ' + (++n) + ' - ticker never leaks payload fields')

// 9. Determinism: same input twice, same output.
const ev = { type: 'api', ts: NOW - 3, tool: 'grep_search' }
eq(petState(ev, NOW), petState(ev, NOW), 'petState is deterministic')

console.log('PASS petstate: ' + n + ' assertions')
