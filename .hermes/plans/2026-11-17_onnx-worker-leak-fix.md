# ONNX worker leak guard — generation-aware redesign (2026-11-17)

Status: implemented on branch `fix/onnx-worker-leak`; commit `TBD`.
Gate results and the live process-table evidence are recorded at the bottom.

## Symptom (owner-reported)
"looks like we have a lot of processes now for a single plugin." Measured live:
**40 ONNX-family processes** (13 worker generations × [worker + ONNX child] +
13 detached watchdogs), ~900 MB working set.

## Root cause (evidence-driven, read-only investigation)
| Fact | Evidence |
|---|---|
| Owner of every worker = gateway core pid 380, which is long-lived | all `worker_watchdog.py 380 <pid>` |
| Registry held ONE entry keyed by owner pid (`380.json`) | `onnx-workers/380.json` -> latest worker; 12 prior workers untracked |
| Watchdog only fires when the OWNER dies | `worker_watchdog._windows_main` polls owner exit; owner 380 never died |
| Reaper only reaped workers whose OWNER was dead | old `_reap_stale_workers`: `if _pid_alive(owner): continue` |
| Each worker also spawns an ONNX-runtime child | 26 `worker.py` procs for 13 generations |

Net: neither guard layer's trigger condition ever became true for a
long-lived owner, so every new `OnnxWorkerClient` stacked another worker.

## Design (what changed and WHY — traceable)

### 1. Registry keyed by WORKER, not owner (`worker_client.py`)
`_record_worker` now writes `<model_dir>/onnx-workers/worker-<worker_pid>.json`
containing `{worker_pid, owner_pid, token, ts}`. N generations from one
owner produce N entries instead of overwriting one. Legacy `<owner_pid>.json`
entries are still read for migration.

### 2. Reaper retires prior generations of a live owner
`_reap_stale_workers(model_dir, current_pid=...)`: a worker is stale if its
owner is gone **OR** it belongs to this process but is not the current
generation (`wpid != current_pid`). `_spawn()` passes the just-spawned pid,
so each new worker retires the previous one immediately. This is the direct
fix for the immortal-owner stacking; the previous generation's detached
watchdog sees its worker die and self-exits.

### 3. Process-table sweep for UNTRACKED workers
`_sweep_unregistered_workers(model_dir, keep_pid, interpreter)` enumerates
the ACTUAL OS process table (`EnumProcesses` + `QueryFullProcessImageNameW`
on Windows; `/proc/<pid>/cmdline` on POSIX), matches the process IMAGE to
this profile's exact onnx venv interpreter (scoped so another profile's
worker is never touched), and terminates any live worker neither recorded
with a live owner nor equal to `keep_pid`. This closes the gap that left 12
workers invisible after registry overwrites. `_spawn()` calls it after
recording the new worker (protected via `keep_pid`). Identity is guarded
twice: exact-image enumeration + `_proc_is_our_worker`.

### 4. Watchdog hardened (release-flake fix)
`worker_watchdog._windows_main` now retries `OpenProcess(owner/worker)` for
~5 s. On a loaded box that call can transiently return NULL (Err 87); the
old code bailed with no guard, which is why
`test_watchdog_terminates_on_owner_death` failed 3/3 locally. If the owner
is already gone at startup, the watchdog terminates the orphan directly.

### 5. Clean shutdown drops the generation entry
`_kill()` / `shutdown()` call `_forget_worker(model_dir, worker_pid)` so a
crash-restart cannot mistake a recycled pid for our worker.

## Test strategy
- Fast guard tests stay in `tests/test_s8_onnx_worker.py` (mocked reaper
  logic, ABI layout, native calls); one assertion updated for the new key
  format.
- New `tests/test_s9_onnx_leak_prerelease.py` — 4 deep tests that drive the
  REAL process table and real kills: T1 N spawns in one owner -> 1 live
  worker; T2 untracked orphan swept; T3 process count flat over cycles;
  T4 registry keys are per-worker. Plus the existing real owner-death
  watchdog test is now `@pytest.mark.prerelease`.
- `pytest.ini` registers the `prerelease` marker; `tests/conftest.py` skips
  those tests unless `IYH_RUN_PRERELEASE=1`. Normal feature runs stay fast;
  release gate: `IYH_RUN_PRERELEASE=1 pytest -m prerelease`.

## Gate results (verbatim, this branch)
- Normal suite: `649 passed, 8 skipped, 1 warning in 53.48s`
- Pre-release suite: `IYH_RUN_PRERELEASE=1 pytest -q -m prerelease` ->
  `5 passed, 652 deselected, 1 warning in 5.19s`
- `tools/check_contracts.py`: `G5: CLEAN (checked C1 C2 C3 C4)`
- `python tools/temp_budget_check.py .pytest-basetemp --max-mb 100`: OK
- `node --input-type=module --check desktop/plugin.js`: OK (untouched)
- `hermes -p plugin-studio plugins doctor its-in-your-head --ci`: exit 0, 9 hooks
- private-import validator: NONE / NONE

## Live convergence evidence (2026-11-17)
Before fix: 40 ONNX-family processes (13 generations).
After the gateway hot-reloaded the patched plugin and next encoded: 4 —
one worker + its ONNX child + one live watchdog (+ one stale watchdog from a
transient test kernel that self-exits). Registry:
`worker-20664.json` -> `{worker_pid:20664, owner_pid:380, token:...}`.
No manual `taskkill` was used.

## Out of scope / notes
- The ONNX-runtime child per worker is expected (the runtime's own process);
  it dies with its parent worker.
- Job Object remains inert under this host (`ACCESS_DENIED`); watchdog +
  reaper + sweep carry the protection. No elevation, no core imports.
- No version tag cut — owner calls the release tag (repo rule: manifest
  bump + tag waits for owner stable-confirmation).
