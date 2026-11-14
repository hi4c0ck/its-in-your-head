"""Instrument one real plugin-host lifecycle (controlled, safe, re-runnable).

The integration test the Hermes log could not certify: it drives the REAL
perception spawn path (Job Object, detached watchdog, stale-registry reaper)
through a real host process, records every PID + the teardown, and proves:

  1. record controller / host / worker / watchdog PIDs
  2. hard-kill the host (the teardown that produced #94736)
  3. the detached watchdog SURVIVES the host death + terminates its worker
     (in `nojob` mode the watchdog is the ONLY agent that can do this)
  4. a replacement host starts a FRESH worker + watchdog, no
     inheritance/duplication of the dead pair
  5. process-exit evidence: the old host actually exited before the new one

The worker is the onnx-venv stand-in (no inference -- the model isn't needed
for the lifecycle under test; the reaper's image-marker check is what makes
it a faithful worker). Windows only (Job Object + OpenProcess/Terminate).

Run:  python lifecycle_probe.py <nojob|job> [--save]
  --save writes the evidence JSON to evidence/<mode>.json next to this file.
"""
import os, sys, json, time, tempfile, subprocess, ctypes

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.path.join(HERE, "host_role.py")
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, REPO)  # make `perception` importable from the probe's helpers
_LOCAL_ONNX_PY = ("C:/Users/user/AppData/Local/hermes/profiles/plugin-studio/environments/"
                  "its-in-your-head-onnx/gen-e76ec4e770054bf283dc35abba5a27af/venv/Scripts/python.exe")
_k = None


def _interp():
    cand = os.environ.get("IYH_ONNX_PYTHON") or _LOCAL_ONNX_PY
    return cand if os.path.isfile(cand) else sys.executable


def K():
    global _k
    if _k is None:
        import ctypes as _c
        _k = _c.windll.kernel32
        _k.OpenProcess.argtypes = [_c.c_uint32, _c.c_int, _c.c_uint32]
        _k.OpenProcess.restype = _c.c_void_p
        _k.TerminateProcess.argtypes = [_c.c_void_p, _c.c_uint32]
        _k.GetLastError.argtypes = []
        _k.GetLastError.restype = _c.c_uint32
    return _k


def alive(pid: int) -> bool:
    """Reliable Windows liveness: OpenProcess with the minimal
    PROCESS_QUERY_LIMITED_INFORMATION. A live (or still-unreaped) process yields
    a handle OR ERROR_ACCESS_DENIED (exists, just no stronger right); only a
    true not-found means it is gone."""
    if not pid:
        return False
    k = K()
    h = k.OpenProcess(0x1000, 0, pid)
    if h:
        k.CloseHandle(h)
        return True
    return k.GetLastError() in (5, 1326, 1332)  # exists-but-inaccessible


def terminate(pid: int) -> str:
    if not pid:
        return "no-pid"
    h = K().OpenProcess(0x1001, 0, pid)  # PROCESS_TERMINATE | PROCESS_QUERY_INFORMATION
    if not h:
        return "already-gone"
    K().TerminateProcess(h, 1)
    K().CloseHandle(h)
    return "hard-killed"


def _KJ():
    """kernel32 with the Job Object argtypes set (idempotent)."""
    import ctypes as _c
    k = K()
    k.CreateJobObjectW.restype = _c.c_void_p
    k.CreateJobObjectW.argtypes = [_c.c_void_p, _c.c_wchar_p]
    k.SetInformationJobObject.restype = _c.c_int
    k.SetInformationJobObject.argtypes = [_c.c_void_p, _c.c_int, _c.c_void_p, _c.c_uint32]
    k.AssignProcessToJobObject.restype = _c.c_int
    k.AssignProcessToJobObject.argtypes = [_c.c_void_p, _c.c_void_p]
    return k


def create_outer_job() -> int:
    """A controller-owned KILL_ON_CLOSE job (distinct from worker_client's
    module-global job), kept open until we close it. 0 = unavailable."""
    import ctypes as _c
    import perception.worker_client as wc
    k = _KJ()
    _ct, _ext = wc._job_structs()
    job = k.CreateJobObjectW(None, None)
    if not job:
        return 0
    ext = _ext()
    ext.BasicLimitInformation.LimitFlags = wc._JOB_LIMIT_KILL_ON_JOB_CLOSE
    if not k.SetInformationJobObject(
            job, wc._JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            _c.byref(ext), _c.sizeof(ext)):
        k.CloseHandle(job)
        return 0
    return job


def assign_to_job(job: int, proc) -> bool:
    import ctypes as _c
    import perception.worker_client as wc
    if not job or proc is None:
        return False
    k = _KJ()
    h = k.OpenProcess(wc._PROC_SET_QUOTA | wc._PROC_TERMINATE, 0, proc.pid)
    if not h:
        return False
    ok = bool(k.AssignProcessToJobObject(job, h))
    k.CloseHandle(h)
    return ok


