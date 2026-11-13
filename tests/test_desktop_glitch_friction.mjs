// tests/test_desktop_glitch_friction.mjs — node test for the v0.1.27
// UPSET-DRIVEN portrait glitch (D1-D6). Extracts the shared trigger +
// upset-state blocks (pure) from desktop/plugin.js and asserts:
//   1. the v0.1.26 friction trigger still starts "never pulsed" and
//      iiyhFrictionPulse() stamps a positive wall-clock timestamp;
//   2. the NEW sustained upset state (IYH_UPSET_STATE / iiyhIngestUpset):
//      neutral at start, a tier-2 block turns it ON (value clamped to
//      [0,1], tier quantized to 0/1/2), a neutral/absent block keeps it
//      OFF; mood (M, D6) rides along only when a finite number;
//   3. the brick renderer's glitch gate is `pulseActive || upSustained`
//      (the v0.1.26 landing window RETAINED, the D4 sustained band added)
//      with the sustained band seeded from a ROLLING seed and the
//      intensity proportional to F' (gGain = 0.55 + 0.45 * upInt);
//   4. the /pulse ingest hook updates the upset state on every read
//      (iiyhIngestUpset(d && d.upset));
//   5. the PULSE ingest effect still stamps the friction trigger ONLY on
//      a friction-lane transition, only on the ANIMATED path.
// Run: node tests/test_desktop_glitch_friction.mjs
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const dir = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(dir, '..', 'desktop', 'plugin.js'), 'utf8')

let n = 0
const ok = (cond, label) => { assert.ok(cond, label); console.log('ok ' + (++n) + ' - ' + label) }

// ---- 1. the shared triggers (pure) ----------------------------------------
const a = src.indexOf('const IYH_FRICTION_TRIGGER = { at: 0 }')
const b = src.indexOf('function iiyhBrickCanvasDraw')
ok(a !== -1 && b !== -1 && b > a, 'trigger block found before the canvas renderer')
const block = src.slice(a, b)
const mod = new Function(
  block + '\n;return { IYH_FRICTION_TRIGGER, iiyhFrictionPulse, ' +
  'IYH_UPSET_STATE, iiyhIngestUpset, IYH_UPSET_GLITCH_TIER };')()
ok(mod.IYH_FRICTION_TRIGGER.at === 0, 'friction trigger starts never-pulsed (at === 0)')
mod.iiyhFrictionPulse()
ok(typeof mod.IYH_FRICTION_TRIGGER.at === 'number' && mod.IYH_FRICTION_TRIGGER.at > 0,
   'iiyhFrictionPulse() stamps a positive wall-clock timestamp')
mod.iiyhFrictionPulse()
ok(mod.IYH_FRICTION_TRIGGER.at > 0, 're-stamping keeps a positive timestamp')
ok(mod.IYH_UPSET_GLITCH_TIER === 0.65,
   'the D4 glitch tier constant mirrors the backend (pulse/upset.py)')

// ---- 2. the sustained upset state (D1-D6) ----------------------------------
ok(mod.IYH_UPSET_STATE.value === 0 && mod.IYH_UPSET_STATE.tier === 0 &&
   mod.IYH_UPSET_STATE.mood === null,
   'upset state starts neutral (no evidence, never invented)')
mod.iiyhIngestUpset(null)
mod.iiyhIngestUpset({})
ok(mod.IYH_UPSET_STATE.tier === 0, 'a null/empty upset block keeps the state neutral')
mod.iiyhIngestUpset({ value: 0.4, tier: 1, upset_turns: 2, mood: 0.6,
                      prognosis: null })
ok(mod.IYH_UPSET_STATE.value === 0.4 && mod.IYH_UPSET_STATE.tier === 1,
   'a lane-tier block records value + tier')
ok(mod.IYH_UPSET_STATE.mood === 0.6, 'the slow mood M rides along when finite')
mod.iiyhIngestUpset({ value: 1.5, tier: 2, mood: 'nope' })
ok(mod.IYH_UPSET_STATE.value === 1, 'F\' is clamped into [0, 1]')
ok(mod.IYH_UPSET_STATE.tier === 2, 'a tier-2 block turns the sustained state ON')
ok(mod.IYH_UPSET_STATE.mood === 0.6, 'a non-finite mood is ignored (keeps the last value)')
mod.iiyhIngestUpset({ value: 0.2, tier: 0, mood: 0.8 })
ok(mod.IYH_UPSET_STATE.tier === 0 && mod.IYH_UPSET_STATE.mood === 0.8,
   'a decayed block turns the state OFF and updates M')

// ---- 3. the renderer is gated by pulse OR sustained-upset -----------------
ok(/const pulseActive = IYH_FRICTION_TRIGGER\.at > 0 && gAge >= 0 && gAge <= gWin/.test(src),
   'the v0.1.26 landing-window gate is RETAINED (pulseActive)')
ok(/const upSustained = IYH_UPSET_STATE\.tier >= 2 && upVal >= IYH_UPSET_GLITCH_TIER/.test(src),
   'the D4 sustained gate: tier >= 2 AND value at/above the tier constant')
ok(/const gActive = pulseActive \|\| upSustained/.test(src),
   'the renderer glitches on the landing window OR the sustained upset')
ok(/gGain = 0\.55 \+ 0\.45 \* upInt/.test(src),
   'the sustained band intensity is proportional to F\' (D4)')
ok(/Math\.floor\(gNow \/ 250\)/.test(src),
   'the sustained band uses a ROLLING seed (the ripple keeps moving)')
ok(/Math\.floor\(IYH_FRICTION_TRIGGER\.at \/ 250\)/.test(src),
   'the pulse band still seeds from the trigger TIME (per-pulse, stable)')
ok(!/frame % \(gEvery/g.test(src), 'no periodic `frame % (gEvery ...)` cadence remains')
ok(!/IYH_J\.glitchEvery/g.test(src), 'renderer no longer reads IYH_J.glitchEvery (timer retired)')
ok(/glitchEvery: 90/.test(src), 'IYH_J.glitchEvery kept for schema continuity (marked retired)')

// ---- 4. the /pulse ingest hook ---------------------------------------------
const hooks = src.split('iiyhIngestUpset(d && d.upset)')
ok(hooks.length >= 2, 'both /pulse query hooks ingest the upset block')
ok(/v0\.1\.27: D1-D6/.test(src), 'the ingest hook is marked v0.1.27')

// ---- 5. the PULSE friction hook (v0.1.26, retained) ------------------------
const effStart = src.indexOf('coord.ingest(rawLanes, cap, { reducedMotion')
const effEnd = src.indexOf('active.forEach(scheduleArrival)', effStart)
const effect = src.slice(effStart, effEnd)
ok(/res\.immediate/.test(effect) && /setTick5/.test(effect), 'ingest effect handles the reduced-motion (immediate) path')
const immediateIdx = effect.indexOf('if (res.immediate)')
const pulseIdx = effect.indexOf('iiyhFrictionPulse()')
ok(immediateIdx !== -1 && pulseIdx !== -1 && pulseIdx > immediateIdx,
   'friction pulse fires only AFTER the reduced-motion return (animated path only)')
ok(/t\.metric === 'friction'/.test(effect), 'pulse fires only on a friction-lane transition')

console.log('PASS glitch-friction (v0.1.27 upset-driven): ' + n + ' assertions')
