// It's in Your Head — bottom-docked signal strip (heat cells + recent I/O ticks).
// Contract: only @hermes/plugin-sdk + react/jsx-runtime imports; jsx() calls, no JSX;
// theme vars only; ResizeObserver-tracked canvas; polling fallback is mandatory.
import {
  host,
  jsx,
  useQuery,
  queryClient,
} from '@hermes/plugin-sdk';
import { useEffect, useRef, useState } from 'react';

const EVENT = 'plugin.its-in-your-head.tick';

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
        dock: { pane: 'workspace', pos: 'bottom' },
        height: '96px',
      },
      render: () => jsx(SignalStrip, { source: ctx.source }),
    });

    // Live path: backend pushes ticks; poller below is the fallback.
    host.onEvent(EVENT, () => {
      queryClient.invalidateQueries({ queryKey: [ctx.source, 'heatmap'] });
      queryClient.invalidateQueries({ queryKey: [ctx.source, 'recent'] });
    });
  },
};

function SignalStrip({ source }) {
  const heat = useQuery({
    queryKey: [source, 'heatmap'],
    queryFn: () => hostFetch(ctx, '/heatmap').catch(() => null),
    refetchInterval: 5000,
  });
  const recent = useQuery({
    queryKey: [source, 'recent'],
    queryFn: () => hostFetch(ctx, '/recent').catch(() => null),
    refetchInterval: 5000,
  });
  const [errored, setErrored] = useState(false);

  const canvasRef = useRef(null);
  const wrapRef = useRef(null);

  // Canvas must track its container: panes resize constantly (sash drags, layout switches).
  useEffect(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;
    const resize = () => {
      canvas.width = Math.max(8, wrap.clientWidth);
      canvas.height = Math.max(8, wrap.clientHeight - 22);
      draw(canvas, heat.data);
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(wrap);
    return () => ro.disconnect();
  }, [heat.data]);

  useEffect(() => { setErrored(!heat.data); }, [heat.data]);

  const labels = (recent.data || []).slice(0, 6).map((r) =>
    `${r.type.replace('api.', '·').replace('turn.', '▸ ')}${r.total_tokens ? ` ${r.total_tokens}t` : ''}`
  );

  return jsx('div', {
    style: {
      display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden',
      fontFamily: 'inherit', fontSize: 11,
      color: 'var(--ui-text-secondary)',
    },
    children: [
      jsx('div', {
        ref: wrapRef,
        style: { flex: 1, minWidth: 0 },
        children: jsx('canvas', { ref: canvasRef, style: { width: '100%', height: '100%' } }),
      }),
      jsx('div', {
        style: {
          whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
          borderTop: '1px solid var(--ui-stroke-secondary)',
          padding: '2px 6px', letterSpacing: '0.04em',
          color: errored ? 'var(--ui-text-quaternary)' : 'var(--ui-text-secondary)',
        },
        children: errored
          ? 'backend off — showing cached strip'
          : (labels.length ? labels.join('   ') : 'no signals yet'),
      }),
    ],
  });
}

// Module-level ctx reference captured by the register() closure scope is not
// available here; use host.request as a stable stand-in for namespace fetches.
// (ctx.rest is the typed door; keep this helper tiny and swappable.)
function hostFetch(ctx, path) {
  return ctx ? ctx.rest(path) : Promise.reject(new Error('no ctx'));
}

function draw(canvas, data) {
  const g = canvas.getContext('2d');
  g.clearRect(0, 0, canvas.width, canvas.height);
  const css = getComputedStyle(document.documentElement);
  const accent = css.getPropertyValue('--ui-accent') || 'currentColor';
  const stroke = css.getPropertyValue('--ui-stroke-secondary') || 'currentColor';
  const cells = data ? Object.entries(data.cells || {}) : [];
  const hours = data ? data.hours || 24 : 24;
  const w = Math.max(4, Math.floor(canvas.width / hours));
  cells.forEach(([hour, c]) => {
    const x = hour * w;
    const calls = c.calls || 0;
    const errors = c.errors || 0;
    const turns = c.turns || 0;
    g.globalAlpha = Math.min(0.9, 0.15 + calls * 0.1);
    g.fillStyle = accent;
    g.fillRect(x, 0, w - 1, canvas.height * 0.8);
    if (errors > 0) {
      g.globalAlpha = 1;
      g.fillStyle = stroke;
      g.fillRect(x, canvas.height - 6, w - 1, 4);
    }
    if (turns > 0) {
      g.globalAlpha = 0.5;
      g.fillStyle = accent;
      g.fillRect(x, 0, w - 1, 3);
    }
  });
  g.globalAlpha = 1;
}
