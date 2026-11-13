"""Detached leak-watchdog for the ONNX worker.

A tiny, stdlib-only child process that outlives its owner. ``worker_client``
spawns it *detached* alongside each worker; it does exactly one thing: when
the OWNER process dies (including a hard kill where atexit never runs), it
terminates the WORKER it was told to guard. If the worker is reaped first
(clean shutdown / the stale-registry reaper), the watchdog sees the worker
gone and exits too.

Why a separate process + why polling:
- It is a *separate, detached* process so a hard-killed owner (taskkill,
  crash, gateway teardown) does not take it down with it — that is the whole
  point: it is the thing that survives the death to do the killing.
- It polls on a short sleep rather than waiting on kernel handles so the same
  code runs unchanged on Windows (TerminateProcess) and POSIX (SIGKILL).
- Stdlib only (os / ctypes / signal / time) so it needs no onnx, no Hermes
  runtime state, no special env, and a plain interpreter. It self-terminates,
  so it never accumulates.

Usage::

    python worker_watchdog.py <owner_pid> <worker_pid>

Exits 0 in every path; it never raises past main().
"""
from __future__ import annotations

import os
import sys
import time

_POLL_S = 0.3
# Defensive cap only; in practice the watchdog exits as soon as the owner or
# the worker dies. Guards a pathological "both live forever" case.
_MAX_RUNTIME_S = 72 * 3600
_STILL_ACTIVE = 259


def _alive(pid: int) -> bool:
    """True when ``pid`` is a live process. Cross-platform, liveness-safe
    (OpenProcess alone is not: an exited-but-unreaped object is still
    openable -> check the exit code)."""
    if pid is None or pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            k.OpenProcess.restype = ctypes.c_void_p
            k.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            k.GetExitCodeProcess.restype = ctypes.c_int
            k.CloseHandle.argtypes = [ctypes.c_void_p]
            h = k.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
            if not h:
                return False
            try:
                code = ctypes.c_uint32()
                if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == _STILL_ACTIVE
            finally:
                k.CloseHandle(h)
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return False


def _kill(pid: int) -> bool:
    """Hard-terminate ``pid`` (TerminateProcess / SIGKILL). Same-user process:
    no elevation required."""
    if pid is None or pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            k.OpenProcess.restype = ctypes.c_void_p
            k.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            k.TerminateProcess.restype = ctypes.c_int
            k.CloseHandle.argtypes = [ctypes.c_void_p]
            h = k.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
            if not h:
                return False
            try:
                return bool(k.TerminateProcess(h, 1))
            finally:
                k.CloseHandle(h)
        import signal
        os.kill(pid, signal.SIGKILL)
        return True
    except Exception:
        return False


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        return 2
    try:
        owner = int(argv[1])
        worker = int(argv[2])
    except ValueError:
        return 2
    deadline = time.time() + _MAX_RUNTIME_S
    while time.time() < deadline:
        if not _alive(worker):
            return 0  # worker already gone -> nothing left to guard
        if not _alive(owner):
            _kill(worker)  # owner died (hard or otherwise) -> take it with us
            return 0
        time.sleep(_POLL_S)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
