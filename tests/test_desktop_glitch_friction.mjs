// tests/test_desktop_glitch_friction.mjs — node test for the v0.1.26
// FRICTION-DRIVEN portrait glitch. Extracts the shared trigger (pure) from
// desktop/plugin.js and asserts:
//   1. the trigger starts "never pulsed" (at === 0) and iiyhFrictionPulse()
//      stamps a positive wall-clock timestamp;
//   2. the brick renderer's glitch is gated by the trigger (reads
//      IYH_FRICTION_TRIGGER.at), NOT by the retired periodic timer
//      (no `frame % (gEvery ...` cadence, no glitchEvery read in the draw fn);
//   3. the PULSE ingest effect stamps the trigger ONLY when a friction-lane
//      transition is generated, and only on the ANIMATED path (reduced motion
//      returns before the pulse).
// Run: node tests/test_desktop_glitch_friction.mjs
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const dir = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(dir, '..', 'desktop', 'plugin.js'), 'utf8')

let n = 0
const ok = (cond, label) => { assert.ok(cond, label); console.log('ok ' + (++n) + ' - ' + label) }

// ---- 1. the shared trigger (pure) -----------------------------------------
const a = src.indexOf('const IYH_FRICTION_TRIGGER = { at: 0 }')
const b = src.indexOf('function iiyhBrickCanvasDraw')
ok(a !== -1 && b !== -1 && b > a, 'IYH_FRICTION_TRIGGER block found before the canvas renderer')
const block = src.slice(a, b)
const mod = new Function(block + '\n;return { IYH_FRICTION_TRIGGER, iiyhFrictionPulse };')()
ok(mod.IYH_FRICTION_TRIGGER.at === 0, 'trigger starts never-pulsed (at === 0)')
mod.iiyhFrictionPulse()
ok(typeof mod.IYH_FRICTION_TRIGGER.at === 'number' && mod.IYH_FRICTION_TRIGGER.at > 0,
   'iiyhFrictionPulse() stamps a positive wall-clock timestamp')
mod.iiyhFrictionPulse()
ok(mod.IYH_FRICTION_TRIGGER.at > 0, 're-stamping keeps a positive timestamp')

// ---- 2. the renderer is friction-gated, the timer is retired --------------
ok(/const gActive = IYH_FRICTION_TRIGGER\.at > 0 && gAge >= 0 && gAge <= gWin/.test(src),
   'glitch active-state is derived from the friction trigger window')
ok(!/frame % \(gEvery/g.test(src), 'no periodic `frame % (gEvery ...)` cadence remains')
ok(!/IYH_J\.glitchEvery/g.test(src), 'renderer no longer reads IYH_J.glitchEvery (timer retired)')
ok(/const gIdx = Math\.floor\(IYH_FRICTION_TRIGGER\.at \/ 250\)/.test(src),
   'band seed derives from the trigger TIME (per-pulse, not a frame counter)')
ok(/glitchEvery: 90/.test(src), 'IYH_J.glitchEvery kept for schema continuity (marked retired)')

// ---- 3. the PULSE hook ------------------------------------------------------
// the pulse call lives in the ingest effect, AFTER the reduced-motion return.
const effStart = src.indexOf('coord.ingest(rawLanes, cap, { reducedMotion')
const effEnd = src.indexOf('active.forEach(scheduleArrival)', effStart)
const effect = src.slice(effStart, effEnd)
ok(/res\.immediate/.test(effect) && /setTick5/.test(effect), 'ingest effect handles the reduced-motion (immediate) path')
const immediateIdx = effect.indexOf('if (res.immediate)')
const pulseIdx = effect.indexOf('iiyhFrictionPulse()')
ok(immediateIdx !== -1 && pulseIdx !== -1 && pulseIdx > immediateIdx,
   'friction pulse fires only AFTER the reduced-motion return (animated path only)')
ok(/t\.metric === 'friction'/.test(effect), 'pulse fires only on a friction-lane transition')

console.log('PASS glitch-friction: ' + n + ' assertions')