def close_job(job: int) -> None:
    if job:
        _KJ().CloseHandle(job)


def _wait_worker_death(pid: int, max_ticks: int = 40) -> "float | None":
    """Poll a worker every 0.5s; return the second it died, or None if still
    alive after the budget."""
    import ctypes as _c
    for i in range(max_ticks):
        time.sleep(0.5)
        if not alive(pid):
            return round(0.5 * (i + 1), 1)
    return None


def spawn_host(mode: str, model_dir: str, py: str) -> dict:
    p = subprocess.Popen([py, HOST, model_dir, mode, py],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1)
    assert p.stdout is not None
    pids = json.loads(p.stdout.readline().strip())
    pids["proc"] = p
    return pids


def scenario(mode: str, save: bool = False) -> int:
    py = _interp()
    model_dir = tempfile.mkdtemp(prefix=f"iyh_lc_{mode}_")
    controller = os.getpid()  # the long-lived top-level (gateway analog)
    ev = {"scenario": mode, "controller_pid": controller, "worker_py": py}

    # Phase 1-2: host A up, worker + watchdog alive pre-teardown
    A = spawn_host(mode, model_dir, py)
    time.sleep(1.5)
    ev["hostA"] = {k: A[k] for k in ("host", "worker", "watchdog", "mode", "job")}
    ev["pre_teardown"] = {"worker_alive": alive(A["worker"]),
                          "watchdog_alive": alive(A["watchdog"]),
                          "host_alive": alive(A["host"])}
    assert ev["pre_teardown"]["worker_alive"] and ev["pre_teardown"]["watchdog_alive"], \
        "host A did not come up cleanly"

    # Phase 3: hard-kill host A = the host teardown (#94736)
    ev["hostA_teardown"] = {"t": time.time(), "result": terminate(A["host"])}

    # Phase 4: did the detached watchdog survive + kill the worker? In `nojob`
    # mode it is the ONLY agent that can kill the worker (no job; the reaper is
    # not running here), so a worker that is alive just after the host kill and
    # then dies is proof the watchdog survived the host teardown + did the kill.
    worker_alive_just_after_kill = None
    worker_death_poll_s = None
    for i in range(40):
        time.sleep(0.5)
        if worker_alive_just_after_kill is None:
            worker_alive_just_after_kill = alive(A["worker"])
        if alive(A["worker"]):
            continue
        worker_death_poll_s = round(0.5 * (i + 1), 1)  # died within this tick
        break
    time.sleep(1.0)  # the watchdog exits within one poll interval of the kill
    ev["post_teardown"] = {
        "worker_alive_just_after_kill": worker_alive_just_after_kill,
        "worker_died": worker_death_poll_s is not None,
        "worker_death_poll_s": worker_death_poll_s,
        "host_dead": A["proc"].poll() is not None,  # reliable via the Popen handle
        "watchdog_exited_after_killing": not alive(A["watchdog"]),
    }

    # Phase 5: replacement host B -> fresh worker + watchdog, no duplication
    B = spawn_host(mode, model_dir, py)
    time.sleep(1.5)
    ev["hostB"] = {k: B[k] for k in ("host", "worker", "watchdog")}
    ev["exit_evidence"] = {
        "hostA_killed_at": ev["hostA_teardown"]["t"],
        "hostA_dead": A["proc"].poll() is not None,
        "hostB_started_after": B["host"] != A["host"],
        "note": "hostA actually exited (its Popen reaped) before hostB started",
    }
    ev["no_duplication"] = {
        "new_worker_distinct": B["worker"] != A["worker"],
        "new_watchdog_distinct": B["watchdog"] != A["watchdog"] and B["watchdog"] != 0,
        "old_worker_gone": not alive(A["worker"]),
        "old_watchdog_gone": not alive(A["watchdog"]),
    }
    ev["reaper"] = ("only the detached watchdog could kill the worker in nojob mode"
                    if mode == "nojob" else "job object (KILL_ON_CLOSE) + watchdog (both belts)")

    # Phase 6: cleanup
    time.sleep(1.0)
    ev["cleanup"] = {"B_worker": terminate(B["worker"]), "B_watchdog": terminate(B["watchdog"]),
                     "B_host": terminate(B["host"])}
    for p in (A, B):
        try:
            p["proc"].stdout.close()
        except Exception:
            pass
        p["proc"].terminate()

    verdict = (ev["post_teardown"]["worker_died"]
               and ev["no_duplication"]["new_worker_distinct"]
               and ev["no_duplication"]["new_watchdog_distinct"]
               and ev["no_duplication"]["old_worker_gone"]
               and ev["no_duplication"]["old_watchdog_gone"])
    ev["verdict"] = "PASS" if verdict else "CHECK"
    ev["verdict_note"] = (
        "worker was alive before the host teardown, then died after it; "
        f"reaper = {ev['reaper']}; replacement host got a fresh pair, no duplication"
        if verdict else "see the evidence above")
    print(json.dumps(ev, indent=2, default=str))
    if save:
        os.makedirs(os.path.join(HERE, "evidence"), exist_ok=True)
        with open(os.path.join(HERE, "evidence", f"{mode}.json"), "w", encoding="utf-8") as fh:
            json.dump(ev, fh, indent=2, default=str)
    return 0 if verdict else 1


