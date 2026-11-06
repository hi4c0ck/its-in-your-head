// It's in Your Head — desktop half. INFRA SKELETON: pane registration + data
// plumbing shape is set; the strip UI itself is OWNER WORK (ARCHITECTURE.md #Desktop).
//
// Contract reminders (Desktop Plugin SDK):
// - imports: ONLY @hermes/plugin-sdk + react/jsx-runtime; jsx() calls, never JSX syntax.
// - theme vars only; panes already sit on the editor background.
// - canvas panes MUST track their container with a ResizeObserver (re-size the
//   width/height attributes, not just CSS) — panes resize constantly.
// - data: useQuery (the app's one React Query client) polling ctx.rest as the
//   fallback; host.onEvent('plugin.its-in-your-head.tick') is the live
//   accelerator — ctx.socket is a no-op on OAuth remotes, so the polling
//   fallback is mandatory, never optional.
import { host, jsx } from '@hermes/plugin-sdk';

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
      render: () => jsx(Strip, { source: ctx.source }),
    });

    // OWNER: live path — subscribe to the backend's tick and invalidate the queries:
    // host.onEvent('plugin.its-in-your-head.tick', () => {
    //   queryClient.invalidateQueries({ queryKey: [ctx.source, 'metrics'] });
    // });
  },
};

function Strip({ source }) {
  // OWNER: heat canvas (ResizeObserver + theme vars, resolved once via
  // getComputedStyle) + recent I/O tick frames.
  // Data plumbing shape (polling fallback):
  //   const heat = useQuery({
  //     queryKey: [source, 'heatmap'],
  //     queryFn: () => ctx.rest('/heatmap').catch(() => null),
  //     refetchInterval: 5000,
  //   });
  return jsx('div', {
    style: {
      color: 'var(--ui-text-secondary)',
      fontSize: 11,
      padding: '4px 8px',
      border: '1px dashed var(--ui-stroke-secondary)',
      borderRadius: 4,
    },
    children: "It's in Your Head — owner: wire the strip (ARCHITECTURE.md #Desktop)",
  });
}
