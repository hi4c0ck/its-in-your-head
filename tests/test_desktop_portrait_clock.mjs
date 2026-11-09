// Desktop pane clock test: the pane-owned deterministic portrait clock
// (IYH-PORTCLOCK block extracted from desktop/plugin.js).
//
// Contract:
//  - first sighting of a data object -> phase 0.0 (parts at rest; pane open
//    returns to neutral, zero persisted live state)
//  - each FRESH poll result (new object identity) -> exactly one +0.85s step
//  - repeated re-renders of the SAME data object never advance the clock
//  - each rendering (compact / studio) tracks an independent clock
//  - fully deterministic: same poll sequence -> same phase sequence
//  - the pane feeds this phase as options.clock into buildPortraitSvg;
//    the engine's part traversal is a pure function of it.

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const dir = fileURLToPath(new URL('..', import.meta.url))
const src = readFileSync(`${dir}/desktop/plugin.js`, 'utf8')

const start = src.indexOf('// IYH-PORTCLOCK-START')
const end = src.indexOf('// IYH-PORTCLOCK-END')
assert.ok(start >= 0 && end > start, 'IYH-PORTCLOCK block found in plugin.js')
const block = src.slice(start, end + '// IYH-PORTCLOCK-END'.length)

// Evaluate the block with the constant it closes over (declared later in the
// module; render-time calls see the real value).
const ctx = vm.createContext({ Map, PORTRAIT_POLL_MS: 850, console, assert })
vm.runInContext(block, ctx, { filename: 'portclock.mjs' })

let n = 0
function ok(cond, label) { n++; if (!cond) { console.error('FAIL:', label); process.exit(1) } }

// 1. first sighting -> phase 0 (rest invariance at pane open)
ok(vm.runInContext('iyhPortraitClockFor("compact", {})', ctx) === 0,
   'first data sighting is phase 0.0')

// 2. fresh poll result -> +0.85s exactly
ok(vm.runInContext('iyhPortraitClockFor("compact", {})', ctx) === 0.85,
   'second fresh poll advances exactly one 0.85s step')
ok(vm.runInContext('iyhPortraitClockFor("compact", {})', ctx) === 1.7,
   'third fresh poll advances to 1.7s')

// 3. same data object re-rendered does NOT advance
vm.runInContext('globalThis.__d = {}', ctx)
const same1 = vm.runInContext('iyhPortraitClockFor("studio", globalThis.__d)', ctx)
const same2 = vm.runInContext('iyhPortraitClockFor("studio", globalThis.__d)', ctx)
const same3 = vm.runInContext('iyhPortraitClockFor("studio", globalThis.__d)', ctx)
ok(same1 === 0 && same2 === 0 && same3 === 0,
   're-renders of the same data object never advance the clock')

// 4. studio and compact track independent clocks
ok(vm.runInContext('iyhPortraitClockFor("studio", {})', ctx) === 0.85,
   'studio advances on its own fresh poll')
ok(vm.runInContext('iyhPortraitClockFor("compact", {})', ctx) === 2.55,
   'compact clock is independent of studio')

// 5. determinism: reset the module state, replay the same sequence
vm.runInContext('IYH_PORTRAIT_CLOCKS.clear()', ctx)
const seq = []
for (let i = 0; i < 4; i++) {
  seq.push(vm.runInContext('iyhPortraitClockFor("compact", {})', ctx))
}
vm.runInContext('IYH_PORTRAIT_CLOCKS.clear()', ctx)
const seq2 = []
for (let i = 0; i < 4; i++) {
  seq2.push(vm.runInContext('iyhPortraitClockFor("compact", {})', ctx))
}
ok(JSON.stringify(seq) === JSON.stringify(seq2), 'same poll sequence -> same phase sequence')
ok(JSON.stringify(seq) === JSON.stringify([0, 0.85, 1.7, 2.55]), 'expected sequence')

// 6. the builder actually threads the clock into buildPortraitSvg options
const buildSrc = src.slice(src.indexOf('function buildIyhPortraitMarkup'),
                          src.indexOf('// Inline SVG has no intrinsic'))
ok(/iyhPortraitClockFor\(rendering, data\)/.test(buildSrc),
   'buildIyhPortraitMarkup passes clock: iyhPortraitClockFor(rendering, data)')

console.log(`OK: ${n} pane clock checks passed`)
