// It's in Your Head — desktop half: a 96px signal strip under the conversation.
//
// Contract (Desktop Plugin SDK):
// - imports: ONLY @hermes/plugin-sdk + react / react/jsx-runtime; jsx() calls,
//   never JSX syntax (the file loads uncompiled).
// - theme vars only — the pane already sits on the app's editor background;
//   no hardcoded color values (DOM cells take theme vars directly).
// - data: useQuery (the app's one React Query client) polling ctx.rest — the
//   mandatory fallback; host.onEvent('plugin.its-in-your-head.tick') is the
//   live accelerator (ctx.socket is a no-op on OAuth remotes).
// - layout budget 96px: 22 header/tabs, 48 main, 18 ticker, 8 separators.
//   Overflow goes to the settings popover, never in-pane scrolling.
import { useState, useRef, useEffect } from 'react'
import { host, icons, useQuery, queryClient } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const POLL_MS = 5000
const PULSE_POLL_MS = 15000
// Legacy hourly lane retired: the pane polls /pulse at the same slow cadence
// the old hourly heat lane used; POLL_MS polling below is the mandatory
// fallback on every route.
const IO_WIDTH = 220   // fixed I/O-frame column
const NARROW_W = 480   // below this frame width the strip is docked narrow (sidebar): I/O column hidden, header collapses to essentials
const IO_LINES = 4
// PULSE matrix rows shown in the main lane: the lane is ~48px, so at most
// 4 rows at a legible 10px pitch — the rest stay reachable through the
// /pulse response and the settings popover, never in-pane scrolling.
const PULSE_MAX_ROWS = 4
const STALE_S = 60     // pet returns to idle after this many quiet seconds
const OK_WINDOW_S = 30 // a successful completion stays 'successful' this long

// PETSTATE-START
// Pure pet state machine: 7 deterministic states, a pure function of the last
// polled event plus time-since. No I/O, no globals — unit-testable.
const PET_GLYPHS = {
  idle: '(-.-)',
  processing: '(o.o)',
  reading: '(o>o)',
  writing: '(o<o)',
  verifying: '(o.o)~',
  successful: '(^.^)',
  friction: '(>_<)',
}

function petGlyph(state) {
  return PET_GLYPHS[state] || PET_GLYPHS.idle
}

function toolNameOf(ev) {
  if (!ev) return ''
  return String(ev.tool || ev.tool_name || ev.name || '').toLowerCase()
}

function petState(ev, nowSec) {
  if (!ev) return 'idle'
  const age = typeof ev.ts === 'number' ? nowSec - ev.ts : 0
  if (age > STALE_S) return 'idle'
  const statusCode = typeof ev.status_code === 'number' ? ev.status_code : 0
  if (ev.type === 'error' || statusCode >= 400) return 'friction'
  const tool = toolNameOf(ev)
  if (tool) {
    if (/read|get|list|search|fetch|cat|grep|glob/.test(tool)) return 'reading'
    if (/write|edit|create|patch|apply|save|put/.test(tool)) return 'writing'
    if (/test|verify|check|lint|run|build/.test(tool)) return 'verifying'
    return 'processing'
  }
  if (ev.type === 'api') {
    const fr = ev.finish_reason ? String(ev.finish_reason) : ''
    if (fr === 'stop' || fr === 'ok' || fr === 'completed') {
      return age > OK_WINDOW_S ? 'idle' : 'successful'
    }
    return 'processing'
  }
  if (ev.type === 'turn') return 'processing'
  return 'processing'
}

// Public ticker phrasing only: tool name + status word. Never raw reasoning
// or raw tool output text — the /recent rows carry no such fields, and any
// future tool field is treated as a name, never a payload.
function tickerPhrase(ev, nowSec) {
  if (!ev) return 'no events yet'
  const age = typeof ev.ts === 'number' ? Math.max(0, Math.floor(nowSec - ev.ts)) : null
  const ageTxt = age === null ? '' : ' · ' + (age < 60 ? age + 's' : Math.floor(age / 60) + 'm')
  const tool = toolNameOf(ev)
  const failed = ev.type === 'error' ||
    (typeof ev.status_code === 'number' && ev.status_code >= 400)
  if (tool) return tool + ' ' + (failed ? 'fail' : 'ok') + ageTxt
  if (ev.type === 'turn') return 'turn ok' + ageTxt
  if (ev.type === 'error') {
    return 'error ' + (ev.status_code ? String(ev.status_code) : 'fail') + ageTxt
  }
  if (ev.type === 'api') {
    const fr = ev.finish_reason ? String(ev.finish_reason) : ''
    const word = (fr && fr !== 'stop' && /^[a-z_]+$/.test(fr)) ? fr : 'ok'
    return 'api ' + word + ageTxt
  }
  return 'event ok' + ageTxt
}
// PETSTATE-END

function fmtTokens(n) {
  if (n === null || n === undefined) return '–'
  if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M'
  if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'k'
  return String(n)
}

function fmtClock(ts) {
  if (!ts) return '–'
  const d = new Date(ts * 1000)
  const p = (x) => String(x).padStart(2, '0')
  return p(d.getHours()) + ':' + p(d.getMinutes())
}

// Backend-read diagnostic: surface the ACTUAL /metrics failure instead of
// collapsing every ctx.rest rejection into "backend offline". The rejection
// shape isn't contract-pinned (it may be an Error carrying .status /
// .statusCode, a string, or a plain object), so we extract an HTTP status from
// every plausible location and classify it — this immediately distinguishes
// auth failure (401/403), a route that was never mounted into the gateway
// (404), a backend fault (5xx), and a connectivity loss (no status at all).
function backendErrorLabel(err) {
  const src =
    err == null
      ? ''
      : String(
          err &&
          (err.message || err.detail || err.reason || err.status || err.statusCode) != null
            ? err.message || err.detail || err.reason || err.status || err.statusCode
            : err,
        )
  let code = null
  if (err && typeof err === 'object') {
    for (const k of ['status', 'statusCode', 'code']) {
      if (typeof err[k] === 'number' && err[k] >= 400 && err[k] < 600) {
        code = err[k]
        break
      }
    }
  }
  if (code === null) {
    const m = src.match(/\b([45]\d\d)\b/)
    if (m) code = Number(m[1])
  }
  let label
  if (code === 401) label = 'unauthorized (401)'
  else if (code === 403) label = 'forbidden (403)'
  else if (code === 404) label = 'route not mounted (404)'
  else if (code !== null && code >= 500) label = 'backend error (' + code + ')'
  else if (code !== null) label = 'HTTP ' + code
  else if (/ECONN|fetch failed|network|unreachable|timed? ?out|socket/i.test(src))
    label = 'unreachable (connection)'
  else if (src.trim()) label = 'read failed'
  else label = 'backend offline'
  return label
}

