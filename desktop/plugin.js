// It's in Your Head — desktop half: a 96px signal strip under the conversation.
//
// Contract (Desktop Plugin SDK):
// - imports: ONLY @hermes/plugin-sdk + react / react/jsx-runtime; jsx() calls,
//   never JSX syntax (the file loads uncompiled).
// - theme vars only — the pane already sits on the app's editor background;
//   canvas colors resolve via getComputedStyle, no hardcoded color values.
// - canvas panes MUST track their container with a ResizeObserver (re-size the
//   width/height attributes, not just CSS).
// - data: useQuery (the app's one React Query client) polling ctx.rest — the
//   mandatory fallback; host.onEvent('plugin.its-in-your-head.tick') is the
//   live accelerator (ctx.socket is a no-op on OAuth remotes).
// - layout budget 96px: 22 header/tabs, 48 main, 18 ticker, 8 separators.
//   Overflow goes to the settings popover, never in-pane scrolling.
import { useEffect, useRef, useState } from 'react'
import { host, useQuery, queryClient } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const POLL_MS = 5000
const HEAT_POLL_MS = 15000
const IO_WIDTH = 220   // fixed I/O-frame column
const IO_LINES = 4
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

// Canvas can't use CSS var() strings — resolve the app's theme vars once per
// draw. The danger tint probes a small set of semantic vars; if the active
// theme defines none, it falls back to the accent (never a hardcoded color).
function resolveColors(canvas) {
  const cs = getComputedStyle(canvas)
  const accent = cs.getPropertyValue('--ui-accent').trim() || 'currentColor'
  const stroke = cs.getPropertyValue('--ui-stroke-secondary').trim() || 'transparent'
  let danger = ''
  for (const v of ['--destructive', '--ui-danger', '--ui-error', '--danger']) {
    danger = cs.getPropertyValue(v).trim()
    if (danger) break
  }
  return { accent, stroke, danger: danger || accent }
}

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

function drawHeat(canvas, heat) {
  const c2d = canvas.getContext('2d')
  if (!c2d) return
  c2d.clearRect(0, 0, canvas.width, canvas.height)
  const colors = resolveColors(canvas)
  const dpr = window.devicePixelRatio || 1
  const hours = (heat && heat.hours) || 24
  const cells = (heat && heat.cells) || {}
  const pad = 2 * dpr
  const laneW = Math.max(1, canvas.width - 2 * pad)
  const laneH = Math.max(1, canvas.height - 2 * pad)
  const cellW = laneW / hours
  const gap = Math.max(dpr, cellW * 0.12)
  let maxCount = 0
  const counts = {}
  for (let i = 0; i < hours; i++) {
    const c = cells[String(i)] || { turns: 0, calls: 0, errors: 0 }
    counts[i] = c
    maxCount = Math.max(maxCount, c.turns + c.calls)
  }
  for (let i = 0; i < hours; i++) {
    const c = counts[i]
    const x = pad + i * cellW
    const w = Math.max(gap, cellW - 2 * gap)
    const activity = c.turns + c.calls
    if (activity > 0) {
      c2d.fillStyle = colors.accent
      c2d.globalAlpha = 0.12 + 0.88 * (activity / maxCount)
      c2d.fillRect(x + gap, pad, w, laneH)
    }
    if (c.errors > 0) {  // danger tick under the cell
      c2d.fillStyle = colors.danger
      c2d.globalAlpha = 0.9
      c2d.fillRect(x + gap, pad + laneH - 2 * dpr, w, 2 * dpr)
    }
    c2d.globalAlpha = 1
  }
}

