"""Generate tools/portrait_motion_tuner.html — a self-contained, single-viewport
slider tuner for the IYH portrait motion-grammar layer.

Inlines the IYH-PORTRAIT-ENGINE block from desktop/plugin.js byte-for-byte,
so whatever you tune in the browser is what the production pane renders.
Regenerate after engine changes:  python tools/build_motion_tuner.py

Design (owner-locked):
  * COMPACT single-viewport page — no page scroll; only the control column
    scrolls when the window is short.
  * CROP-to-fit main stage (the face fills the pane, motion is catchable)
    plus exact-size 48/96px dock previews.
  * TIME dimension: STEP / PLAY advance the target at the production 0.85s
    poll cadence with a deterministic script around the slider bases, so
    gaze saccades actually happen as visible moments (the pane swaps the
    SVG exactly like production — no CSS transitions, no random jitter).
  * Saccade log: gaze landings + the per-target cloud (center ± width) so
    the 15px moments are catchable numerically, not just by staring.
"""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
START = "// IYH-PORTRAIT-ENGINE-START"
END = "// IYH-PORTRAIT-ENGINE-END"
s = SRC.index(START)
e = SRC.index(END)
engine_block = SRC[s + len(START): e]
sha = hashlib.sha256(engine_block.encode("utf-8")).hexdigest()[:12]

HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IYH portrait motion tuner</title>
<style>
/* Standalone file: fixed dark chrome, paper portrait panes. In the Hermes
   preview the frame injects theme vars before these rules. */