// PULSE-START
// Pure PULSE matrix helpers (no DOM/canvas): column mapping + cell values.
// Column mapping (F1): the visible order is LIVE / GOAL / GOOD / FRICTION
// per the original design (pulse/matrix.py). The server's `columns` list
// still carries the legacy 'unlabeled' entry, but no row carries an
// `unlabeled` cell — unlabeled tools report state='learning' on their
// goal/good/friction cells (row.state / row.labeled), so 'unlabeled'
// folds into those cells and is dropped from the visible columns.
// LIVE is derived client-side from the per-row `live` counts the response
// already carries (calls_started/calls_completed/errors) — no engine
// field is needed and none is invented here.
function pulseCellValue(cell) {
  if (!cell || typeof cell !== 'object') return null
  const v = cell.value
  return typeof v === 'number' && Number.isFinite(v)
    ? Math.min(1, Math.max(0, v))
    : null
}

function pulseColumns(pulse) {
  const semantic = ['goal', 'good', 'friction']
  const fromServer = pulse && Array.isArray(pulse.columns)
    ? pulse.columns.filter((c) => semantic.indexOf(c) !== -1)
    : []
  const rest = fromServer.length
    ? semantic.filter((c) => fromServer.indexOf(c) !== -1)
    : semantic
  return ['live', ...rest]
}

// LIVE value: squash unbounded activity counts onto 0..1 (1 call -> 0.5,
// saturating towards 1) so the cell shares the alpha encoding of the
// similarity cells. No recorded activity -> null (neutral/blank), which
// also marks the row as not live for the muted row styling below.
function pulseLiveValue(row) {
  const live = row && typeof row.live === 'object' ? row.live : null
  if (!live) return null
  const total = [live.calls_started, live.calls_completed, live.errors]
    .reduce((s, v) => s + (typeof v === 'number' && Number.isFinite(v) ? v : 0), 0)
  if (total <= 0) return null
  return 1 - 1 / (1 + total)
}
// Row selection is deterministic and view-side only: the server returns
// rows in first_seen order; the pane shows the most relevant `cap` of them.
// Ranking: (1) live activity total descending — rows the response records
// activity for rank ahead of rows with none; (2) strongest semantic cell
// (goal/good/friction max 0..1) descending; (3) original server order
// (stable). No hardcoded tool list, no randomness.
function selectPulseRows(rows, cap) {
  if (!Array.isArray(rows) || cap <= 0) return []
  const ranked = rows.map((row, i) => {
    const live = pulseLiveValue(row)
    const liveT = live === null ? -1 : live
    let sem = 0
    ;['goal', 'good', 'friction'].forEach((c) => {
      const v = pulseCellValue(row ? row[c] : null)
      if (v !== null && v > sem) sem = v
    })
    return { row: row, i: i, liveT: liveT, sem: sem }
  })
  ranked.sort((a, b) => (b.liveT - a.liveT) || (b.sem - a.sem) || (a.i - b.i))
  return ranked.slice(0, cap).map((r) => r.row)
}
// PULSE-END

// PULSE matrix structure: a real grid of row × column cells (a DOM table,
// not a painted canvas). The label and its four graphic cells are sibling
// cells of one grid row, so the layout engine — not pitch arithmetic —
// guarantees every tool label sits on its own row of cells. Rows are
// dynamic (whichever tool rows GET /pulse carries, selected by
// selectPulseRows); empty cells reserve their slot, so the grid geometry
// never shifts. Same encoding contract as the retired heat lane: cells
// carry the host accent at 0.12..1.0 alpha (friction uses the danger
// token, same alpha scale) over the host surface; no hardcoded colors.
function PulseMatrix({ pulse }) {
  const columns = pulseColumns(pulse)
  const rows = pulse && Array.isArray(pulse.rows) ? pulse.rows : []
  if (rows.length === 0) {
    return jsx('div', {
      className: 'flex flex-1 items-center justify-center',
      style: {
        fontSize: 10, color: 'var(--ui-text-quaternary)',
        fontFamily: 'var(--ui-font-mono, monospace)', letterSpacing: 1,
      },
      children: 'pulse matrix · 4 semantic columns',
    })
  }
  const shown = selectPulseRows(rows, PULSE_MAX_ROWS)
  const cells = []
  shown.forEach((row, ri) => {
    const tool = String((row && row.tool_name) || (row && row.tool) || '—')
    // A row is live when the response records activity for it (row.live
    // counts); live rows are highlighted, rows with no activity yet are
    // muted — no new colors.
    const liveV = pulseLiveValue(row)
    const isLive = liveV !== null
    cells.push(jsx('div', {
      key: 'lbl-' + ri,
      title: tool,
      className: 'flex items-center overflow-hidden',
      style: {
        fontSize: 9, lineHeight: '14px', minWidth: 0,
        fontFamily: 'var(--ui-font-mono, monospace)',
        color: isLive ? V_SEC : V_QUAT,
      },
      children: tool.slice(0, 16),
    }))
    columns.forEach((col, ci) => {
      const v = col === 'live' ? liveV : pulseCellValue(row ? row[col] : null)
      const a = v === null || v <= 0
        ? 0
        : (0.12 + 0.88 * v) * (isLive ? 1 : 0.45)
      cells.push(jsx('div', {
        key: col + '-' + ri + '-' + ci,
        title: col,
        style: {
          background: col === 'friction' ? V_DANGER : V_ACCENT,
          opacity: a,
          borderRadius: 2,
          minHeight: 4,
        },
      }))
    })
  })
  return jsx('div', {
    role: 'grid',
    'aria-label': 'pulse matrix',
    className: 'flex-1',
    style: {
      display: 'grid',
      minWidth: 0,
      gridTemplateColumns: 'minmax(56px, 34%) repeat(' + (columns.length - 1) + ', 1fr)',
      gridTemplateRows: 'repeat(' + PULSE_MAX_ROWS + ', 1fr)',
      gap: '2px 3px',
    },
    children: cells,
  })
}