function IoLine({ ev }) {
  let tag, body, dot
  // DOM dots use theme vars directly (var() with a nested fallback — no
  // hardcoded color values); only the canvas needs getComputedStyle.
  if (ev.type === 'turn') {
    tag = 'U'; body = ev.model || 'turn'; dot = 'var(--ui-stroke-secondary)'
  } else if (ev.type === 'api') {
    tag = 'A'
    body = fmtTokens(ev.total_tokens) + ' tok' + (ev.finish_reason ? ' · ' + ev.finish_reason : '')
    dot = 'var(--ui-accent)'
  } else if (ev.type === 'error') {
    tag = 'E'; body = ev.status_code ? String(ev.status_code) : 'error'
    dot = 'var(--destructive, var(--ui-accent))'
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

function IconButton({ title, label, onClick }) {
  return jsx('button', {
    type: 'button',
    title,
    onClick,
    style: {
      background: 'transparent', border: 'none', cursor: 'pointer',
      color: 'var(--ui-text-quaternary)', fontSize: 12, padding: '0 3px', height: 22,
    },
    children: label,
  })
}

function Strip({ source, rest }) {
  const wrapRef = useRef(null)
  const canvasRef = useRef(null)
  const [tab, setTab] = useState('pulse')
  const [panel, setPanel] = useState(null) // null | 'settings' | 'help'

  const metricsQ = useQuery({
    queryKey: [source, 'iyh-metrics'],
    queryFn: () => rest('/metrics'),
    refetchInterval: POLL_MS,
    retry: false,
  })
  const heatQ = useQuery({
    queryKey: [source, 'iyh-heatmap'],
    queryFn: () => rest('/heatmap?window=24h'),
    refetchInterval: HEAT_POLL_MS,
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

  // Latest heat data in a ref so the ResizeObserver callback (mounted once)
  // never draws a stale value after a data refresh.
  const heatRef = useRef(null)
  heatRef.current = heatQ.data

  // ResizeObserver: re-size the canvas attributes (not just CSS) on every
  // container change — sash drags, layout switches, tab remounts.
  useEffect(() => {
    const wrap = wrapRef.current
    const canvas = canvasRef.current
    if (!wrap || !canvas) return
    const dpr = window.devicePixelRatio || 1
    const fit = () => {
      const r = wrap.getBoundingClientRect()
      canvas.width = Math.max(1, Math.round(r.width * dpr))
      canvas.height = Math.max(1, Math.round(r.height * dpr))
      drawHeat(canvas, heatRef.current)
    }
    const ro = new ResizeObserver(fit)
    ro.observe(wrap)
    return () => ro.disconnect()
  }, [tab])

  // Re-draw when heat data arrives (query refetch / tick invalidation).
  useEffect(() => {
    const canvas = canvasRef.current
    if (canvas && canvas.width > 0) drawHeat(canvas, heatQ.data)
  }, [heatQ.data, tab])

  const metrics = metricsQ.data
  let status
  if (metricsQ.isError && !metrics) {
    status = 'backend offline — polling continues'
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
            jsxs('div', {
              ref: wrapRef,
              className: 'relative flex-1',
              style: { minWidth: 0 },
              children: [
                jsx('canvas', {
                  ref: canvasRef,
                  // Sizing comes from the ResizeObserver width/height attributes
                  // (inset stretches the box); no CSS width/height here.
                  style: { position: 'absolute', inset: 0 },
                }),
                !heatQ.data && jsx('span', {
                  style: {
                    position: 'absolute', inset: 0, display: 'flex',
                    alignItems: 'center', justifyContent: 'center',
                    fontSize: 10, color: 'var(--ui-text-quaternary)',
                  },
                  children: '24h heat lane',
                }),
              ],
            }),
            jsxs('div', {
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
            }),
          ],
        })
      : tab === 'portrait'
        ? emptyState('PORTRAIT', portraitQ)
        : emptyState('DRIFT', driftQ)

  const popover = panel && jsxs('div', {
    style: {
      position: 'absolute', inset: 0, zIndex: 10,
      background: 'var(--ui-popover, var(--ui-surface, transparent))',
      border: '1px solid var(--ui-stroke-secondary)',
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
            children: 'PULSE heat lane · PORTRAIT/DRIFT pending backend · pet glyph = last tool event · ticker = tool + status only',
          }),
    ],
  })

  return jsxs('div', {
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
            title: 'pet: ' + pet,
            style: {
              fontFamily: 'var(--ui-font-mono, monospace)', fontSize: 10,
              color: 'var(--ui-text-quaternary)', width: 36, flexShrink: 0,
            },
            children: petGlyph(pet),
          }),
          jsx('span', { style: { fontWeight: 500, flexShrink: 0 }, children: "It's in Your Head" }),
          jsxs('div', {
            className: 'flex items-center',
            style: { gap: 2, flexShrink: 0 },
            children: TABS.map((t) => jsx(TabButton, {
              key: t.id, tab: t, active: tab === t.id,
              onClick: () => setTab(t.id),
            })),
          }),
          jsx('span', {
            style: {
              flex: 1, textAlign: 'right', fontSize: 10,
              color: 'var(--ui-text-quaternary)',
              whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
            },
            children: status,
          }),
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