:root {
  --fg: var(--foreground, #e8e6e0);
  --muted: var(--muted-foreground, #9a958c);
  --border: var(--border, #2a2d33);
  --accent: var(--accent, #d8c9a3);
  --card: var(--card, #14161a);
  --paper: #ebe5db;
}
* { box-sizing: border-box }
html, body { height: 100%; margin: 0; background: var(--card) }
body { color: var(--fg); font: 12px/1.45 system-ui, "Segoe UI", sans-serif;
       display: flex; flex-direction: column }
/* compact: no scroll at normal pane heights; only scrolls when it truly
   overflows (very short windows). */

/* ---- top bar ---- */
.top { flex: none; display: flex; align-items: center; gap: 10px;
       padding: 7px 14px; border-bottom: 1px solid var(--border) }
.top .ttl { font-size: 11px; font-weight: 600; letter-spacing: 1.5px }
.top .sha { font-size: 9px; color: var(--muted); letter-spacing: .5px }
.top .sp { flex: 1 }
.btn { font: inherit; font-size: 10px; letter-spacing: .5px; color: var(--fg);
       background: transparent; border: 1px solid var(--border);
       border-radius: 6px; padding: 4px 10px; cursor: pointer }
.btn:hover { border-color: var(--accent) }
.btn.on { color: var(--accent); border-color: var(--accent) }
.btn.play { min-width: 74px }
.tabs { display: flex; gap: 5px }
.tabs .btn.on { color: var(--fg) }

/* ---- main layout: stage left, controls right ---- */
main { flex: 1; min-height: 0; display: flex; gap: 14px; padding: 12px 14px }
.stagecol { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column;
            gap: 8px; min-height: 0 }
.pvrow { display: flex; gap: 12px; align-items: flex-start; min-height: 0 }
.cropbox { flex: none; width: min(46dvh, 430px); height: min(46dvh, 430px);
           position: relative; overflow: hidden; background: var(--paper);
           border-radius: 10px; border: 1px solid var(--border) }
.cropbox .xform { position: absolute; inset: 0; transform-origin: 50% 50% }
.cropbox.zoom .xform { transform: scale(2) }       /* face zoom: crop-to-fit on the face */
.cropbox.fit .xform  { transform: scale(1) }       /* full portrait */
.cropbox svg { position: absolute; inset: 0; width: 100%; height: 100% }
.cap { font-size: 9px; color: var(--muted); letter-spacing: 1px;
       text-align: center; margin-top: 4px }
.minis { flex: none; display: flex; flex-direction: column; gap: 10px }
.mini { background: var(--paper); border: 1px solid var(--border);
        border-radius: 8px; display: flex; flex-direction: column; align-items: center }
.mini .box { display: grid; place-items: center }
.mini .b48 { width: 48px; height: 48px }
.mini .b96 { width: 96px; height: 96px }
.mini svg { display: block }
.mini .cap { margin: 4px 0 0; padding: 0 8px 5px }

/* ---- saccade log + readout ---- */
.logbox { flex: none; border: 1px solid var(--border); border-radius: 8px;
          padding: 6px 10px; background: var(--card) }
.logbox .lg { font-size: 9px; color: var(--muted); letter-spacing: 1px;
              display: flex; gap: 14px; margin-bottom: 2px }
.logbox .lg b { color: var(--accent); font-weight: 600 }
.logbox svg { display: block; width: 100%; height: 52px }
.readrow { flex: none; display: flex; flex-wrap: wrap; gap: 4px 14px;
           font-size: 10px; color: var(--muted); font-variant-numeric: tabular-nums;
           padding: 0 2px }
.readrow .k { letter-spacing: .4px }
.readrow .v { color: var(--fg) }
.readrow .v.big { color: var(--accent); font-size: 12px; font-weight: 600 }

/* ---- control column ---- */
.ctrlcol { flex: 0 0 302px; overflow-y: auto; min-height: 0; padding-right: 2px }
.group { border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px;
         margin-bottom: 10px; background: var(--card) }
.group h2 { font-size: 9px; letter-spacing: 1.5px; color: var(--accent);
            margin: 0 0 6px; font-weight: 600 }
.row { display: flex; align-items: center; gap: 8px; margin: 4px 0; font-size: 10px }
.row label { flex: 0 0 96px; color: var(--muted); letter-spacing: .4px }
.row input[type=range] { flex: 1; accent-color: var(--accent); height: 13px }
.row .val { flex: 0 0 38px; text-align: right; font-variant-numeric: tabular-nums }
.row select { flex: 1; background: var(--card); color: var(--fg);
              border: 1px solid var(--border); border-radius: 4px; padding: 2px 4px;
              font-size: 10px }
.row input[type=checkbox] { accent-color: var(--accent) }
.presets { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 10px }
.presets button { font: inherit; font-size: 9px; letter-spacing: .4px; color: var(--fg);
                  background: var(--card); border: 1px solid var(--border);
                  border-radius: 99px; padding: 3px 9px; cursor: pointer }
.presets button:hover { border-color: var(--accent) }
details.baseline { border: 1px solid var(--border); border-radius: 8px;
                   padding: 6px 10px; margin-bottom: 10px; background: var(--card) }
details.baseline summary { font-size: 9px; letter-spacing: 1.5px; color: var(--muted);
                           cursor: pointer; padding: 2px 0 }
.note { font-size: 9px; color: var(--muted); letter-spacing: .3px; margin-top: 5px }
.floor-warn { color: #c9a86a; font-size: 9px; letter-spacing: .5px; margin-top: 4px;
              display: none }
</style>
</head>
<body>
<div class="top">
  <span class="ttl">IYH PORTRAIT · MOTION GRAMMAR TUNER</span>
  <span class="sha">engine sha __SHA__ · deterministic · 0.85s poll cadence</span>
  <span class="sp"></span>
  <button class="btn" id="b-reset" title="back to tick 0">t = 0</button>
  <button class="btn" id="b-step">STEP &#9654;</button>
  <button class="btn play" id="b-play">PLAY</button>
  <button class="btn on" id="b-zoom">FACE ZOOM</button>
  <button class="btn on" id="b-mark" title="crosshair rings on the pupils — saccade readout">MARKS</button>
  <span class="tabs" id="tabs"></span>
</div>

<main>
  <div class="stagecol">
    <div class="pvrow">
      <div>
        <div class="cropbox zoom" id="cropbox">
          <div class="xform" id="xform"></div>
        </div>
        <div class="cap" id="cropcap">STUDIO · FACE ZOOM (crop-to-fit)</div>
      </div>
      <div class="minis">
        <div class="mini"><div class="box b48" id="mini48"></div>
          <div class="cap">DOCK 48PX</div></div>
        <div class="mini"><div class="box b96" id="mini96"></div>
          <div class="cap">DOCK 96PX</div></div>
      </div>
      <div class="cap" id="vis48" style="margin-top:6px">48PX VISIBILITY — measuring…</div>
    </div>

    <div class="logbox">
      <div class="lg"><span>SACCADE LOG — gaze landings (<b id="lg-gaze">–</b> now) ·
      cloud band = this target's territory · t = <b id="lg-t">0</b></span></div>
      <svg id="logsvg" viewBox="0 0 600 52" preserveAspectRatio="none"></svg>
    </div>

    <div class="readrow" id="readrow"></div>
  </div>

  <div class="ctrlcol">
    <div class="presets" id="presets"></div>

    <div class="group">
      <h2>LIVE EXPRESSION (transient gesture)</h2>
      <div class="row"><label>stage</label><select id="f-stage">
        <option value="none">none (rest)</option>
        <option value="prompt">prompt (stage 1)</option>
        <option value="paired" selected>paired (stage 2)</option>
      </select></div>
      <div class="row"><label>openness</label><input type="range" id="f-openness" min="-1" max="1" step="0.01" value="0"><span class="val"></span></div>
      <div class="row"><label>focus</label><input type="range" id="f-focus" min="-1" max="1" step="0.01" value="0"><span class="val"></span></div>
      <div class="row"><label>resonance</label><input type="range" id="f-resonance" min="0" max="1" step="0.01" value="0.5"><span class="val"></span></div>
      <div class="row"><label>tension</label><input type="range" id="f-tension" min="0" max="1" step="0.01" value="0.5"><span class="val"></span></div>
      <div class="row"><label>intensity</label><input type="range" id="f-intensity" min="0" max="1" step="0.01" value="0.6"><span class="val"></span></div>
      <div class="row"><label>confidence</label><input type="range" id="f-confidence" min="0" max="1" step="0.01" value="0.6"><span class="val"></span></div>
      <div class="note">STEP/PLAY drives a deterministic script around these base values —
      each tick is a new expression target: the gaze re-saccades, the territory breathes.</div>
    </div>

    <div class="group">
      <h2>MORPHOLOGY (learned pair mutation)</h2>
      <div class="row"><label>mutation active</label><input type="checkbox" id="m-active"></div>
      <div class="row"><label>pair</label><select id="m-pair">
        <option value="cr,ad">context_richness + analytical_depth</option>
        <option value="ex,sc">exploration + scope</option>
        <option value="di,it">directness + iteration</option>
        <option value="x,y">unmapped (generic motif)</option>
      </select></div>
      <div class="row"><label>strength</label><input type="range" id="m-strength" min="0" max="0.6" step="0.01" value="0"><span class="val"></span></div>
      <div class="row"><label>line_density</label><input type="range" id="m-ld" min="0" max="0.6" step="0.01" value="0"><span class="val"></span></div>
      <div class="row"><label>contour_asym</label><input type="range" id="m-ca" min="-0.6" max="0.6" step="0.01" value="0"><span class="val"></span></div>
      <div class="row"><label>echo_strength</label><input type="range" id="m-es" min="0" max="0.6" step="0.01" value="0"><span class="val"></span></div>
      <div class="row"><label>gesture_bias</label><input type="range" id="m-gb" min="-0.6" max="0.6" step="0.01" value="0"><span class="val"></span></div>
      <div class="floor-warn" id="m-warn">strength ≤ 0.02 active floor → mutation inert (neutral wins)</div>
    </div>

    <details class="baseline">
      <summary>PERSISTENT DIMENSIONS (baseline profile)</summary>
      <div class="row"><label>exploration</label><input type="range" id="d-exploration" min="0" max="1" step="0.01" value="0.40"><span class="val"></span></div>
      <div class="row"><label>directness</label><input type="range" id="d-directness" min="0" max="1" step="0.01" value="0.75"><span class="val"></span></div>
      <div class="row"><label>analytical_depth</label><input type="range" id="d-analytical_depth" min="0" max="1" step="0.01" value="0.55"><span class="val"></span></div>
      <div class="row"><label>iteration</label><input type="range" id="d-iteration" min="0" max="1" step="0.01" value="0.30"><span class="val"></span></div>
      <div class="row"><label>scope</label><input type="range" id="d-scope" min="0" max="1" step="0.01" value="0.65"><span class="val"></span></div>
      <div class="row"><label>context_richness</label><input type="range" id="d-context_richness" min="0" max="1" step="0.01" value="0.60"><span class="val"></span></div>
      <div class="note">the stable six-dimension profile the live layer rides on</div>
    </details>
  </div>
</main>

<script>
// ---- production engine, inlined verbatim from desktop/plugin.js ----
__ENGINE__
// ---- harness stubs the engine block expects at module scope ----
const useState = () => [null, () => {}], useEffect = () => {};
const jsx = () => null, jsxs = () => null;
const clamp01 = (v) => (typeof v === 'number' && Number.isFinite(v)
  ? Math.min(1, Math.max(0, v)) : null);

// ---- tuner UI: compact, time-driven, deterministic ----
const IYH = IYH_PORTRAIT;
const DIMS = IYH.PORTRAIT_DIMENSIONS;
const $ = (id) => document.getElementById(id);
const clamp1 = (v) => Math.max(-1, Math.min(1, v));
const clamp01b = (v) => Math.max(0, Math.min(1, v));
const TEMPLATES = IYH.listPortraitTemplates();
let template = IYH.DEFAULT_PORTRAIT_TEMPLATE;

// TIME state. STEP/PLAY advance `tick` at the production poll cadence
// (0.85s). Every tick is a new expression target: updated_at advances and
// a deterministic script wobbles the slider bases, so the gaze saccade
// re-rolls and the territory breathes — exactly like real turns.
// No Math.random anywhere: identical base sliders + identical tick
// sequence -> identical renders.
let tick = 0;
const visCache = Object.create(null); // template → 48px visibility report
let playId = null;
const POLL_MS = 850;
const script = (k) => ({
  openness: clamp1((+$('f-openness').value) + 0.22 * Math.sin(k * 0.90)
             + 0.07 * Math.sin(k * 2.31 + 0.4)),
  focus: clamp1((+$('f-focus').value) + 0.18 * Math.sin(k * 0.63 + 1.3)),
  resonance: clamp01b((+$('f-resonance').value) + 0.10 * Math.sin(k * 0.47 + 2.1)),
  tension: clamp01b((+$('f-tension').value) + 0.08 * Math.sin(k * 0.53 + 0.7)),
  intensity: clamp01b((+$('f-intensity').value) + 0.15 * Math.sin(k * 0.41 + 2.9)),
});

function payload(k) {
  const user = {};
  for (const d of DIMS) user[d] = parseFloat($('d-' + d).value);
  const data = { available: true,
                 dimensions: Object.fromEntries(DIMS.map((d) => [d, 0.5])),
                 user, agent: null,
                 confidence: Object.fromEntries(DIMS.map((d) => [d, 0.6])) };
  const stage = $('f-stage').value;
  if (stage !== 'none') {
    const s = script(k);
    data.live_expression = {
      stage, updated_at: +(1000 + k * 0.85).toFixed(2),
      openness: s.openness, focus: s.focus,
      resonance: s.resonance, tension: s.tension,
      intensity: s.intensity, confidence: +$('f-confidence').value,
    };
  }
  if ($('m-active').checked) {
    const pair = $('m-pair').value.split(',');
    data.morphology = {
      available: true, pair, evidence: 50, confidence: 0.7,
      strength: +$('m-strength').value,
      line_density: +$('m-ld').value, contour_asymmetry: +$('m-ca').value,
      echo_strength: +$('m-es').value, gesture_bias: +$('m-gb').value,
    };
  }
  return data;
}

// ---- saccade log: last N targets — landings + their cloud band ----
const logEntries = [];
const LOG_N = 48;
function pushLog(motion) {
  logEntries.push({ k: tick, gaze: motion.gazeDX, cloud: motion.gazeTerritory,
                    center: motion.gazeCenter });
  if (logEntries.length > LOG_N) logEntries.shift();
}
function drawLog() {
  const svg = $('logsvg');
  const W = 600, H = 52, Y0 = H / 2, SCALE = 1.5; // units -> px
  const n = logEntries.length;
  let out = '';
  const X = (i) => 8 + (W - 16) * (n <= 1 ? 0.5 : i / (n - 1));
  if (n > 1) {
    // cloud band: center ± width per target
    let top = '', bot = '';
    for (let i = 0; i < n; i++) {
      const e = logEntries[i];
      const c = Y0 - e.center * SCALE;
      const hw = Math.max(0.5, e.cloud * SCALE);
      top += (i ? 'L' : 'M') + X(i).toFixed(1) + ',' + (c - hw).toFixed(1);
      const j = n - 1 - i;
      const ej = logEntries[j];
      bot += (i ? 'L' : 'M') + X(j).toFixed(1) + ',' +
             (Y0 - ej.center * SCALE + Math.max(0.5, ej.cloud * SCALE)).toFixed(1);
    }
    out += '<path d="' + top + ' ' + bot + ' Z" fill="rgba(216,201,163,0.14)"/>';
    // cloud center line
    out += '<path d="' + logEntries.map((e, i) =>
      (i ? 'L' : 'M') + X(i).toFixed(1) + ',' + (Y0 - e.center * SCALE).toFixed(1)
    ).join(' ') + '" fill="none" stroke="rgba(154,149,140,0.55)" stroke-width="1"/>';
    // landing line (gaze saccades — the moments are the spikes)
    out += '<path d="' + logEntries.map((e, i) =>
      (i ? 'L' : 'M') + X(i).toFixed(1) + ',' +
      (Y0 - Math.max(-30, Math.min(30, e.gaze)) * SCALE).toFixed(1)
    ).join(' ') + '" fill="none" stroke="var(--accent)" stroke-width="1.4"/>';
  }
  out += '<line x1="8" x2="' + (W - 8) + '" y1="' + Y0 + '" y2="' + Y0 +
         '" stroke="rgba(154,149,140,0.35)" stroke-dasharray="3 4"/>';
  if (n > 0) {
    const e = logEntries[n - 1];
    out += '<circle cx="' + X(n - 1) + '" cy="' +
           (Y0 - Math.max(-30, Math.min(30, e.gaze)) * SCALE).toFixed(1) +
           '" r="2.6" fill="var(--accent)"/>';
  }
  svg.innerHTML = out;
}

function render() {
  const data = payload(tick);
  const model = IYH.buildPortraitModel(data);
  // Template-owned movement contract: the deltas this template actually
  // renders with (feature-relative travel, region-clamped).
  const m = IYH.movementFor(model, template);

  // Main stage: crop-to-fit the studio portrait, then zoom the face.
  // Gaze markers are drawn INSIDE the svg (viewBox units) so the slice
  // crop and the face zoom apply to them identically — pixel-perfect on
  // the pupils, where each saccade lands.
  const xform = $('xform');
  xform.innerHTML = IYH.buildPortraitSvg(data,
    { template, rendering: 'studio', id: 'tuner-crop' });
  const svgEl = xform.querySelector('svg');
  svgEl.setAttribute('preserveAspectRatio', 'xMidYMid slice');
  if ($('b-mark').classList.contains('on')) {
    const NS = 'http://www.w3.org/2000/svg';
    const g = model.expression.gazeX + m.gazeDX; // baseline + saccade
    const gy = (model.expression.gazeY + m.gazeDY) * 0.4; // vertical baseline + saccade
    // near eye only: the marker rides the SAME head-group transform as the
    // rendered pupil, so it stays concentric at every headTilt.
    for (const [cx, cy] of [[309 + g, 367 + gy]]) {
      // The marker rides the SAME nested head transforms as the rendered
      // pupil of the active template (each template tilts the head a
      // different way):
      // soft-flow-study: its legacy face maps gaze through different offsets
      // (gazeOffsetX/-26 base, half saccade gain) — the studio marker would
      // mislead; skip it there. Saccade log + readout still carry the data.
      if (template === 'soft-flow-study') break;
      const eT = model.expression.headTilt;
      const cT = eT + m.headTilt;
      const markerT = 'rotate(' + (-2.2 + eT * 0.12).toFixed(2) + ' 360 400) rotate('
        + (-4.2 + cT * 0.62).toFixed(2) + ' 360 360)';
      const mk = document.createElementNS(NS, 'g');
      mk.setAttribute('stroke', 'rgba(122,96,58,.95)');
      mk.setAttribute('fill', 'none');
      mk.setAttribute('transform', markerT);
      mk.innerHTML =
        '<circle cx="' + cx + '" cy="' + cy + '" r="13" stroke-width="2.5"/>' +
        '<line x1="' + (cx - 22) + '" y1="' + cy + '" x2="' + (cx + 22) +
        '" y2="' + cy + '" stroke-width="1.6"/>' +
        '<line x1="' + cx + '" y1="' + (cy - 22) + '" x2="' + cx +
        '" y2="' + (cy + 22) + '" stroke-width="1.6"/>';
      svgEl.appendChild(mk);
    }
  }
  $('mini48').innerHTML = IYH.buildPortraitSvg(data,
    { template, rendering: 'compact', id: 'tuner-m48' });
  $('mini96').innerHTML = IYH.buildPortraitSvg(data,
    { template, rendering: 'compact', id: 'tuner-m96' });

  // 48px visibility measurement (deterministic; cached per template).
  visCache[template] = visCache[template] || IYH.visibilityReport(template);
  const rep = visCache[template];
  const f2 = (v) => (v == null ? '–' : Number(v).toFixed(2));
  $('vis48').innerHTML = rep.minVisibleAt48 > 0
    ? '<span class="k">48PX VISIBILITY</span> <span class="v big">'
      + (rep.pass ? 'PASS' : 'FAIL') + '</span> <span class="v">worst gated step '
      + f2(rep.worst && rep.worst.coordinatedPx) + 'px (min '
      + (rep.minVisibleCoordinated48 || rep.minVisibleAt48) + ') · full travel '
      + f2(rep.fullTravel && rep.fullTravel.coordinatedPx) + 'px</span>'
    : '<span class="k">48PX VISIBILITY</span> <span class="v">legacy travel · full '
      + f2(rep.fullTravel && rep.fullTravel.coordinatedPx) + 'px (reported only)</span>';

  pushLog(m);
  drawLog();

  // compact numeric readout (1u = 0.667px @480 studio, ~1.15px in the crop pane)
  const px = (u) => (u * 1.15).toFixed(1);
  $('readrow').innerHTML =
    '<span class="k">stage <span class="v">' + (m.stage || 'rest') + '</span></span>' +
    '<span class="k">gaze <span class="v big">' + px(m.gazeDX) + 'px</span>' +
    ' <span class="v">' + m.gazeDX.toFixed(1) + 'u</span></span>' +
    '<span class="k">cloud <span class="v">' + px(m.gazeTerritory * 2) + 'px wide</span></span>' +
    '<span class="k">head <span class="v">' + m.headTilt.toFixed(1) + '°</span></span>' +
    '<span class="k">brow <span class="v">' + px(m.brow) + 'px</span></span>' +
    '<span class="k">aperture <span class="v">' + px(m.eyeOpen * 20) + 'px</span></span>' +
    '<span class="k">mouth <span class="v">' + px(Math.abs(m.mouthLift) + Math.abs(m.mouthTension)) + 'px</span></span>' +
    '<span class="k">pressure <span class="v">' + m.pressure.toFixed(2) + '</span></span>' +
    '<span class="k">continuity <span class="v">' + m.continuity.toFixed(2) + '</span></span>';
  $('lg-gaze').textContent = px(m.gazeDX) + 'px';
  $('lg-t').textContent = String(tick);
  for (const id of ['f-openness','f-focus','f-resonance','f-tension','f-intensity',
                    'f-confidence','m-strength','m-ld','m-ca','m-es','m-gb',
                    'd-exploration','d-directness','d-analytical_depth',
                    'd-iteration','d-scope','d-context_richness']) {
    const el = $(id);
    el.nextElementSibling.textContent = (+el.value).toFixed(2);
  }
  const ms = +$('m-strength').value;
  $('m-warn').style.display = ($('m-active').checked && ms <= 0.02) ? 'block' : 'none';
}

function step() { tick += 1; render(); }
function stopPlay() {
  if (playId) { clearInterval(playId); playId = null; }
  $('b-play').textContent = 'PLAY';
  $('b-play').classList.remove('on');
}
function startPlay() {
  stopPlay();
  playId = setInterval(step, POLL_MS);
  $('b-play').textContent = 'PAUSE';
  $('b-play').classList.add('on');
}

function setFromPreset(p) {
  stopPlay();
  tick = 0;
  $('f-stage').value = p.stage;
  $('f-openness').value = p.o; $('f-focus').value = p.f;
  $('f-resonance').value = p.r; $('f-tension').value = p.t;
  $('f-intensity').value = p.i; $('f-confidence').value = p.c;
  $('m-active').checked = p.ma; $('m-strength').value = p.ms;
  $('m-ld').value = p.ld; $('m-ca').value = p.ca;
  $('m-es').value = p.es; $('m-gb').value = p.gb;
  logEntries.length = 0;
  render();
}

function buildPresets() {
  const host = $('presets');
  const defs = [
    ['neutral rest', {stage:'none',o:0,f:0,r:.5,t:.5,i:0,c:0,ma:false,ms:0,ld:0,ca:0,es:0,gb:0}],
    ['exploratory', {stage:'prompt',o:.9,f:.05,r:.6,t:.4,i:.9,c:.3,ma:false,ms:0,ld:0,ca:0,es:0,gb:0}],
    ['strong focus', {stage:'paired',o:-.2,f:.95,r:.55,t:.45,i:.95,c:.7,ma:false,ms:0,ld:0,ca:0,es:0,gb:0}],
    ['high resonance', {stage:'paired',o:.3,f:.2,r:.95,t:.05,i:.7,c:.8,ma:false,ms:0,ld:0,ca:0,es:0,gb:0}],
    ['high divergence', {stage:'paired',o:.1,f:.4,r:.08,t:.92,i:.9,c:.8,ma:false,ms:0,ld:0,ca:0,es:0,gb:0}],
    ['established pair', {stage:'none',o:0,f:0,r:.5,t:.5,i:0,c:0,ma:true,ms:.59,ld:.21,ca:.03,es:.06,gb:0}],
    ['live + mutation', {stage:'paired',o:.7,f:.3,r:.7,t:.3,i:.75,c:.7,ma:true,ms:.59,ld:.21,ca:.03,es:.06,gb:0}],
  ];
  for (const [label, p] of defs) {
    const b = document.createElement('button');
    b.textContent = label;
    b.onclick = () => setFromPreset(p);
    host.appendChild(b);
  }
}

function buildTabs() {
  const host = $('tabs');
  for (const t of TEMPLATES) {
    const b = document.createElement('button');
    b.textContent = t.label || t.id;
    b.className = 'btn' + (t.id === template ? ' on' : '');
    b.onclick = () => {
      template = t.id;
      for (const x of host.children) x.classList.remove('on');
      b.classList.add('on');
      render();
    };
    host.appendChild(b);
  }
}

$('b-step').onclick = step;
$('b-play').onclick = () => (playId ? stopPlay() : startPlay());
$('b-reset').onclick = () => { stopPlay(); tick = 0; logEntries.length = 0; render(); };
$('b-zoom').onclick = () => {
  const on = $('b-zoom').classList.toggle('on');
  $('cropbox').classList.toggle('zoom', on);
  $('cropbox').classList.toggle('fit', !on);
  $('cropcap').textContent = on ? 'STUDIO · FACE ZOOM (crop-to-fit)' : 'STUDIO · FULL PORTRAIT';
};
$('b-mark').onclick = () => {
  $('b-mark').classList.toggle('on');
  render();
};

for (const el of document.querySelectorAll('input, select')) {
  el.addEventListener('input', () => { stopPlay(); tick = 0; logEntries.length = 0; render(); });
}

buildTabs(); buildPresets();
// start on the exploratory preset so saccades are visible from t = 0
setFromPreset({ stage: 'prompt', o: .9, f: .05, r: .6, t: .4, i: .9, c: .3,
                ma: false, ms: 0, ld: 0, ca: 0, es: 0, gb: 0 });
</script>
</body>
</html>
"""

out = (HTML
       .replace("__ENGINE__", engine_block)
       .replace("__SHA__", sha))
dest = ROOT / "tools" / "portrait_motion_tuner.html"
dest.write_text(out, encoding="utf-8")
print("wrote", dest, len(out), "bytes; engine block sha256:", sha)
