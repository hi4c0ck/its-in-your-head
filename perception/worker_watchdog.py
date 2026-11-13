"""Detached ONNX-worker watchdog.

A tiny, stdlib-only child process that outlives its owner. ``worker_client``
spawns it *detached* alongside each worker and passes it the owner PID + the
worker PID. It does exactly one thing: when the OWNER process dies (including
a hard kill where atexit never runs), it terminates the WORKER it was told to
guard AT that moment, closing the "an orphaned worker lives until the next
spawn" gap. If the worker is reaped first (clean shutdown / the stale-registry
reaper), the watchdog sees the worker gone and exits too.

Design notes
------------
- Separate + detached (DETACHED_PROCESS / start_new_session) so a hard-killed
  owner (taskkill, crash, gateway teardown) does not take the watchdog down
  with it -- the watchdog is the thing that survives the death to do the
  killing.
- No silent lifetime cap: the watchdog lives until a death event (owner dies
  -> terminate the worker + exit; worker already gone -> exit). A capped
  lifetime would silently drop the guard while the owner + worker were still
  up.
- Stable process identity, not a bare PID (a PID can be recycled between a
  liveness check and the termination): on Windows it holds OpenProcess handles
  to the owner + worker and fires TerminateProcess on the worker handle; on
  Linux it holds a pidfd for the worker where the kernel exposes one and sends
  the signal through it, falling back to the PID only where pidfd is
  unavailable.
- Stdlib only (os / ctypes / signal / time) so it needs no onnx, no Hermes
  runtime state, no special env; it self-terminates so it never accumulates.

Usage::

    python worker_watchdog.py <owner_pid> <worker_pid>

Exits 0 in every path; it never raises past main().

Scope (kept deliberately narrow): this proves a guarded worker is cleaned up
when its DIRECT owner is terminated. It is not proof that every Hermes host or
Windows Job-Object teardown path keeps this child alive long enough (a Windows
Job Object can reap a detached child unless CREATE_BREAKAWAY_FROM_JOB is
permitted by the parent job). The stale-registry reaper is the
environment-independent backstop for the cases it misses.
"""
from __future__ import annotations

import os
import sys
import time
import ctypes

_POLL_S = 0.2                      # owner-death poll cadence
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_TERMINATE = 0x0001
_STILL_ACTIVE = 259
# /proc is Linux-only; macOS + other POSIX have none (fall back to os.kill).
_PROC_ROOT = os.path.exists("/proc/1/stat")


def _alive(pid: int) -> bool:
    """True when ``pid`` is a live process.

    - Linux: a SIGKILL'd-but-unreaped process is a zombie and
      ``os.kill(pid, 0)`` still succeeds, so read the ``/proc/<pid>/stat``
      state and treat ``Z`` as dead (a hard-killed owner is exactly that).
    - macOS / other POSIX (no /proc): the plain ``os.kill(pid, 0)`` result is
      used; launchd reaps a hard-killed child fast enough that its ESRCH
      arrives shortly after death.
    """
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours to signal -> conservatively alive
    except OSError:
        return False
    if _PROC_ROOT:
        try:
            with open("/proc/{}".format(pid) + "/stat") as fh:
                # stat = "pid (comm) state ..."; comm may hold spaces/parens,
                # so anchor on the last ')' before the state field.
                state = fh.read().rsplit(")", 1)[1].split()[0]
            return state != "Z"
        except OSError:
            return False  # the /proc entry is gone -> dead
    return True


def _windows_main(owner: int, worker: int) -> int:
    """Hold handles to the owner + worker (stable identity) + fire
    TerminateProcess on the worker handle the moment the owner exits."""
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k.GetExitCodeProcess.restype = ctypes.c_int
    k.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k.TerminateProcess.restype = ctypes.c_int
    k.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    owner_h = k.OpenProcess(_PROCESS_QUERY_INFORMATION, 0, owner)
    worker_h = k.OpenProcess(_PROCESS_TERMINATE | _PROCESS_QUERY_INFORMATION,
                             0, worker)
    if not owner_h or not worker_h:
        # Already gone or no right to open it: cannot guard. The reaper is the
        # backstop; a live owner + worker is handled on its clean path.
        return 0
    try:
        while True:
            oc = ctypes.c_uint32()
            if k.GetExitCodeProcess(owner_h, ctypes.byref(oc)) \
                    and oc.value != _STILL_ACTIVE:
                # Owner gone -> terminate the worker by its STABLE handle
                # (immune to a recycled PID).
                k.TerminateProcess(worker_h, 1)
                return 0
            wc_ = ctypes.c_uint32()
            if k.GetExitCodeProcess(worker_h, ctypes.byref(wc_)) \
                    and wc_.value != _STILL_ACTIVE:
                return 0  # worker already gone (clean shutdown)
            time.sleep(_POLL_S)
    finally:
        k.CloseHandle(owner_h)
        k.CloseHandle(worker_h)


def _posix_main(owner: int, worker: int) -> int:
    """Fire on owner death; terminate the worker by STABLE identity (a held
    pidfd where the kernel + glibc expose one, else the PID itself)."""
    import signal

    kill_fd = None
    libc = None
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.pidfd_open.restype = ctypes.c_int
        libc.pidfd_open.argtypes = [ctypes.c_int]
        libc.pidfd_send_signal.restype = ctypes.c_int
        libc.pidfd_send_signal.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
        fd = libc.pidfd_open(worker)
        if isinstance(fd, int) and fd >= 0:
            kill_fd = fd
    except Exception:
        kill_fd = None  # pidfd unavailable -> fall back to os.kill(pid)

    def _kill_worker() -> None:
        if kill_fd is not None and libc is not None:
            # A pidfd refers to the specific process object, so a recycled PID
            # cannot redirect the kill to an unrelated new process.
            libc.pidfd_send_signal(kill_fd, signal.SIGKILL, None, 0)  # type: ignore[attr-defined]
        else:
            try:
                os.kill(worker, signal.SIGKILL)  # type: ignore[attr-defined]
            except OSError:
                pass

    while True:
        if not _alive(owner):
            _kill_worker()
            return 0
        if not _alive(worker):
            return 0
        time.sleep(_POLL_S)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        return 0
    try:
        owner = int(argv[1])
        worker = int(argv[2])
    except ValueError:
        return 0
    if os.name == "nt":
        return _windows_main(owner, worker)
    return _posix_main(owner, worker)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:
        # A crashing watchdog just means "no immediate guard"; the reaper is
        # the backstop. Never take down the spawn path.
        sys.exit(0)
