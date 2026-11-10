// tests/test_desktop_perception_expansion.mjs — E2 spec §6 readout
// (per-pattern observed/interpreted bars, strongest-resemblance card,
// signed amplification ticks) in the expanded PORTRAIT view, plus the
// 96px strip byte-identity snapshot. Source-shape assertions on CALL
// forms + a sha256 pin of the pre-change Strip region — never a DOM
// render (no React runtime in node; P3-style).
// Run: node tests/test_desktop_perception_expansion.mjs
import { readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const dir = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(dir, '..', 'desktop', 'plugin.js'), 'utf8')

let n = 0
const ok = (cond, label) => { assert.ok(cond, label); console.log('ok ' + (++n) + ' - ' + label) }

const ep = src.slice(src.indexOf('function ExpandedPortrait'), src.indexOf('function ExpandedDrift'))
assert.ok(ep.length > 0, 'ExpandedPortrait block found')

// (a) six per-pattern rows with bars from a full fixture payload: one
// row per payload entry, capped at six, v0.1.4 flank-tick convention
ok(/patterns\.slice\(0,\s*6\)\.map/.test(ep), 'per-pattern rows map over payload entries (all six, capped)')
ok(/affTick\(p && p\.prompt_affinity/.test(ep), 'observed tick fed by prompt_affinity (never blended)')
ok(/affTick\(p && p\.agent_affinity/.test(ep), 'interpreted tick fed by agent_affinity (never blended)')
ok(/0\.25 \+ 0\.75 \* Math\.min\(1, Math\.max\(0, v\)\)/.test(ep), 'flank tick opacity convention (0.25+0.75v, clamped)')
ok(/0\.15/.test(ep), 'absent value renders a faint tick (opacity 0.15), never fabricated')
ok(/'PATTERNS'/.test(ep), 'per-pattern grid sub-header rendered')
ok(/'OBSERVED'/.test(ep) && /'INTERPRETED'/.test(ep), 'observed/interpreted column labels')

// (b) None-valued entries render '—' (the num() type guard), never a
// number — both value cells go through num()
ok(/v === null \|\| typeof v !== 'number' \|\| !Number\.isFinite\(v\) \? '—'/.test(ep), 'num() maps null/non-number to dash')
ok(/num\(p && p\.prompt_affinity\)/.test(ep), 'observed value cell goes through num()')
ok(/num\(p && p\.agent_affinity\)/.test(ep), 'interpreted value cell goes through num()')

// (c) strongest-resemblance card: name + its affinities + repeated +
// HIGH-SIGNAL mark; repeated mark only when repeated === true
ok(/strongP\.repeated\s*===\s*true/.test(ep), 'repeated mark gated on repeated === true')
ok(/strongP\.amplification\s*>=\s*HIGH_SIGNAL\s*\?\s*' · HIGH SIGNAL'/.test(ep), 'HIGH SIGNAL mark on the strongest card')

// settings-key disclosure: the pane's expanded surface has no settings
// query (the /settings poll lives in the Strip component, untouched);
// 0.15 is hardcoded WITH a comment pointing at the key, not read live.
// E4 hunk (disclosed): HIGH_SIGNAL moved to module scope so the
// portrait-line notice gate shares the constant — the pins below now
// match at module scope, not inside ep.
ok(/perception\.reinforce_min_amplification/.test(src), 'hardcode comment points at perception.reinforce_min_amplification')
ok(/const HIGH_SIGNAL\s*=\s*0\.15/.test(src), 'HIGH_SIGNAL default 0.15 at module scope')
ok(!/useQuery/.test(ep), 'ExpandedPortrait stays a pure component (no settings poll added)')

// (d) high-signal mark: amplification 0.2 -> >= 0.15 marks; 0.1 does
// not. The threshold is INCLUSIVE on the mark, STRICT on the tick.
ok(/strongP\.amplification\s*>=\s*HIGH_SIGNAL/.test(ep), 'mark threshold is >= 0.15 (0.2 marks, 0.1 does not)')

// (e) signed amplification tick per row: up when > +0.15, down when
// < -0.15, none in between (0.05 renders an empty cell)
ok(/amp !== null && amp > HIGH_SIGNAL \? '▲'/.test(ep), 'up tick when amplification > +HIGH_SIGNAL')
ok(/amp !== null && amp < -HIGH_SIGNAL \? '▼' : ''/.test(ep), 'down tick when amplification < -HIGH_SIGNAL; empty cell in between')

// (f) confNum in its OWN cell (the P3 type fix) — never mixed into the
// bar/affinity rendering
ok(/num\(p && p\.confidence\)/.test(ep), 'per-pattern confidence rendered in its own cell via num()')
ok(/title:\s*'confidence \(separate channel\)'/m.test(ep), 'confidence labelled as separate channel')
ok(!/prompt_affinity\s*\+\s*agent_affinity/.test(ep), 'prompt and agent affinity never blended')

// (g) neutral state: perception_available false OR zero filled
// entries -> today's 'no data yet' state, faint opacity 0.55, unchanged
ok(/'PERCEPTION · no data yet'/.test(ep), 'neutral no-data text kept')
ok(/opacity:\s*percOn \? 1 : 0\.55/.test(ep), 'absent section rendered faint (opacity 0.55), unchanged')
ok(/percOn\s*&&\s*patterns\.length\s*>\s*0/.test(ep), 'per-pattern grid only when available AND entries exist')

// (h) 96px strip output byte-identity snapshot. Re-baselined at the
// encoder-switcher change, then at the v1.9 icon-set pass, then at the
// portrait-engine integration: the Strip region now intentionally threads
// the shared portrait-template preference store (portraitPreferences) into
// PortraitView + the openWorkspace render. The guard still pins the EXACT
// Strip source so any unintended strip edit is caught — update this hash
// ONLY when the strip source is intentionally changed.
const strip = src.slice(src.indexOf('function Strip'), src.indexOf('export default {'))
assert.ok(strip.length > 0, 'Strip region found')
// Re-baselined at the Stage 4 measured-capacity / surface-composition
// pass: the strip now measures width AND height, selects bottom/sidebar
// from the measured box, renders PORTRAIT+PULSE coexisting modules,
// and consumes the v2 lane adapter (DRIFT stays expanded-only).
// Re-baselined again at the v1.9 panel-controls + pet-face pass: the
// strip now carries the in-panel EXP/ENC/mirror controls, the two-row
// narrow sidebar layout, the ticker overlay, and the two new affection
// pet faces (wonder / to-pet) with the patPet click mechanic.
// Re-baselined at v0.1.10 (review fixes 5/7/8/9): the strip now
// measures the PULSE module for capacity (chrome-free), passes
// surfaceBox + available into PulseLanes, splits the sidebar on an
// equal 1 1 0% flex basis, keeps the two-row header sidebar-only, and
// registers a SINGLE pane surface (no right-zone copy).
// Re-baselined at v0.1.10+2: the right-zone sidebar registration is
// restored (the user's working surface) and openWs captures the
// host close handle for the expanded view's collapse button.
// Re-baselined at v0.1.10+3 (Continuous Tool Activity): the Strip now
// threads the PULSE response's persisted tool_start batch into
// PulseLanes (activityEvents prop) and the PULSE queryFns ride the
// cursor suffix; the portrait canvas pass (offscreen morph buffer +
// particles) landed in the same window. Intentional strip edit.
// Re-baselined after the v13–v19 portrait brick-mosaic work: the Strip
// region now carries the J brick-mosaic canvas state (drift/dither
// mask, glitch band), the content-sized portrait module split, and
// the fill: true sidebar compact view. Intentional strip edit.
// Re-baselined at v0.1.11 (B, cursor hold-model): the Strip now threads
// the REST fetch handle into PulseLanes (rest prop) so it can issue
// targeted /pulse?tool_name=&after_event_id= flushes of a visible
// tool's unconsumed backlog (which lives in the DB, not renderer memory).
// Re-baselined at v0.1.12 (PULSE space pass): the I/O readout column now
// carries the lane-box chrome (1px border + 3px padding + 2px radius) so it
// suits the four bordered lane boxes. Intentional strip edit.
const STRIP_SHA_S16_MOTION = 'c4e9eb09294ceb2a06225bed0bbd5f4cf5512278ba88054c0559346480f084cf'
assert.equal(createHash('sha256').update(strip).digest('hex'), STRIP_SHA_S16_MOTION,
  '96px strip source byte-identical to the S16 motion-grammar baseline')
ok(true, 'strip byte-identity snapshot pinned')

// locked invariants (P3 subset, re-asserted where E2 touches the block)
ok(!/Math\.random/.test(ep), 'Perception render remains deterministic')
ok(!/#[0-9a-fA-F]{3,8}\b/.test(ep.replace(/var\(--[^)]+\)/g, '')), 'no hardcoded hex colors in the section')
ok(!/V_DANGER/.test(ep), 'no negative styling for absence (V_DANGER unused)')

console.log('PASS desktop_perception_expansion: ' + n + ' assertions')
