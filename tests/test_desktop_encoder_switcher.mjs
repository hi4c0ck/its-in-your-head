// tests/test_desktop_encoder_switcher.mjs — source-shape test for the
// embedding-encoder switcher (Sketch <-> MiniLM).
//
// Pins the user-facing labels, the POST /settings write wiring, the
// /engine-status readiness poll, and the Select SDK import so a renderer
// regression (e.g. a dropped import or a renamed option) is caught at the
// suite gate. Source-shape, not runtime: reads desktop/plugin.js and asserts
// on the wiring strings.
// Run: node tests/test_desktop_encoder_switcher.mjs
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const dir = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(dir, '..', 'desktop', 'plugin.js'), 'utf8')

let n = 0
const has = (needle, label) => {
  assert.ok(src.includes(needle), label + ' (missing: ' + needle + ')')
  console.log('ok ' + (++n) + ' - ' + label)
}
const not = (needle, label) => {
  assert.ok(!src.includes(needle), label + ' (stale: ' + needle + ')')
  console.log('ok ' + (++n) + ' - ' + label)
}

// 1. The two user-facing encoder names ship (never the raw tier ids as
//    labels). "Sketch" = fast offline tier-0, "MiniLM" = semantic tier-1.
has("label: 'Sketch'", "Sketch label for tier-0")
has("label: 'MiniLM'", "MiniLM label for tier-1-onnx")
has("{ id: 'tier-0'", "tier-0 option id wired")
has("{ id: 'tier-1-onnx'", "tier-1-onnx option id wired")

// 2. The write path: POST /settings carrying the dotted C4 key in a nested
//    body (the route accepts {"settings":{...}} or a flat {...}).
has("rest('/settings', { method: 'POST', body: { settings: { 'perception.encoder': id } }",
  'POST /settings writes the active encoder')

// 3. Tier-1 readiness is polled so a flip to MiniLM without the model can\'t
//    silently fall back to Sketch.
has("rest('/engine-status')", 'polls /engine-status for Tier-1 readiness')
has('st.onnx_model', 'reads onnx_model readiness from /engine-status')

// 4. The SDK Select primitives are all imported (a dropped import throws at
//    render — pin the group so the census stays green).
has('Select, SelectTrigger, SelectValue, SelectContent, SelectItem',
  'Select SDK primitives imported')

// 5. The switcher is rendered in the settings popover, wired to the settings
//    poll + refetch-on-apply; the stale read-only gear title is gone.
has('jsx(EncoderSwitcher, {', 'EncoderSwitcher rendered in the settings popover')
has("onApplied: () => { if (settingsQ) settingsQ.refetch() }", 'refetches settings after a flip')
not('read-only until /settings POST lands', 'stale read-only gear title removed')

// 6. The pure label/hint helpers exist (marker-safe, no react) so the
//    source-shape slice stays deterministic.
has('function encoderDisplayName(id)', 'encoderDisplayName helper present')
has('function encoderHint(id)', 'encoderHint helper present')

console.log('PASS encoder_switcher: ' + n + ' source-shape checks')
