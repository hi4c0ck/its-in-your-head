# It's in Your Head

Hermes Agent plugin (unified package): local prompt analytics + a small desktop
signal strip. **Current state: infrastructure skeleton — structure, contracts,
manifest, test harness, and install wiring are in place; the functional logic
(hook bodies, embedder, worker, routes, strip UI) is implemented per
`ARCHITECTURE.md` slot by slot.**

## Privacy

Everything stays on your machine: prompt text and metrics are stored locally
under `plugin-data/its-in-your-head/` (WAL SQLite). The embedding layer is
deliberately zero-network. Nothing leaves the machine.

## Install (manual)

Copy this folder into `~/.hermes/plugins/` (or the profile plugins root under
a named profile), add `its-in-your-head` to `plugins.enabled` in `config.yaml`
(equivalently: `hermes -p <profile> plugins enable its-in-your-head`), then
enable the desktop half in Capabilities > Plugins.
Verify: `hermes plugins doctor its-in-your-head --ci`.

## Dev install

`hermes://plugin/install?repo=hi4c0ck/its-in-your-head&enable=1`

## Tests

`<hermes venv python> -m pytest -q` (pytest.ini pins the importlib import mode
this package shape needs).