function IoLine({ ev }) {
  let tag, body, dot
  // DOM dots use theme vars directly (var() with a nested fallback — no
  // hardcoded color values).
  if (ev.type === 'turn') {
    tag = 'U'; body = ev.model || 'turn'; dot = 'var(--ui-stroke-secondary)'
  } else if (ev.type === 'api') {
    tag = 'A'
    body = fmtTokens(ev.total_tokens) + ' tok' + (ev.finish_reason ? ' · ' + ev.finish_reason : '')
    dot = 'var(--ui-accent)'
  } else if (ev.type === 'error') {
    tag = 'E'; body = ev.status_code ? String(ev.status_code) : 'error'
    dot = 'var(--dt-destructive, var(--destructive, var(--ui-accent)))'
  } else {
    return null
  }
  return jsxs('div', {
    className: 'flex items-center gap-1 overflow-hidden',
    style: { fontSize: 10, lineHeight: '12px', color: 'var(--ui-text-secondary)' },
    children: [
      jsx('span', {
        style: {
          width: 7, height: 7, borderRadius: 4,
          flexShrink: 0, background: dot, display: 'inline-block',
        },
      }),
      jsx('span', { style: { color: 'var(--ui-text-quaternary)', width: 10, flexShrink: 0 }, children: tag }),
      jsx('span', {
        style: { whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' },
        children: body,
      }),
    ],
  })
}

// C4 settings keys, listed read-only until the /settings POST route lands.
const SETTING_KEYS = [
  'perception.enabled',
  'observation.reasoning',
  'observation.retain_days',
  'pulse.prototype_cap',
  'pulse.min_labeled',
  'pulse.thresholds.goal',
  'pulse.thresholds.good',
  'pulse.thresholds.friction',
  'adaptation.enabled',
  'adaptation.rules',
]

// Shared theme-var strings for the portrait/drift views, defined once so the
// var() census stays stable; each is an installed-build token (fixture-verified).
const V_ACCENT = 'var(--ui-accent)'
const V_SEC = 'var(--ui-text-secondary)'
const V_QUAT = 'var(--ui-text-quaternary)'
const V_STROKE = 'var(--ui-stroke-secondary)'
const V_DANGER = 'var(--dt-destructive, var(--ui-accent))'

function clamp01(v) {
  return typeof v === 'number' && Number.isFinite(v)
    ? Math.min(1, Math.max(0, v))
    : null
}

// PORTRAIT-GLYPH-START
// Pure deterministic portrait glyph: a 48x48 orbit sigil. No DOM/React/Date/
// random, no globals beyond the constants below. Canonical dim order makes
// output byte-identical regardless of input key order. Each dim is quantized
// to integer steps (0..8 in 1/8 increments) so a 0.05 value move shifts at
// most one step. Two tracks are rendered as separate <g data-layer> groups
// and never blended: observed (user) and interpreted (agent, absent when
// all agent values are null).
const GLYPH_DIMS = ['directness', 'context_richness', 'exploration', 'analytical_depth', 'iteration', 'scope']
const GLYPH_ACCENT = 'var(--ui-accent)'
const GLYPH_SEC = 'var(--ui-text-secondary)'
const GLYPH_QUAT = 'var(--ui-text-quaternary)'
const GLYPH_STROKE = 'var(--ui-stroke-secondary)'

function glyphClamp(v) {
  return typeof v === 'number' && Number.isFinite(v) ? Math.min(1, Math.max(0, v)) : null
}
function glyphStep(v) {
  const c = glyphClamp(v)
  return c === null ? -1 : Math.round(c * 8)
}
function glyphFmt(n) {
  const r = Math.round(n * 100) / 100
  return String(r === 0 ? 0 : r)
}
function glyphTrackSteps(track) {
  const t = track && typeof track === 'object' ? track : {}
  return GLYPH_DIMS.map((d) => glyphStep(t[d]))
}
function glyphEvidenceCount(input) {
  if (input && typeof input === 'object') {
    if (typeof input.evidenceCount === 'number' && Number.isFinite(input.evidenceCount)) return Math.max(0, input.evidenceCount)
    const series = (input.drift && typeof input.drift === 'object' && input.drift.series) || input.series
    if (series && typeof series === 'object') {
      let best = 0
      for (const d of GLYPH_DIMS) {
        const s = series[d]
        if (Array.isArray(s) && s.length > best) best = s.length
      }
      return best
    }
    if (input.portrait && typeof input.portrait === 'object') return glyphEvidenceCount(input.portrait)
  }
  return 0
}
function glyphEvidenceStep(evCount) {
  if (evCount >= 16) return 4
  if (evCount >= 8) return 3
  if (evCount >= 4) return 2
  if (evCount >= 1) return 1
  return 0
}
function glyphConfStep(confidence, present) {
  const c = confidence && typeof confidence === 'object' ? confidence : {}
  let sum = 0, n = 0
  for (let i = 0; i < GLYPH_DIMS.length; i++) {
    if (!present[i]) continue
    const v = glyphClamp(c[GLYPH_DIMS[i]])
    if (v !== null) { sum += v; n++ }
  }
  if (n === 0) return 0
  return Math.round((sum / n) * 4)
}
function glyphPoint(cx, cy, r, deg) {
  const rad = (deg * Math.PI) / 180
  return [cx + r * Math.cos(rad), cy + r * Math.sin(rad)]
}
function glyphTrackGroup(layer, steps, unfold, interpreted) {
  const isInterp = !!interpreted
  const opacity = isInterp ? 0.2 + 0.5 * unfold : 0.3 + 0.6 * unfold
  const stepOr = (v, dflt) => (v < 0 ? dflt : v)
  const scopeStep = stepOr(steps[5], 2)
  const iterStep = stepOr(steps[4], 0)
  const richStep = stepOr(steps[1], 0)
  const explStep = stepOr(steps[2], 0)
  const depthStep = stepOr(steps[3], 0)
  const directStep = stepOr(steps[0], 0)
  const cx = 24, cy = 24
  const radius = 7 + scopeStep * 1.3
  const ringsFull = 1 + Math.floor(iterStep / 2)
  const spokesFull = 1 + richStep
  const sides = 3 + depthStep
  const rings = Math.max(1, 1 + Math.floor((ringsFull - 1) * unfold))
  const spokes = Math.max(1, 1 + Math.floor((spokesFull - 1) * unfold))
  const lattice = Math.floor(depthStep * unfold / 2)
  const straight = directStep >= 4
  const rot = explStep * 10
  const satR = 1 + Math.floor(explStep / 4)
  const ringStroke = isInterp ? GLYPH_SEC : GLYPH_STROKE
  const innerStroke = isInterp ? GLYPH_SEC : GLYPH_ACCENT
  const spokeStroke = isInterp ? GLYPH_QUAT : GLYPH_SEC
  const satFill = isInterp ? GLYPH_QUAT : GLYPH_ACCENT
  // Interpreted track is scaled 0.92 about the shared centre so the two
  // layers stay geometrically distinct even when both tracks quantize to the
  // same steps (otherwise identical steps would overplot pixel-for-pixel).
  const layerTransform = isInterp ? ' transform="translate(24 24) scale(0.92) translate(-24 -24)"' : ''
  let out = '<g data-layer="' + layer + '" opacity="' + glyphFmt(opacity) + '"' + layerTransform + '>'
  for (let i = 0; i < rings; i++) {
    const rr = radius - i * 3.2
    if (rr < 3) break
    out += '<circle cx="' + cx + '" cy="' + cy + '" r="' + glyphFmt(rr) + '" fill="none" stroke="' + ringStroke + '"' +
      (isInterp ? ' stroke-dasharray="2 2"' : '') + ' stroke-width="1"/>'
  }
  for (let i = 0; i < spokes; i++) {
    const ang = rot + (360 / spokes) * i
    const p = glyphPoint(cx, cy, radius, ang)
    out += '<line x1="' + cx + '" y1="' + cy + '" x2="' + glyphFmt(p[0]) + '" y2="' + glyphFmt(p[1]) + '" stroke="' + spokeStroke + '" stroke-width="1"/>'
    out += '<circle cx="' + glyphFmt(p[0]) + '" cy="' + glyphFmt(p[1]) + '" r="' + satR + '" fill="' + satFill + '"/>'
  }
  const innerR = Math.max(4, radius * 0.55)
  const pts = []
  for (let i = 0; i < sides; i++) pts.push(glyphPoint(cx, cy, innerR, rot + (360 / sides) * i - 90))
  if (straight) {
    const attr = pts.map((p) => glyphFmt(p[0]) + ',' + glyphFmt(p[1])).join(' ')
    out += '<polygon points="' + attr + '" fill="none" stroke="' + innerStroke + '" stroke-width="1.2" stroke-linejoin="miter" data-joint="miter"/>'
  } else {
    let d = 'M' + glyphFmt(pts[0][0]) + ' ' + glyphFmt(pts[0][1])
    for (let i = 1; i <= sides; i++) {
      const p = pts[i % sides]
      d += ' Q' + glyphFmt(pts[(i - 1) % sides][0]) + ' ' + glyphFmt(pts[(i - 1) % sides][1]) + ' ' + glyphFmt(p[0]) + ' ' + glyphFmt(p[1])
    }
    d += ' Z'
    out += '<path d="' + d + '" fill="none" stroke="' + innerStroke + '" stroke-width="1.2" stroke-linejoin="round" data-joint="round"/>'
  }
  for (let i = 0; i < lattice; i++) {
    const ang = rot + i * (180 / Math.max(1, lattice))
    const a = glyphPoint(cx, cy, innerR, ang)
    const b = glyphPoint(cx, cy, innerR, ang + 180)
    out += '<line x1="' + glyphFmt(a[0]) + '" y1="' + glyphFmt(a[1]) + '" x2="' + glyphFmt(b[0]) + '" y2="' + glyphFmt(b[1]) + '" stroke="' + spokeStroke + '" stroke-width="0.7"/>'
  }
  out += '</g>'
  return out
}
function buildGlyphAt(input, size) {
  const s = typeof size === 'number' && Number.isFinite(size) && size > 0 ? Math.round(size) : 48
  const svgOpen = '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg" width="' + s + '" height="' + s + '" role="img">'
  const src = input && typeof input === 'object' ? (input.portrait && typeof input.portrait === 'object' && !input.dimensions ? input.portrait : input) : {}
  const available = src.available !== false
  const userSteps = glyphTrackSteps(src.user)
  const agentSteps = glyphTrackSteps(src.agent)
  const userPresent = userSteps.map((s2) => s2 >= 0)
  const agentPresent = agentSteps.map((s2) => s2 >= 0)
  const anyUser = userPresent.some(Boolean)
  const anyAgent = agentPresent.some(Boolean)
  const minimal = svgOpen + '<g data-layer="observed" opacity="0.25"><circle cx="24" cy="24" r="8" fill="none" stroke="' + GLYPH_STROKE + '" stroke-width="1"/></g></svg>'
  if (!available || (!anyUser && !anyAgent)) return minimal
  const present = GLYPH_DIMS.map((_, i) => userPresent[i] || agentPresent[i])
  const evStep = glyphEvidenceStep(glyphEvidenceCount(input))
  const confStep = glyphConfStep(src.confidence, present)
  const strength = evStep + confStep
  const unfold = Math.min(1, Math.max(0, strength / 8))
  let body = ''
  if (anyUser) body += glyphTrackGroup('observed', userSteps, unfold, false)
  if (anyAgent) body += glyphTrackGroup('interpreted', agentSteps, unfold, true)
  if (!body) return minimal
  return svgOpen + body + '</svg>'
}
// buildGlyph: the 48px compact case (byte-identical to the pre-refactor output).
function buildGlyph(input) {
  return buildGlyphAt(input, 48)
}
// PORTRAIT-GLYPH-END

function PortraitView({ data, drift }) {
  const dims =
    data && data.dimensions && typeof data.dimensions === 'object'
      ? Object.keys(data.dimensions)
      : []
  const hasEvidence =
    !!(data && data.available) &&
    dims.some(
      (d) =>
        clamp01(data.user && data.user[d]) !== null ||
        clamp01(data.agent && data.agent[d]) !== null,
    )
  if (!hasEvidence) {
    return jsxs('div', {
      className: 'flex flex-1 items-center justify-center',
      style: {
        fontSize: 10, color: V_QUAT,
        fontFamily: 'var(--ui-font-mono, monospace)', letterSpacing: 1,
      },
      children: ['PORTRAIT · no data yet — evidence empty'],
    })
  }
  let evidenceCount = 0
  if (drift && drift.series && typeof drift.series === 'object') {
    for (const d of Object.keys(drift.series)) {
      const s = drift.series[d]
      if (Array.isArray(s) && s.length > evidenceCount) evidenceCount = s.length
    }
  }
  const glyph = buildGlyph({
    available: data.available,
    dimensions: data.dimensions,
    user: data.user,
    agent: data.agent,
    confidence: data.confidence,
    evidenceCount,
  })
  return jsxs('div', {
    className: 'flex flex-1 items-center',
    style: { gap: 12, minHeight: 0, overflow: 'hidden' },
    children: [
      jsx('span', {
        title: 'portrait glyph',
        style: { width: 48, height: 48, flexShrink: 0, overflow: 'hidden' },
        dangerouslySetInnerHTML: { __html: glyph },
      }),
    ],
  })
}

function driftDelta(series) {
  if (!Array.isArray(series) || series.length < 2) return null
  const a = series[series.length - 2]
  const b = series[series.length - 1]
  return typeof a === 'number' && typeof b === 'number' ? b - a : null
}

function DriftView({ data, narrow }) {
  const series =
    data && data.series && typeof data.series === 'object' ? data.series : {}
  const axes = Object.keys(series)
  const deltas = axes.map((d) => driftDelta(series[d]))
  if (!(data && data.available) || deltas.every((x) => x === null)) {
    return jsxs('div', {
      className: 'flex flex-1 items-center justify-center',
      style: {
        fontSize: 10, color: V_QUAT,
        fontFamily: 'var(--ui-font-mono, monospace)', letterSpacing: 1,
      },
      children: ['DRIFT · no data yet — need 2 snapshots'],
    })
  }
  return jsxs('div', {
    className: 'flex flex-1 items-center',
    style: {
      gap: narrow ? '2px 6px' : 10,
      minHeight: 0, overflow: 'hidden',
      flexWrap: narrow ? 'wrap' : undefined,
      rowGap: narrow ? 2 : undefined,
    },
    children: axes.map((d, i) => {
      const delta = deltas[i]
      const mag = delta === null ? 0 : Math.min(1, Math.abs(delta))
      const neg = delta !== null && delta < 0
      return jsxs('div', {
        key: d,
        className: 'flex flex-col',
        // Narrow dock: two rows of three (labels move into the tooltip —
        // the numbers stay; full axis inspection lives in the workspace).
        style: { gap: 2, minWidth: 0, flex: narrow ? '1 1 30%' : undefined, title: d },
        children: [
          !narrow && jsx('span', {
            style: {
              fontSize: 8, color: V_QUAT, whiteSpace: 'nowrap',
              overflow: 'hidden', textOverflow: 'ellipsis',
            },
            children: d.slice(0, 10),
          }),
          jsxs('div', {
            style: {
              position: 'relative', width: 46, height: 6,
              background: V_STROKE,
            },
            children: [
              jsx('div', {
                style: {
                  position: 'absolute', top: 0, bottom: 0,
                  left: neg ? 0 : '50%',
                  width: Math.round(mag * 50) + '%',
                  background: neg ? V_DANGER : V_ACCENT,
                  opacity: delta === null ? 0 : 0.9,
                },
              }),
              neg &&
                jsx('div', {
                  style: {
                    position: 'absolute', top: -2, bottom: -2,
                    left: Math.round(mag * 50) + '%', width: 2,
                    background: V_DANGER,
                  },
                }),
            ],
          }),
          jsx('span', {
            style: { fontSize: 8, color: neg ? V_DANGER : V_SEC },
            children: delta === null
              ? '·'
              : (neg ? '−' : '+') + Math.abs(delta).toFixed(2),
          }),
        ],
      }, i)
    }),
  })
}

function settingValue(settings, key) {
  if (!settings) return '—'
  let cur = settings
  for (const part of key.split('.')) {
    if (cur === null || typeof cur !== 'object') return '—'
    cur = cur[part]
  }
  if (cur === null || cur === undefined) return '—'
  return Array.isArray(cur) ? cur.length + ' rules' : String(cur)
}

// WORKSPACE-START
// Pure expanded-workspace helpers (no DOM/React/globals): sparkline
// geometry for the DRIFT history view. Deterministic: identical series
// -> identical points; values are rounded to 0.1 units so sub-pixel
// drift never changes the output.
const SPARK_W = 96
const SPARK_H = 24
function driftSparkline(values) {
  if (!Array.isArray(values)) return null
  const nums = values.filter((v) => typeof v === 'number' && Number.isFinite(v))
  if (nums.length < 2) return null
  const min = Math.min.apply(null, nums)
  const max = Math.max.apply(null, nums)
  const span = max - min || 1
  const pad = 2
  const n = nums.length
  const points = nums.map((v, i) => {
    const x = (pad + (i * (SPARK_W - 2 * pad)) / (n - 1)).toFixed(1)
    const y = (SPARK_H - pad - ((v - min) / span) * (SPARK_H - 2 * pad)).toFixed(1)
    return x + ',' + y
  })
  return { points: points.join(' '), delta: Math.round((nums[n - 1] - nums[n - 2]) * 1000) / 1000, count: n }
}
// WORKSPACE-END

// ExpandedWorkspace — the full inspection surface opened by the compact
// instrument's single expand icon (host.openWorkspace). Same three views,
// same visual language, same data (the shared React Query client / keys
// below — no second observer, engine or collector). The extra space buys
// information density, not decoration: the complete tool matrix, the
// portrait as the main visual with its six measurements as inspection
// (never the portrait itself), and readable drift history.
const WS_WORKSPACE_ID = 'its-in-your-head-workspace'

function ExpandedPulse({ pulse }) {
  const columns = pulseColumns(pulse)
  const rows = pulse && Array.isArray(pulse.rows) ? pulse.rows : []
  const cellText = (cell) => {
    if (!cell || typeof cell !== 'object') return '—'
    const state = String(cell.state || 'neutral')
    if (state === 'ready') return cell.level === 'strong' ? 'ready · strong' : 'ready · weak'
    return state
  }
  const cellTip = (col, cell, row) => {
    const v = col === 'live' ? pulseLiveValue(row) : pulseCellValue(cell)
    let t = col + ' ' + (v === null ? 'neutral (no evidence)' : String(Math.round(v * 100) / 100))
    if (col === 'friction' && cell && typeof cell === 'object') {
      if (cell.user !== null && cell.user !== undefined) t += ' · user ' + String(Math.round(cell.user * 100) / 100)
      if (cell.objective !== null && cell.objective !== undefined) t += ' · objective ' + String(Math.round(cell.objective * 100) / 100)
    }
    return t
  }
  if (rows.length === 0) {
    return jsx('div', {
      className: 'flex flex-1 items-center justify-center',
      style: { fontSize: 10, color: V_QUAT, fontFamily: 'var(--ui-font-mono, monospace)', letterSpacing: 1 },
      children: 'PULSE · no tool rows yet — rows appear as tools are observed',
    })
  }
  const cellBase = {
    fontSize: 8, fontFamily: 'var(--ui-font-mono, monospace)',
    color: V_QUAT, textAlign: 'center', letterSpacing: 0.5,
  }
  return jsxs('div', {
    className: 'flex-1',
    style: { minHeight: 0, overflowY: 'auto', display: 'flex', flexDirection: 'column' },
    children: [
      jsxs('div', {
        className: 'flex',
        style: { gap: 8, padding: '4px 0', borderBottom: '1px solid ' + V_STROKE, flexShrink: 0 },
        children: [
          jsx('span', { style: Object.assign({ flex: '0 0 120px' }, cellBase, { color: V_SEC }), children: 'TOOL' }),
          columns.map((c) => jsx('span', {
            key: c,
            style: Object.assign({ flex: 1 }, cellBase, { color: c === 'friction' ? V_DANGER : V_SEC, fontWeight: 600 }),
            children: c.toUpperCase(),
          })),
        ],
      }),
      rows.map((row, ri) => {
        const tool = String((row && row.tool_name) || (row && row.tool) || '—')
        const liveV = pulseLiveValue(row)
        const isLive = liveV !== null
        return jsxs('div', {
          key: ri,
          className: 'flex items-center',
          style: { gap: 8, padding: '5px 0', borderBottom: '1px solid ' + V_STROKE, flexShrink: 0 },
          children: [
            jsx('span', {
              className: 'overflow-hidden',
              style: { flex: '0 0 120px', fontSize: 9, color: isLive ? V_SEC : V_QUAT, fontFamily: 'var(--ui-font-mono, monospace)' },
              title: tool,
              children: tool.slice(0, 18),
            }),
            columns.map((c) => {
              const cell = c === 'live' ? null : row ? row[c] : null
              const v = c === 'live' ? liveV : pulseCellValue(cell)
              const a = v === null || v <= 0 ? 0 : 0.12 + 0.88 * v
              return jsxs('div', {
                key: c,
                className: 'flex flex-col',
                style: { flex: 1, gap: 2, minWidth: 0 },
                title: cellTip(c, cell, row),
                children: [
                  jsx('div', {
                    style: { height: 5, borderRadius: 1, background: c === 'friction' ? V_DANGER : V_ACCENT, opacity: a },
                  }),
                  c === 'live'
                    ? jsx('span', { style: Object.assign({}, cellBase, { color: isLive ? V_SEC : V_QUAT }), children: isLive ? String(Math.round(liveV * 100) / 100) : '—' })
                    : jsx('span', { style: Object.assign({}, cellBase, { color: cell && cell.state === 'ready' ? V_SEC : V_QUAT }), children: cellText(cell) }),
                ],
              })
            }),
          ],
        })
      }),
    ],
  })
}

function ExpandedPortrait({ data, drift }) {
  const dims = data && data.dimensions && typeof data.dimensions === 'object' ? Object.keys(data.dimensions) : []
  const hasEvidence = !!(data && data.available) && dims.some((d) =>
    clamp01(data.user && data.user[d]) !== null || clamp01(data.agent && data.agent[d]) !== null)
  let evidenceCount = 0
  if (drift && drift.series && typeof drift.series === 'object') {
    for (const d of Object.keys(drift.series)) {
      const s = drift.series[d]
      if (Array.isArray(s) && s.length > evidenceCount) evidenceCount = s.length
    }
  }
  const glyph = buildGlyphAt({
    available: data && data.available,
    dimensions: data && data.dimensions,
    user: data && data.user,
    agent: data && data.agent,
    confidence: data && data.confidence,
    evidenceCount,
  }, 160)
  const num = (v) => (v === null || typeof v !== 'number' || !Number.isFinite(v) ? '—' : String(Math.round(v * 100) / 100))
  const axes = dims.length > 0 ? dims : GLYPH_DIMS
  return jsxs('div', {
    className: 'flex-1',
    style: { minHeight: 0, overflowY: 'auto', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10, padding: '8px 0' },
    children: [
      hasEvidence
        ? jsx('div', {
            title: 'portrait glyph — the accumulated resemblance; not a measurement',
            style: { width: 160, height: 160, flexShrink: 0 },
            dangerouslySetInnerHTML: { __html: glyph },
          })
        : jsx('span', {
            style: { fontSize: 10, color: V_QUAT, fontFamily: 'var(--ui-font-mono, monospace)', letterSpacing: 1 },
            children: 'PORTRAIT · no data yet — evidence empty',
          }),
      // Inspection layer: the six measurements behind the glyph. They are
      // evidence, not the portrait — kept below the glyph, in its own table.
      jsxs('div', {
        style: { width: '100%', maxWidth: 480, fontSize: 8, fontFamily: 'var(--ui-font-mono, monospace)' },
        children: [
          jsxs('div', {
            className: 'flex',
            style: { gap: 8, padding: '3px 0', borderBottom: '1px solid ' + V_STROKE, color: V_SEC, letterSpacing: 1 },
            children: [
              jsx('span', { style: { flex: '0 0 140px' }, children: 'DIMENSION' }),
              jsx('span', { style: { flex: 1, textAlign: 'right' }, children: 'OBSERVED' }),
              jsx('span', { style: { flex: 1, textAlign: 'right' }, children: 'INTERPRETED' }),
              jsx('span', { style: { flex: 1, textAlign: 'right' }, children: 'CONF' }),
            ],
          }),
          axes.map((d) => jsxs('div', {
            key: d,
            className: 'flex',
            style: { gap: 8, padding: '3px 0', borderBottom: '1px solid ' + V_STROKE, color: V_QUAT },
            children: [
              jsx('span', { style: { flex: '0 0 140px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, title: d, children: d }),
              jsx('span', { style: { flex: 1, textAlign: 'right', color: V_SEC }, children: num(data && data.user && data.user[d]) }),
              jsx('span', { style: { flex: 1, textAlign: 'right', color: V_SEC }, children: num(data && data.agent && data.agent[d]) }),
              jsx('span', { style: { flex: 1, textAlign: 'right' }, children: num(data && data.confidence && data.confidence[d]) }),
            ],
          })),
          jsx('div', {
            style: { padding: '3px 0', color: V_QUAT, letterSpacing: 1 },
            children: evidenceCount > 0 ? evidenceCount + ' evidence snapshots · observed track persisted, interpreted where the agent recorded it' : 'no evidence snapshots yet',
          }),
        ],
      }),
    ],
  })
}

function ExpandedDrift({ data }) {
  const series = data && data.series && typeof data.series === 'object' ? data.series : {}
  const axes = Object.keys(series)
  if (!(data && data.available) || axes.length === 0) {
    return jsx('div', {
      className: 'flex flex-1 items-center justify-center',
      style: { fontSize: 10, color: V_QUAT, fontFamily: 'var(--ui-font-mono, monospace)', letterSpacing: 1 },
      children: 'DRIFT · no data yet — need 2 snapshots',
    })
  }
  return jsxs('div', {
    className: 'flex-1',
    style: { minHeight: 0, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 6, padding: '6px 0' },
    children: axes.map((d) => {
      const spark = driftSparkline(series[d])
      const delta = driftDelta(series[d])
      const neg = delta !== null && delta < 0
      return jsxs('div', {
        key: d,
        className: 'flex items-center',
        style: { gap: 10, borderBottom: '1px solid ' + V_STROKE, paddingBottom: 6 },
        children: [
          jsx('span', {
            style: { flex: '0 0 110px', fontSize: 9, color: V_SEC, fontFamily: 'var(--ui-font-mono, monospace)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
            title: d,
            children: d,
          }),
          spark && jsx('svg', {
            viewBox: '0 0 ' + SPARK_W + ' ' + SPARK_H,
            style: { flex: 1, maxWidth: 420, height: SPARK_H, minWidth: 60 },
            children: [
              jsx('line', { x1: 0, y1: SPARK_H - 0.5, x2: SPARK_W, y2: SPARK_H - 0.5, stroke: V_STROKE, 'stroke-width': 1 }),
              jsx('polyline', {
                points: spark.points, fill: 'none', stroke: V_ACCENT,
                'stroke-width': 1.5, 'stroke-linejoin': 'round', style: { opacity: 0.9 },
              }),
            ],
          }),
          jsx('span', {
            style: { flex: '0 0 64px', textAlign: 'right', fontSize: 9, fontFamily: 'var(--ui-font-mono, monospace)', color: delta === null ? V_QUAT : (neg ? V_DANGER : V_SEC) },
            children: delta === null ? '—' : (neg ? '−' : '+') + Math.abs(delta).toFixed(2),
          }),
          jsx('span', { style: { flex: '0 0 48px', textAlign: 'right', fontSize: 8, color: V_QUAT, fontFamily: 'var(--ui-font-mono, monospace)' }, children: String(series[d].length) + ' pts' }),
        ],
      })
    }),
  })
}

function ExpandedWorkspace({ source, rest }) {
  const [tab, setTab] = useState('pulse')
  // Same query keys as the compact instrument -> the app's single React
  // Query client dedupes and both surfaces share one cache (one observer
  // set per key, never a second collector).
  const metricsQ = useQuery({
    queryKey: [source, 'iyh-metrics'],
    queryFn: () => rest('/metrics'),
    refetchInterval: POLL_MS,
    retry: false,
  })
  const pulseQ = useQuery({
    queryKey: [source, 'iyh-pulse'],
    queryFn: () => rest('/pulse'),
    refetchInterval: PULSE_POLL_MS,
    retry: false,
  })
  const portraitQ = useQuery({
    queryKey: [source, 'iyh-portrait'],
    queryFn: () => rest('/portrait').catch(() => null),
    refetchInterval: POLL_MS,
    retry: false,
  })
  const driftQ = useQuery({
    queryKey: [source, 'iyh-drift'],
    queryFn: () => rest('/drift').catch(() => null),
    refetchInterval: POLL_MS,
    retry: false,
  })
  const metrics = metricsQ.data
  const live = !!metrics
  const statusText = !metrics
    ? (metricsQ.isError ? backendErrorLabel(metricsQ.error) + ' · polling continues' : 'waiting for first read…')
    : metrics.events + ' events · ' + fmtTokens(metrics.total_tokens) + ' tok'
  return jsxs('div', {
    className: 'flex h-full flex-col',
    style: {
      padding: '6px 12px', gap: 6, minHeight: 0,
      borderTop: '1px solid ' + V_STROKE,
      color: V_SEC,
      fontFamily: 'var(--ui-font-mono, monospace)',
      overflow: 'hidden',
    },
    children: [
      jsxs('div', {
        className: 'flex items-center',
        style: { height: 22, flexShrink: 0, gap: 8, fontSize: 11 },
        children: [
          jsx('span', { style: { fontWeight: 600, letterSpacing: 1, fontSize: 10, flexShrink: 0 }, children: "IT'S IN YOUR HEAD" }),
          jsxs('span', {
            className: 'flex items-center',
            style: { gap: 4, fontSize: 8, letterSpacing: 1, color: live ? V_SEC : V_QUAT, flexShrink: 0 },
            children: [
              jsx('span', {
                style: { width: 6, height: 6, borderRadius: 3, display: 'inline-block', background: live ? V_ACCENT : V_STROKE },
              }),
              live ? 'LIVE' : (metricsQ.isError ? 'DEGRADED' : 'WAITING'),
            ],
          }),
          jsxs('div', {
            className: 'flex items-center',
            style: { gap: 2, flexShrink: 0 },
            children: TABS.map((t) => jsx(TabButton, {
              key: t.id, tab: t, active: tab === t.id,
              onClick: () => setTab(t.id),
            })),
          }),
          jsx('span', {
            title: statusText,
            style: { flex: 1, textAlign: 'right', fontSize: 9, color: V_QUAT, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' },
            children: statusText,
          }),
        ],
      }),
      tab === 'pulse'
        ? jsx(ExpandedPulse, { pulse: pulseQ.data })
        : tab === 'portrait'
          ? jsx(ExpandedPortrait, { data: portraitQ.data, drift: driftQ.data })
          : jsx(ExpandedDrift, { data: driftQ.data }),
    ],
  })
}

const TABS = [
  { id: 'pulse', label: 'PULSE' },
  { id: 'portrait', label: 'PORTRAIT' },
  { id: 'drift', label: 'DRIFT' },
]

function TabButton({ tab, active, onClick }) {
  return jsx('button', {
    type: 'button',
    title: tab.label + ' tab',
    onClick,
    style: {
      background: 'transparent',
      border: 'none',
      borderBottom: '1px solid var(--ui-accent)',
      borderBottomColor: active ? 'var(--ui-accent)' : 'transparent',
      color: active ? 'var(--ui-text-secondary)' : 'var(--ui-text-quaternary)',
      fontSize: 10, letterSpacing: 1, padding: '0 4px', height: 22,
      cursor: 'pointer', fontWeight: active ? 600 : 400,
    },
    children: tab.label,
  })
}

function IconButton({ title, label, onClick, disabled }) {
  return jsx('button', {
    type: 'button',
    title,
    onClick: disabled ? undefined : onClick,
    disabled: disabled || undefined,
    style: {
      background: 'transparent', border: 'none', cursor: disabled ? 'default' : 'pointer',
      color: 'var(--ui-text-quaternary)', fontSize: 12, padding: '0 3px', height: 22,
      opacity: disabled ? 0.35 : 1, display: 'inline-flex', alignItems: 'center',
    },
    children: label,
  })
}

function Strip({ source, rest }) {
  const [tab, setTab] = useState('pulse')
  const [panel, setPanel] = useState(null) // null | 'settings' | 'help'
  const rootRef = useRef(null)
  const [narrow, setNarrow] = useState(false)
  // Frame orientation: the layout engine hands us the box (wide bottom
  // lane or narrow sidebar column) — we only READ its width, via the one
  // ResizeObserver on the root. The fixed-size glyph/canvas contract is
  // untouched: nothing here re-sizes a drawing surface, it just picks
  // which elements earn a 300px-wide frame.
  useEffect(() => {
    const el = rootRef.current
    if (!el) return undefined
    let ro = null
    const update = (w) => {
      const n = w < NARROW_W
      setNarrow((p) => (p === n ? p : n))
    }
    update(el.clientWidth)
    if (typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver((entries) => {
        const e = entries[entries.length - 1]
        update((e && e.contentRect && e.contentRect.width) || el.clientWidth)
      })
      ro.observe(el)
    }
    return () => { if (ro) ro.disconnect() }
  }, [])

  const metricsQ = useQuery({
    queryKey: [source, 'iyh-metrics'],
    queryFn: () => rest('/metrics'),
    refetchInterval: POLL_MS,
    retry: false,
  })
  const pulseQ = useQuery({
    queryKey: [source, 'iyh-pulse'],
    queryFn: () => rest('/pulse'),
    refetchInterval: PULSE_POLL_MS,
    retry: false,
  })
  const recentQ = useQuery({
    queryKey: [source, 'iyh-recent'],
    queryFn: () => rest('/recent?limit=' + IO_LINES),
    refetchInterval: POLL_MS,
    retry: false,
  })
  // Planned routes: poll 404-tolerant; a miss renders an empty state, never
  // an error toast. The catch folds any failure into "no data yet".
  const portraitQ = useQuery({
    queryKey: [source, 'iyh-portrait'],
    queryFn: () => rest('/portrait').catch(() => null),
    refetchInterval: POLL_MS,
    retry: false,
  })
  const driftQ = useQuery({
    queryKey: [source, 'iyh-drift'],
    queryFn: () => rest('/drift').catch(() => null),
    refetchInterval: POLL_MS,
    retry: false,
  })
  const settingsQ = useQuery({
    queryKey: [source, 'iyh-settings'],
    queryFn: () => rest('/settings').catch(() => null),
    refetchInterval: POLL_MS,
    retry: false,
  })

  const metrics = metricsQ.data
  let status
  if (metricsQ.isError && !metrics) {
    // Surface the real cause (404 = route never mounted into the gateway,
    // 401/403 = auth, 5xx = backend fault, no-status = connectivity) instead
    // of a catch-all "backend offline".
    status = backendErrorLabel(metricsQ.error) + ' · polling continues'
  } else if (!metrics) {
    status = 'waiting for first read…'
  } else {
    status = metrics.events + ' events · ' + fmtTokens(metrics.total_tokens) + ' tok · last ' + fmtClock(metrics.last_event)
  }
  const recent = (recentQ.data || []).slice(0, IO_LINES)
  const lastEv = recent.length > 0 ? recent[0] : null
  const nowSec = Date.now() / 1000
  const pet = petState(lastEv, nowSec)
  const ticker = tickerPhrase(lastEv, nowSec)

  // The single expand control: open the expanded workspace tab (stable id
  // -> re-fronting the existing tab instead of stacking duplicates; the
  // tab's own Close routes through the host teardown). Feature-detect on
  // older desktops and degrade gracefully.
  const wsCan = typeof host.openWorkspace === 'function'
  const openWs = () => {
    if (!wsCan) return
    host.openWorkspace(WS_WORKSPACE_ID, {
      title: "It's in Your Head",
      render: () => jsx(ExpandedWorkspace, { source: source, rest: rest }),
    })
  }

  const emptyState = (label, q) => jsxs('div', {
    className: 'flex flex-1 items-center justify-center',
    style: {
      fontSize: 10, color: 'var(--ui-text-quaternary)',
      fontFamily: 'var(--ui-font-mono, monospace)', letterSpacing: 1,
    },
    children: [q.data ? '[+] ' + label + ' live' : '[ . ] ' + label + ' · no data yet'],
  })

  const main =
    tab === 'pulse'
      ? jsxs('div', {
          className: 'flex flex-1 items-stretch',
          style: { gap: 8, minHeight: 0 },
          children: [
            jsx(PulseMatrix, { pulse: pulseQ.data }),
            // The fixed I/O-frame column only earns a wide frame: in a
            // narrow dock the matrix takes the full width instead.
            ...(!narrow
              ? [jsxs('div', {
                  className: 'flex flex-col justify-end overflow-hidden',
                  style: { width: IO_WIDTH, gap: 2 },
                  children: [
                    recent.length > 0
                      ? recent.map((ev, i) => jsx(IoLine, { key: i, ev }))
                      : jsx('span', {
                          style: { fontSize: 10, color: 'var(--ui-text-quaternary)' },
                          children: metrics && metrics.events > 0
                            ? 'I/O frames arrive with each agent call'
                            : 'no events yet — ticks on your next agent turn',
                        }),
                  ],
                })]
              : []),
          ],
        })
      : tab === 'portrait'
        ? jsx(PortraitView, { data: portraitQ.data, drift: driftQ.data })
        : jsx(DriftView, { data: driftQ.data, narrow })

  const popover = panel && jsxs('div', {
    style: {
      position: 'absolute', inset: 0, zIndex: 10,
      // --ui-popover / --ui-surface are ABSENT in the installed build
      // (verified: no definition in its styles.css). The host's real popover
      // token is --dt-popover (= color-mix 96% of --ui-bg-elevated, the same
      // value the app uses for dropdown/dialog popovers), with --ui-bg-elevated
      // and --dt-card behind it and a translucent color-mix as the last resort.
      background: 'var(--dt-popover, var(--ui-bg-elevated, color-mix(in srgb, var(--dt-card, #fcfcfc) 96%, transparent)))',
      border: '1px solid var(--ui-stroke-secondary, var(--dt-border, transparent))',
      color: 'var(--ui-text-secondary)',
      fontSize: 10, padding: '6px 8px', overflow: 'hidden',
      display: 'flex', flexDirection: 'column', gap: 4,
    },
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between',
        children: [
          jsx('span', {
            style: { fontWeight: 500, letterSpacing: 1 },
            children: panel === 'settings' ? 'SETTINGS · READ-ONLY' : 'ABOUT THIS STRIP',
          }),
          jsx('button', {
            type: 'button', title: 'Close', onClick: () => setPanel(null),
            style: {
              background: 'transparent', border: 'none', cursor: 'pointer',
              color: 'var(--ui-text-quaternary)', fontSize: 11,
            },
            children: '×',
          }),
        ],
      }),
      panel === 'settings'
        ? jsxs('div', {
            style: {
              display: 'flex', flexWrap: 'wrap', gap: '2px 12px',
              fontFamily: 'var(--ui-font-mono, monospace)',
              color: 'var(--ui-text-quaternary)',
            },
            children: SETTING_KEYS.map((k) => jsxs('span', {
              key: k,
              children: [
                jsx('span', { style: { color: 'var(--ui-text-secondary)' }, children: k }),
                ' = ' + settingValue(settingsQ.data, k),
              ],
            })),
          })
        : jsx('div', {
            style: { color: 'var(--ui-text-quaternary)' },
            children: 'PULSE semantic matrix · PORTRAIT user/agent tracks · DRIFT deltas · pet glyph = last tool event · ticker = tool + status only',
          }),
    ],
  })

  return jsxs('div', {
    ref: rootRef,
    className: 'flex h-full flex-col',
    style: {
      position: 'relative',
      padding: '4px 8px',
      gap: 3,
      borderTop: '1px solid var(--ui-stroke-secondary)',
      color: 'var(--ui-text-secondary)',
      overflow: 'hidden',
    },
    children: [
      jsxs('div', {
        className: 'flex items-center',
        style: { height: 22, fontSize: 11, gap: 6, flexShrink: 0 },
        children: [
          jsx('span', {
            // Narrow dock: the wordmark and status text yield to the tabs —
            // both move into this tooltip.
            title: narrow
              ? "It's in Your Head · pet: " + pet + ' · ' + status
              : 'pet: ' + pet,
            style: {
              fontFamily: 'var(--ui-font-mono, monospace)', fontSize: 10,
              color: 'var(--ui-text-quaternary)', width: 36, flexShrink: 0,
            },
            children: petGlyph(pet),
          }),
          ...(!narrow
            ? [jsx('span', { style: { fontWeight: 500, flexShrink: 0 }, children: "It's in Your Head" })]
            : []),
          jsxs('div', {
            className: 'flex items-center',
            style: { gap: 2, flexShrink: 0 },
            children: TABS.map((t) => jsx(TabButton, {
              key: t.id, tab: t, active: tab === t.id,
              onClick: () => setTab(t.id),
            })),
          }),
          ...(!narrow
            ? [jsx('span', {
                title: status,
                style: {
                  flex: 1, textAlign: 'right', fontSize: 10,
                  color: 'var(--ui-text-quaternary)',
                  whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                },
                children: status,
              })]
            : [jsx('span', { style: { flex: 1, flexShrink: 1 }, children: '' })]),
          jsx(IconButton, {
            title: 'Help — about this strip',
            label: '?',
            onClick: () => setPanel(panel === 'help' ? null : 'help'),
          }),
          jsx(IconButton, {
            title: 'Settings (read-only until /settings POST lands)',
            label: '⚙',
            onClick: () => setPanel(panel === 'settings' ? null : 'settings'),
          }),
          jsx(IconButton, {
            title: wsCan
              ? 'Open workspace — PULSE / PORTRAIT / DRIFT, expanded inspection'
              : 'Workspace unavailable — this desktop build predates host.openWorkspace',
            label: jsx(icons.Maximize, { size: 12 }),
            onClick: openWs,
            disabled: !wsCan,
          }),
        ],
      }),
      main,
      jsx('div', {
        style: {
          height: 18, flexShrink: 0, fontSize: 10, lineHeight: '18px',
          color: 'var(--ui-text-quaternary)',
          fontFamily: 'var(--ui-font-mono, monospace)',
          whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
          borderTop: '1px solid var(--ui-stroke-secondary)',
        },
        children: ticker,
      }),
      popover,
    ],
  })
}

export default {
  id: 'its-in-your-head',
  name: "It's in Your Head",
  defaultEnabled: true,
  register(ctx) {
    ctx.register({
      id: 'its-in-your-head',
      area: 'panes',
      title: "It's in Your Head",
      data: {
        placement: 'main',
        dock: { pane: 'workspace', pos: 'bottom' },  // owner slot 4: strip under the conversation
        height: '96px',                              // ~1/10-1/16 of a 1080p window
      },
      render: () => jsx(Strip, { source: ctx.source, rest: ctx.rest }),
    })

    // Live accelerator (optional): backend tick -> invalidate the polling
    // queries. The polling fallback above stays mandatory — ctx.socket is a
    // no-op on OAuth remotes, and the gateway event stream is the push path.
    host.onEvent('plugin.its-in-your-head.tick', () => {
      queryClient.invalidateQueries({ queryKey: [ctx.source] })
    })
  },
}
