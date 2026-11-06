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
const { petState, petGlyph, tickerPhrase, PET_GLYPHS } =
  new Function(consts + '\n' + block + '\n;return { petState, petGlyph, tickerPhrase, PET_GLYPHS };')()

const NOW = 1000000
let n = 0
const eq = (actual, expected, label) => {
  assert.equal(actual, expected, label + ' (got ' + actual + ')')
  console.log('ok ' + (++n) + ' - ' + label)
}

// 1. No events -> idle, glyph is the idle glyph.
eq(petState(null, NOW), 'idle', 'no event -> idle')
eq(petGlyph('idle'), PET_GLYPHS.idle, 'idle glyph resolves')

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

// 7. All 7 states have distinct non-empty glyphs.
const glyphs = new Set(Object.values(PET_GLYPHS))
eq(Object.keys(PET_GLYPHS).length, 7, 'exactly 7 pet states')
eq(glyphs.size, 7, '7 distinct glyphs')

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
