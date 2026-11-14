"""A 'plugin host' process for the lifecycle probe: the production
perception spawn path, in a form we can hard-kill to simulate the Hermes
host teardown that produced #94736.

It does exactly what the real host does when it starts ONNX:
  - (job mode) create the KILL_ON_CLOSE job object
  - spawn the worker (the stand-in, in the onnx venv image), assign it to the job
  - record it in the stale-worker registry (the reaper's input)
  - spawn the detached watchdog
  - print its PIDs as one JSON line, then stay alive until torn down

Usage: host_role.py <model_dir> <mode:job|nojob> [onnx_python]
  onnx_python defaults to $IYH_ONNX_PYTHON, else the local onnx venv, else
  the running interpreter.
"""
import os, sys, time, json, subprocess

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRATCH = os.path.dirname(os.path.abspath(__file__))

_LOCAL_ONNX_PY = ("C:/Users/user/AppData/Local/hermes/profiles/plugin-studio/environments/"
                  "its-in-your-head-onnx/gen-e76ec4e770054bf283dc35abba5a27af/venv/Scripts/python.exe")


def _interp():
    cand = (sys.argv[3] if len(sys.argv) > 3 else None) \
        or os.environ.get("IYH_ONNX_PYTHON") or _LOCAL_ONNX_PY
    return cand if os.path.isfile(cand) else sys.executable


sys.path.insert(0, REPO)
import perception.worker_client as wc  # noqa: E402


def main() -> None:
    model_dir = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "job"
    py = _interp()
    host_pid = os.getpid()

    # The real host creates the KILL_ON_CLOSE job ONCE (module global).
    job = wc._windows_kill_job() if mode == "job" else 0

    # A normal child of the host (only the watchdog is detached) -- matching
    # OnnxWorkerClient._spawn, so hard-killing the host orphans it and the
    # belts (job / watchdog / reaper) are what actually reap it.
    wenv = wc._worker_env()
    worker = subprocess.Popen(
        [py, os.path.join(SCRATCH, "worker_standin.py"), model_dir],
        env=wenv,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if job:
        wc._attach_to_kill_job(worker)          # belt 3: job object membership
    wc._record_worker(model_dir, worker.pid)    # belt 2: reaper registry
    watchdog = wc._spawn_watchdog(worker, owner_pid=host_pid)  # belt 1: primary

    print(json.dumps({"host": host_pid, "worker": worker.pid,
                      "watchdog": watchdog.pid if watchdog else 0,
                      "mode": mode, "job": bool(job), "worker_py": py}), flush=True)
    time.sleep(3000)  # host stays alive until the orchestrator tears it down


if __name__ == "__main__":
    main()