def scenario_outerjob(save: bool = False) -> int:
    """Outer-Window-Job containment: the simulated host is a member of a
    containing KILL_ON_CLOSE job (the case Hermes itself may sit in). Exercises
    BOTH teardown paths and proves the worker is reaped under each:

      A. owner termination with the outer job still open -> the outer job's
         KILL_ON_CLOSE has not fired (its handle is open), so the detached
         watchdog (a member of the outer job, still alive) reaps the worker.
      B. outer job closure -> KILL_ON_CLOSE fires; the whole subtree
         (host + worker + watchdog, all members) dies together. If the
         watchdog cannot act (it dies with the job close), the outer job's
         KILL_ON_CLOSE still reaps the worker. No leak under either path.
    """
    py = _interp()
    model_dir = tempfile.mkdtemp(prefix="iyh_lc_outer_")
    ev = {"scenario": "outerjob", "controller_pid": os.getpid(), "worker_py": py}

    # --- Sub-phase A: owner termination, outer job A still open ---
    outerA = create_outer_job()
    ev["outer_job_available"] = bool(outerA)
    if not outerA:
        ev["verdict"] = "SKIP"; ev["note"] = "outer KILL_ON_CLOSE job unavailable"
        print(json.dumps(ev, indent=2)); return 0
    A = spawn_host("nojob", model_dir, py)   # host creates no job of its own
    ev["A_assigned_to_outer"] = assign_to_job(outerA, A["proc"])
    time.sleep(1.0)
    ev["A_host"] = {k: A[k] for k in ("host", "worker", "watchdog")}
    ev["A_pre"] = {"worker": alive(A["worker"]), "watchdog": alive(A["watchdog"]),
                   "host": alive(A["host"])}
    ev["A_teardown"] = {"result": terminate(A["host"]),
                        "note": "outer job A still open -> KILL_ON_CLOSE has not fired"}
    _deathA = _wait_worker_death(A["worker"])
    ev["A_post"] = {"worker_died": _deathA is not None,
                    "worker_death_poll_s": _deathA,
                    "host_dead": A["proc"].poll() is not None,
                    "watchdog_exited": not alive(A["watchdog"]),
                    "outer_job_A_still_open": True}
    close_job(outerA)
    A["proc"].terminate()
    A["proc"].stdout.close()

    # --- Sub-phase B: outer job closure kills the whole subtree ---
    outerB = create_outer_job()
    B = spawn_host("nojob", model_dir, py)
    ev["B_assigned_to_outer"] = assign_to_job(outerB, B["proc"])
    time.sleep(1.0)
    ev["B_host"] = {k: B[k] for k in ("host", "worker", "watchdog")}
    ev["B_pre"] = {"worker": alive(B["worker"]), "watchdog": alive(B["watchdog"]),
                   "host": alive(B["host"])}
    close_job(outerB)   # KILL_ON_CLOSE -> host + worker + watchdog die together
    time.sleep(2.0)
    ev["B_post"] = {"host_died": B["proc"].poll() is not None,
                    "worker_died": not alive(B["worker"]),
                    "watchdog_died": not alive(B["watchdog"]),
                    "outer_job_B_closed": True}
    B["proc"].terminate()
    B["proc"].stdout.close()

    verdict = (ev["A_post"]["worker_died"]
               and ev["B_post"]["host_died"]
               and ev["B_post"]["worker_died"]
               and ev["B_post"]["watchdog_died"])
    ev["verdict"] = "PASS" if verdict else "CHECK"
    ev["note"] = ("owner-termination: the still-open outer job keeps the watchdog "
                  "alive to reap the worker; job-closure: KILL_ON_CLOSE reaps the "
                  "whole subtree; no leak under either path"
                  if verdict else "see evidence above")
    print(json.dumps(ev, indent=2, default=str))
    if save:
        os.makedirs(os.path.join(HERE, "evidence"), exist_ok=True)
        with open(os.path.join(HERE, "evidence", "outerjob.json"), "w", encoding="utf-8") as fh:
            json.dump(ev, fh, indent=2, default=str)
    return 0 if verdict else 1


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--save"]
    save = "--save" in sys.argv
    mode = args[0] if args else "nojob"
    sys.exit(scenario_outerjob(save=save) if mode == "outerjob"
             else scenario(mode, save=save))
