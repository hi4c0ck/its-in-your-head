// tests/test_desktop_perception.mjs — source-shape assertions for the
// Perception section (D5) + confNum mean fix in desktop/plugin.js.
// Regex on CALL forms, never bare identifiers.
// Run: node tests/test_desktop_perception.mjs
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const dir = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(dir, '..', 'desktop', 'plugin.js'), 'utf8')

let n = 0
const ok = (cond, label) => { assert.ok(cond, label); console.log('ok ' + (++n) + ' - ' + label) }
const count = (s, re) => (s.match(re) || []).length

const ep = src.slice(src.indexOf('function ExpandedPortrait'), src.indexOf('function ExpandedDrift'))
assert.ok(ep.length > 0, 'ExpandedPortrait block found')

// 1) perception section renders from the key
ok(/data\s*&&\s*data\.perception/.test(ep), 'ExpandedPortrait reads data.perception key')
ok(/perception_available/.test(ep), 'ExpandedPortrait gates on perception_available')
ok(/Array\.isArray\(\s*perception\.patterns\s*\)/.test(ep), 'ExpandedPortrait reads perception.patterns as array')
ok(/dim_signals/.test(ep), 'ExpandedPortrait reads perception.dim_signals')
ok(/'PERCEPTION'/.test(ep), 'Perception section header rendered inside ExpandedPortrait')
ok(/percSection/.test(ep) && count(ep, /percSection/g) >= 2, 'percSection built and mounted in ExpandedPortrait children')

// strongest-pattern row: separate prompt/agent affinity, margin, amplification,
// repeated marker only when true, evidence always shown
ok(/strongP\.agent_affinity/.test(ep), 'strongest row renders agent_affinity')
ok(/strongP\.prompt_affinity/.test(ep), 'strongest row renders prompt_affinity (separate from agent)')
ok(/strongP\.margin/.test(ep), 'strongest row renders margin')
ok(/strongP\.amplification/.test(ep), 'strongest row renders amplification')
ok(/strongP\.repeated\s*===\s*true/.test(ep), 'repeated marker gated on repeated === true')
ok(/strongP\.evidence/.test(ep), 'strongest row renders evidence count')
ok(!/\(strongP\.prompt_affinity\s*\+\s*strongP\.agent_affinity|prompt_affinity\s*\+\s*agent_affinity/.test(ep), 'prompt and agent affinity never blended')

// per-dimension rows: prompt/agent signal separate, confidence its own channel
ok(/num\(sig\.agent\)/.test(ep), 'dim row renders agent signal')
ok(/num\(sig\.prompt\)/.test(ep), 'dim row renders prompt signal')
ok(/num\(data && data\.confidence && data\.confidence\[d\]\)/.test(ep), 'dim row renders per-dim confidence as separate channel')
ok(/title:\s*'confidence \(separate channel\)'/.test(ep), 'confidence labelled as separate channel')

// 2) neutral path on absent key or perception_available false
ok(/perception = data && data\.perception && typeof data\.perception === 'object'/.test(ep), 'absent key -> perception null (neutral)')
ok(/percOn = !!\(perception && perception\.perception_available\)/.test(ep), 'perception_available false -> percOn false')
ok(/'PERCEPTION · no data yet'/.test(ep), 'neutral faint fallback text on absent/unavailable')
ok(/opacity:\s*percOn \? 1 : 0\.55/.test(ep), 'absent section rendered faint (opacity 0.55), never negative')
ok(!/V_DANGER/.test(ep), 'Perception section never uses V_DANGER (no negative styling for absence)')

// 3) confNum mean computation in PortraitView
const pv = src.slice(src.indexOf('function PortraitView'), src.indexOf('function ExpandedPulse'))
ok(/const confVals =/.test(pv), 'PortraitView computes confVals')
ok(/for \(const d of dims\)/.test(pv), 'confVals iterates per-dim entries')
ok(/data\.confidence\[d\]/.test(pv), 'confVals reads per-dim confidence dict')
ok(/confVals\.reduce\(\(a, b\) => a \+ b, 0\) \/ confVals\.length/.test(pv), 'confNum is the MEAN of per-dim confidences')
ok(/confVals\.length > 0/.test(pv), 'all-None -> empty confVals -> dash fallback path kept')
ok(/confNum \? confNum \+ ' conf · ' : ''/.test(pv), 'readout dash fallback unchanged (confNum ? ... : empty)')
ok(/evidenceCount > 0 \? evidenceCount \+ ' snap' : 'evidence…'/.test(pv), 'evidence count readout unchanged')
ok(/' · ⛶ full'/.test(pv), 'expand hint unchanged')

// 4) locked invariants still hold (call forms)
ok(count(src, /new ResizeObserver\(/g) === 4, 'four ResizeObserver( calls: portrait fit + PulseLanes grid + two Strip composition reads (all mounted-surface measures, no per-frame loops)')
ok(src.includes('jsx(IyhPortraitCompact, { data, preferences, size: 48, fill: true })') && src.includes('jsx(IyhPortraitCompact, { data, preferences, size: 48 })'), 'compact PORTRAIT renders the IYH_PORTRAIT engine compact view (fill: true stretches the canvas in the tall sidebar; the 48px strip keeps the fixed square)')
ok(src.includes('jsx(IyhPortraitStudio, { data, preferences })'), 'expanded PORTRAIT renders the IYH_PORTRAIT engine studio view')
ok(!/Math\.random/.test(ep), 'Perception render is deterministic (no Math.random in ExpandedPortrait)')
ok(!/#[0-9a-fA-F]{3,8}\b/.test(ep.replace(/var\(--[^)]+\)/g, '')), 'Perception section has no hardcoded hex colors')

// 5) L-DISPLAY: the rolling-reference signal row (OOD drift + freshness)
ok(/perception\.ood_drift/.test(ep), 'OOD · REFERENCE row reads perception.ood_drift')
ok(/perception\.reference_freshness/.test(ep), 'OOD · REFERENCE row reads perception.reference_freshness')
ok(/'OOD · REFERENCE'/.test(ep), 'OOD · REFERENCE row header rendered')
ok(/num\(ood\)/.test(ep), 'OOD row renders the ood_drift value')
ok(/refLiveDims/.test(ep) && /refLive/.test(ep), 'freshness row shows live dims + live count')
ok(/refNewest/.test(ep), 'freshness row shows newest reassembly age')
ok(/children: refFresh \?/.test(ep), 'freshness row gates on refFresh (dash fallback when absent)')

console.log('PASS desktop_perception: ' + n + ' assertions')
