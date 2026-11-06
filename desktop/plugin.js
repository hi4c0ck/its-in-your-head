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
import { useEffect, useRef } from 'react'
import { host, useQuery, queryClient } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const POLL_MS = 5000
const HEAT_POLL_MS = 15000
const IO_WIDTH = 220   // fixed I/O-frame column
const IO_LINES = 4

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

function Strip({ source, rest }) {
  const wrapRef = useRef(null)
  const canvasRef = useRef(null)

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

  // Latest heat data in a ref so the ResizeObserver callback (mounted once)
  // never draws a stale value after a data refresh.
  const heatRef = useRef(null)
  heatRef.current = heatQ.data

  // ResizeObserver: re-size the canvas attributes (not just CSS) on every
  // container change — sash drags, layout switches.
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
  }, [])

  // Re-draw when heat data arrives (query refetch / tick invalidation).
  useEffect(() => {
    const canvas = canvasRef.current
    if (canvas && canvas.width > 0) drawHeat(canvas, heatQ.data)
  }, [heatQ.data])

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

  return jsxs('div', {
    className: 'flex h-full flex-col',
    style: {
      padding: '4px 8px',
      gap: 3,
      borderTop: '1px solid var(--ui-stroke-secondary)',
      color: 'var(--ui-text-secondary)',
      overflow: 'hidden',
    },
    children: [
      jsxs('div', {
        className: 'flex items-baseline justify-between',
        style: { fontSize: 11 },
        children: [
          jsx('span', { style: { fontWeight: 500 }, children: "It's in Your Head" }),
          jsx('span', {
            style: { fontSize: 10, color: 'var(--ui-text-quaternary)' },
            children: status,
          }),
        ],
      }),
      jsxs('div', {
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
                style: { position: 'absolute', inset: 0, width: '100%', height: '100%' },
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
      }),
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
