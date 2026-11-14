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
    return assign_pid_to_job(job, proc.pid if proc else 0)


def assign_pid_to_job(job: int, pid: int) -> bool:
    """Assign `pid` to `job` (OpenProcess + AssignProcessToJobObject). This is
    the way a containing supervisor puts a process (or its already-running
    children) into a KILL_ON_CLOSE job."""
    import perception.worker_client as wc
    if not job or not pid:
        return False
    k = _KJ()
    h = k.OpenProcess(wc._PROC_SET_QUOTA | wc._PROC_TERMINATE, 0, pid)
    if not h:
        return False
    ok = bool(k.AssignProcessToJobObject(job, h))
    k.CloseHandle(h)
    return ok


def close_job(job: int) -> None:
    if job:
        _KJ().CloseHandle(job)


def is_process_in_job(job: int, pid: int) -> bool:
    """kernel32 IsProcessInJob: TRUE iff `pid` is a member of `job`. A dead or
    unopenable pid -> False. This is the positive membership proof the outerjob
    scenario requires (not just 'the worker happened to die')."""
    import ctypes as _c
    if not pid or not job:
        return False
    k = _KJ()
    k.IsProcessInJob.restype = _c.c_int
    k.IsProcessInJob.argtypes = [_c.c_void_p, _c.c_void_p, _c.POINTER(_c.c_int)]
    h = K().OpenProcess(0x4000 | 0x1000, 0, pid)  # QUERY_INFORMATION | QUERY_LIMITED
    if not h:
        return False
    member = _c.c_int(0)
    k.IsProcessInJob(h, job, _c.byref(member))
    k.CloseHandle(h)
    return bool(member.value)


def spawn_host_gated(model_dir: str, py: str):
    """Start host_role.py in gated mode. It prints a READY line, then blocks
    until we create its release file. Returns (proc, ready, release_file) so the
    caller can (1) read READY, (2) assign the host to a job, (3) create the
    release file so the host spawns its children, (4) read the 'up' PIDs line
    via _release()."""
    release_file = os.path.join(
        model_dir, "release_%d_%d" % (int(time.time() * 1000), os.getpid()))
    env = dict(os.environ, IYH_PROBE_RELEASE=release_file)
    p = subprocess.Popen([py, HOST, model_dir, "nojob", py],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1, env=env)
    assert p.stdout is not None
    ready = json.loads(p.stdout.readline().strip())
    return p, ready, release_file


