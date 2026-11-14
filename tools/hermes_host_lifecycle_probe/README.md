# Hermes host-lifecycle probe (process-tree leak guard, layer certification)

A controlled, re-runnable diagnostic that drives the **real** `perception`
spawn path — Job Object (`KILL_ON_CLOSE`), the detached watchdog
(`worker_watchdog`), and the stale-registry reaper — through a real host
process, hard-kills that host (the teardown that produced the `hermes_state`
`#94736` "teardown/worker race" warning), and verifies the leak-guard invariants
from the live process tree, not from mocks.

## What it proves
- **Watchdog independence.** In `nojob` mode the host creates *no* Job Object
  and the reaper is not running, so the **detached watchdog is the only agent
  that can kill the worker**. A worker that is alive just after the host is
  hard-killed and then dies is direct proof the watchdog **survived the host
  teardown** and terminated its worker.
- **No duplication on restart.** A replacement host starts a **fresh** worker
  + watchdog; the dead pair is confirmed gone (the reaper reaped the stale
  entry). No host inherits or duplicates the previous worker/watchdog.
- **Exit-sequence evidence.** The old host's `Popen` is reaped (confirmed dead)
  *before* the replacement host starts.
- **`job` mode** additionally shows the Job Object `KILL_ON_CLOSE` + the
  watchdog acting together (the full belt layering).
- **Outer-Job containment (`outerjob` mode).** Puts the simulated host *inside*
  a containing `KILL_ON_CLOSE` Job Object (the case the real Hermes host may
  sit in) and proves the worker is reaped under BOTH teardown paths:
  (a) owner termination with the outer job still open → the outer job's
  `KILL_ON_CLOSE` has not fired, so the detached watchdog (a member, still
  alive) reaps the worker; (b) outer job closure → `KILL_ON_CLOSE` reaps the
  whole subtree (host + worker + watchdog) together. This covers the
  `CREATE_BREAKAWAY_FROM_JOB` concern: a detached child is still a member of
  the parent's job, but the worker stays contained either way.

## Run (Windows, onnx venv interpreter available)
```
python tools/hermes_host_lifecycle_probe/lifecycle_probe.py nojob      # watchdog-only proof
python tools/hermes_host_lifecycle_probe/lifecycle_probe.py job        # full belt
python tools/hermes_host_lifecycle_probe/lifecycle_probe.py outerjob   # host inside a containing job
python tools/hermes_host_lifecycle_probe/lifecycle_probe.py nojob --save  # also write evidence/nojob.json
```
The worker/host run under the onnx venv python so the worker's image carries
the `its-in-your-head-onnx` marker the reaper's identity check matches.
Override the interpreter with `IYH_ONNX_PYTHON` (or it falls back to the local
venv, then `sys.executable`).

`evidence/` holds three captured runs (2026-11-14): `nojob.json`, `job.json`,
`outerjob.json` — all PASS.

## Boundaries (honest scope)
- **Stand-in worker.** The ONNX model is not provisioned in this profile, so
  the worker is a lifecycle stand-in (same onnx-venv image, same start/idle/
  reap shape) — no inference. The mechanism under test is the lifecycle, not
  the embedding.
- **Controlled host, not the live gateway.** This hard-kills a test-spawned
  host, not the running Hermes gateway/plugin-host (doing that would 502 the
  live session for the gateway's ~30s respawn). It certifies the mechanism
  under a host death + a containing-job membership, not the gateway's exact
  job/breakaway configuration.
- **Outer-Job case is covered by `outerjob`.** The earlier open question — a
  detached watchdog is still a member of the host's `KILL_ON_CLOSE` job unless
  it breaks away — is now exercised: `outerjob` shows the worker stays
  contained under both owner-termination (watchdog reaps it, the outer job
  still open) and outer-job closure (`KILL_ON_CLOSE` reaps the whole subtree).
  The Job Object is retained as an independent belt (owner decision); the
  probe certifies the containment, it does not justify removing the Job Object.
