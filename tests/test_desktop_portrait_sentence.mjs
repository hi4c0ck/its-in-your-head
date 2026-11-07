// tests/test_desktop_portrait_sentence.mjs — E4: the deterministic
// portrait lines (self + notice) in desktop/plugin.js.
// Source-shape pins (P3-style, never bare identifiers) + a pure
// simulation of portraitLines() on fixture payloads (the function is
// extracted by marker-free slicing and eval'd — no React runtime).
// Run: node tests/test_desktop_portrait_sentence.mjs
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const dir = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(dir, '..', 'desktop', 'plugin.js'), 'utf8')

let n = 0
const ok = (cond, label) => { assert.ok(cond, label); console.log('ok ' + (++n) + ' - ' + label) }

// --- shape pins ----------------------------------------------------------
ok(/const SELF_CONF\s*=\s*0\.5/.test(src), 'self-line gate constant 0.5 (live-data calibrated)')
ok(/const HIGH_SIGNAL\s*=\s*0\.15/.test(src), 'notice gate reuses the reinforce threshold 0.15 (module scope)')
ok(/function portraitLines\(data\)/.test(src), 'portraitLines(data) pure function exists')
ok(/const PATTERN_SELF\s*=\s*\{/.test(src), 'self-line template table')
ok(/const PATTERN_NOTICE\s*=\s*\{/.test(src), 'notice-line noun table')
ok(/'you supply rich context'/.test(src), 'context_richness self template (word-only)')
ok(/'you demand reasoning depth'/.test(src), 'analytical_depth self template (word-only)')
ok(/'you fence your scope explicitly'/.test(src), 'scope self template (word-only)')
ok(/'you want plain output, no preamble'/.test(src), 'directness self template (word-only)')
ok(/'you explore before you commit'/.test(src), 'exploration self template (word-only)')
ok(/'you iterate in small loops'/.test(src), 'iteration self template (word-only)')
ok(/'The agent reads your ' \+ PATTERN_NOTICE\[best\.p\.name\] \+ ' stronger than you say it'/.test(src), 'notice sentence assembled from the noun table')
ok(/amp < HIGH_SIGNAL/.test(src), 'notice gate: amplification >= 0.15 (negative amp never produces a notice)')
ok(/c < SELF_CONF/.test(src), 'notice gate: confidence >= 0.5 as well')
// determinism: confidence desc, name-asc tie-break; no randomness
ok(/b\.c - a\.c\)/.test(src), 'ranked by confidence desc')
ok(/String\(a\.p\.name \|\| ''\) < String\(b\.p\.name \|\| ''\)/.test(src), 'name-asc tie-break (locale-independent)')
ok(!/Math\.random/.test(src.slice(src.indexOf('function portraitLines'), src.indexOf('function PortraitView'))), 'portraitLines is deterministic (no Math.random)')
// no LLM, no digits in the prose: the template tables are word-only
const selfTbl = src.slice(src.indexOf('const PATTERN_SELF'), src.indexOf('}', src.indexOf('const PATTERN_SELF') + 60 * 4))
ok(!/\d/.test(selfTbl), 'self templates contain no digits')
const noticeTbl = src.slice(src.indexOf('const PATTERN_NOTICE'), src.indexOf('}', src.indexOf('const PATTERN_NOTICE') + 60 * 4))
ok(!/\d/.test(noticeTbl), 'notice nouns contain no digits')
ok(!/openai|anthropic|fetch\(|axios|LLM|llm/i.test(src.slice(src.indexOf('function portraitLines'), src.indexOf('function PortraitView'))), 'no LLM call in the portrait-line layer')

// strip wiring: awareness line is conditional, original readout row kept
ok(/const stripLine = lines\.notice \|\| lines\.self/.test(src), 'strip line: notice takes priority over self')
ok(/\.\.\.\(stripLine \? \[jsx\('span'/.test(src), 'strip sentence row rendered only when gated in')
ok(/confNum \? confNum \+ ' conf · ' : ''/.test(src), 'original readout call-form intact (P3)')

// expanded wiring: both lines stacked, gated, no filler
ok(/pl\.self \|\| pl\.notice/.test(src), 'expanded view renders lines only when present')
ok(/pl\.notice/.test(src.slice(src.indexOf('function ExpandedPortrait'), src.indexOf('function ExpandedDrift'))), 'expanded notice line rendered in the PORTRAIT view')

// --- pure simulation of portraitLines (fixture payloads) ----------------
// The layer is self-contained (no React/jsx deps): extract the
// constants + function source and run it as a plain JS body.
const fnSrc = src.slice(
  src.indexOf('const SELF_CONF'),
  src.indexOf('function PortraitView'))
assert.ok(fnSrc.includes('function portraitLines'), 'portraitLines source extracted')
const portraitLines = new Function(fnSrc + '\n; return portraitLines;')()

const perc = (entries) => ({ perception: { perception_available: true, patterns: entries } })
const E = (o) => ({ name: 'x', confidence: null, amplification: null, ...o })

// (1) live-shaped: CR conf 0.89 amp +0.75, directness conf 0.46 amp +0.19
let r = portraitLines(perc([
  E({ name: 'context_richness', confidence: 0.89, amplification: 0.75 }),
  E({ name: 'directness', confidence: 0.46, amplification: 0.19 }),
]))
assert.equal(r.self, 'You supply rich context')
assert.equal(r.notice, 'The agent reads your context stronger than you say it')
ok(true, 'live-shaped fixture: self line from top pattern, notice from the max-positive-amp gate-passing entry')

// (2) two patterns past the gate -> paired sentence
r = portraitLines(perc([
  E({ name: 'context_richness', confidence: 0.9, amplification: 0.1 }),
  E({ name: 'directness', confidence: 0.7, amplification: 0.1 }),
]))
assert.equal(r.self, 'You supply rich context and want plain output, no preamble')
ok(true, 'paired self sentence when top-2 both >= 0.5')

// (3) gate: top pattern under 0.5 -> no self line (weak session, no filler)
r = portraitLines(perc([E({ name: 'scope', confidence: 0.3, amplification: -0.2 })]))
assert.equal(r.self, null)
assert.equal(r.notice, null)
ok(true, 'confidence < 0.5 AND negative amp -> both lines absent (honest)')

// (4) negative amplification never produces a notice
r = portraitLines(perc([
  E({ name: 'exploration', confidence: 0.8, amplification: -0.23 }),
]))
assert.equal(r.self, 'You explore before you commit')
assert.equal(r.notice, null)
ok(true, 'negative amp (agent contained) -> self line only')

// (5) notice picks the max amp among gate-passing entries; tie -> name asc
r = portraitLines(perc([
  E({ name: 'scope', confidence: 0.6, amplification: 0.2 }),
  E({ name: 'context_richness', confidence: 0.9, amplification: 0.4 }),
]))
assert.equal(r.notice, 'The agent reads your context stronger than you say it')
ok(true, 'notice follows the largest amplification, not recency')

// (6) absent/unavailable perception -> no lines, no throw
assert.deepEqual(portraitLines({}), { self: null, notice: null })
assert.deepEqual(portraitLines({ perception: { perception_available: false, patterns: [] } }), { self: null, notice: null })
assert.deepEqual(portraitLines(null), { self: null, notice: null })
ok(true, 'absent key / perception_available false / null data -> {null, null}, no throw')

// (7) determinism: identical inputs -> identical outputs (order-insensitive)
const pats = [
  E({ name: 'context_richness', confidence: 0.8, amplification: 0.3 }),
  E({ name: 'analytical_depth', confidence: 0.6, amplification: -0.05 }),
  E({ name: 'directness', confidence: 0.7, amplification: 0.16 }),
]
const a = portraitLines(perc(pats))
const b = portraitLines(perc([...pats].reverse()))
assert.deepEqual(a, b)
ok(true, 'payload order never changes the lines')

console.log('PASS desktop_portrait_sentence: ' + n + ' assertions')