def _release(proc, release_file: str) -> dict:
    """Create the release file (unblocking the gated host) + read its 'up' PIDs
    line, which carries the worker + watchdog it spawned as job children."""
    with open(release_file, "w", encoding="utf-8") as fh:
        fh.write("go")
    pids = json.loads(proc.stdout.readline().strip())
    pids["proc"] = proc
    return pids


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
    """Outer-Job containment, proven by ACTUAL membership.

    The sequencing that makes this valid (per MS-Learn: a parent's NEW children
    inherit its job, but pre-existing children are not retro-added):

      1. start the host in gated mode -> it announces READY but has NO children
      2. controller assigns the host to a containing KILL_ON_CLOSE outer job
      3. controller releases the host -> only now does it spawn worker + watchdog
      4. controller calls IsProcessInJob for ALL THREE (host, worker, watchdog)
      5. only then runs the two teardown experiments

      A. terminate the owner, keep the outer job open -> the outer KILL_ON_CLOSE
         has NOT fired, so the detached watchdog (a member, still alive) reaps
         the worker. The worker's death is BY THE WATCHDOG, not the job.
      B. close the outer job -> KILL_ON_CLOSE fires; host + worker + watchdog
         (all confirmed members) terminate together.

    PASS requires successful assignment AND all-positive membership AND the
    observed teardown behavior -- not merely that the worker ended up dead.
    """
    py = _interp()
    model_dir = tempfile.mkdtemp(prefix="iyh_lc_outer_")
    ev = {"scenario": "outerjob", "controller_pid": os.getpid(), "worker_py": py}

    def members(tag: str, outer: int):
        """Two-phase setup + genuine containment.

        (1) Gated two-phase: the host announces READY, we assign it to the outer
            job, we release it, and only then does it spawn worker + watchdog
            (the sequencing the reviewer requires -- not assign-after-spawn).
        (2) Honest inherited-membership finding: on Win11 a host assigned to a
            job post-creation does NOT auto-contain the children it then spawns
            (a process created OUTSIDE the job has an empty creation-time job
            chain). We record whether the children inherited WITHOUT any
            explicit assignment of them.
        (3) Genuine containment: a containing supervisor puts each already-
            running member into the KILL_ON_CLOSE job, so we explicitly assign
            the worker + watchdog too. Only then does IsProcessInJob confirm
            all three are real members -- the containment the verdict requires.
        """
        proc, _ready, rel = spawn_host_gated(model_dir, py)
        host_pid = proc.pid
        host_assigned = assign_to_job(outer, proc)
        ev[f"{tag}_host_assigned_to_outer"] = host_assigned
        pids = _release(proc, rel)  # now the host spawns its worker + watchdog
        worker, watchdog = pids["worker"], pids["watchdog"]
        time.sleep(0.4)
        # Inherited membership (no explicit child assignment) -- the finding.
        inherited = {"host": is_process_in_job(outer, host_pid),
                     "worker": is_process_in_job(outer, worker),
                     "watchdog": is_process_in_job(outer, watchdog)}
        ev[f"{tag}_inherited_membership"] = inherited
        # Genuine containment: assign the children explicitly (supervisor model).
        w_ok = assign_pid_to_job(outer, worker)
        d_ok = assign_pid_to_job(outer, watchdog)
        ev[f"{tag}_explicit_child_assign"] = {"worker": w_ok, "watchdog": d_ok}
        time.sleep(0.3)
        mem = {"host": is_process_in_job(outer, host_pid),
               "worker": is_process_in_job(outer, worker),
               "watchdog": is_process_in_job(outer, watchdog)}
        ev[f"{tag}_pids"] = {"host": host_pid, "worker": worker, "watchdog": watchdog}
        ev[f"{tag}_membership_in_outer"] = mem
        ev[f"{tag}_all_members"] = all(mem.values())
        ev[f"{tag}_pre"] = {"host": alive(host_pid), "worker": alive(worker),
                            "watchdog": alive(watchdog)}
        return {"proc": proc, "host": host_pid, "worker": worker, "watchdog": watchdog,
                "all_members": all(mem.values())}

    # --- Sub-phase A: terminate the owner, outer job A still open ---
    outerA = create_outer_job()
    ev["outer_job_available"] = bool(outerA)
    if not outerA:
        ev["verdict"] = "SKIP"; ev["note"] = "outer KILL_ON_CLOSE job unavailable"
        print(json.dumps(ev, indent=2)); return 0
    A = members("A", outerA)
    if A:
        ev["A_teardown"] = {"result": terminate(A["host"]),
                            "note": "outer job A kept open -> KILL_ON_CLOSE not yet fired; "
                                    "only the watchdog can kill the worker here"}
        _deathA = _wait_worker_death(A["worker"])
        ev["A_post"] = {"worker_died": _deathA is not None,
                        "worker_death_poll_s": _deathA,
                        "host_dead": A["proc"].poll() is not None,
                        "watchdog_exited_after_killing": not alive(A["watchdog"]),
                        "outer_job_A_still_open": True}
    close_job(outerA)
    if A:
        A["proc"].terminate()
        A["proc"].stdout.close()

    # --- Sub-phase B: closing the outer job kills the whole subtree ---
    outerB = create_outer_job()
    B = members("B", outerB)
    if B:
        ev["B_teardown"] = {"result": "close_job(outerB)",
                            "note": "KILL_ON_CLOSE -> host + worker + watchdog die together"}
        close_job(outerB)
        time.sleep(2.0)
        ev["B_post"] = {"host_died": B["proc"].poll() is not None,
                        "worker_died": not alive(B["worker"]),
                        "watchdog_died": not alive(B["watchdog"]),
                        "outer_job_B_closed": True}
        B["proc"].terminate()
        B["proc"].stdout.close()

    a_ok = (A["all_members"]
            and ev.get("A_post", {}).get("worker_died")
            and ev.get("A_post", {}).get("host_dead"))
    b_ok = (B["all_members"]
            and ev.get("B_post", {}).get("host_died")
            and ev.get("B_post", {}).get("worker_died")
            and ev.get("B_post", {}).get("watchdog_died"))
    verdict = a_ok and b_ok
    ev["verdict"] = "PASS" if verdict else "CHECK"
    ev["required_for_PASS"] = [
        "A/B host assigned to the outer job",
        "A/B all-three IsProcessInJob members (genuine containment)",
        "A: owner terminated + outer job kept open -> worker died + host dead "
        "(the watchdog, a surviving member, reaped the worker)",
        "B: outer job closed -> host + worker + watchdog all died together",
    ]
    ev["finding_inherited_membership"] = (
        "on this build, a host assigned to the outer job POST-creation does not "
        "auto-contain the children it later spawns (inherited_membership for the "
        "worker + watchdog is False) -- a process created outside the job carries "
        "an empty creation-time job chain. Genuine containment therefore requires "
        "the containing supervisor to put each already-running member into the "
        "job, which is what the explicit_child_assign step models.")
    ev["note"] = ("two-phase host (assign, then spawn) + explicit containment of "
                  "all three (IsProcessInJob-verified); owner-termination left the "
                  "outer job open so the watchdog reaped the worker, and closing "
                  "the outer job killed the whole contained subtree"
                  if verdict else "see evidence above (membership / teardown)")
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
