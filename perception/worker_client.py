"""Tier-1 ONNX worker client (P3): persistent subprocess over JSON-lines.

Contract (from onnx-worker/worker.py):
  python worker.py <model_dir> <manifest.json>
  req {"op":"embed","texts":[...],"request_id":"r1"}
    -> {"request_id":"r1","vectors":[[f,...]],"model_version":"...","dim":384,"latency_ms":...}
  req {"op":"ping","request_id":"p1"} -> {"request_id":"p1","ok":true,"model_version":"...","provider":"..."}
  req {"op":"shutdown"} -> worker exits
  any error -> {"request_id":...,"error":"<class>: <msg>"}
Startup verifies sha256 of every manifest file; non-zero exit on failure.

Runtime env is a READ-ONLY pm.environment_python() lookup — no
provisioning on the encode path. The env is provisioned only by
explicit user actions (/mirror engine enable, maintainer --repin) via
pm.ensure_project_environment(..., explicit=True).
The worker subprocess runs in an isolated env (_worker_env()): a
minimal allowlist, so it never inherits Hermes provider keys,
*_TOKEN / *_SECRET, PYTHONPATH or PYTHONHOME.
``pm`` import is guarded: unavailable means runtime unavailable, never raises
at import time. The client never downloads a model (enable flow owns that).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:  # guarded: never raise out of the import
    import pm  # type: ignore
    _PM_AVAILABLE = True
except Exception:
    pm = None  # type: ignore
    _PM_AVAILABLE = False

ROOT = Path(__file__).resolve().parent.parent
ONNX_WORKER_DIR = ROOT / "onnx-worker"
WORKER_PY = ONNX_WORKER_DIR / "worker.py"
MANIFEST_PATH = ROOT / "onnx" / "manifest.json"
PM_ENV_NAME = "its-in-your-head-onnx"
PLUGIN_ID = "its-in-your-head"
DEFAULT_TIMEOUT_S = 60.0
_MODEL_ID_FOR_DIR = "onnx-community/paraphrase-multilingual-MiniLM-L12-v2-ONNX"

# ---------------------------------------------------------------------------
# Process-tree leak guard.
#
# The ONNX worker is a Popen child reaped ONLY by an explicit shutdown().
# With no teardown, a hard-killed owner (the gateway, the plugin-host child,
# a taskkill) ORPHANS worker.py — each restart stacks another one. Four
# layers, all best-effort (a failure drops only the safety net, never the
# worker). guard_status() reports which tier is in effect:
#   1. WATCHDOG (primary immediate cleanup, "watchdog"): a detached
#      stdlib-only child (perception/worker_watchdog.py) spawned alongside
#      each worker; when the OWNER dies it terminates the worker AT that
#      moment — closing the "orphan lives until the next spawn" gap. No
#      service installer, no elevation.
#   2. STALE-REGISTRY REAPER (backstop, "reaper-only"): environment-
#      independent; on the next spawn it terminates any worker whose owner
#      is gone, after a pid-safety check (never a recycled pid).
#   3. KILL_ON_JOB_CLOSE JOB OBJECT (independent Windows belt): _windows_
#      kill_job sets KILL_ON_JOB_CLOSE on a 144-byte JOBOBJECT_EXTENDED_
#      LIMIT_INFORMATION; when the last job handle closes, the kernel kills
#      every member -- independently of the watchdog. _windows_kill_job
#      reads the limit back and bails (falling back to watchdog + reaper)
#      when it is not actually applied on this system.
#   4. atexit: on a CLEAN exit, shut the shared client down (reap worker +
#      watchdog).
# Note: guard_status() names the MECHANISM tier actually engaged; it does
# NOT assert hard-kill is verified -- a verified hard-kill additionally
# requires the real owner-termination + close-kills-member tests to have
# passed on Windows.
# ---------------------------------------------------------------------------
WATCHDOG_PY = Path(__file__).resolve().with_name("worker_watchdog.py")
_kill_job_handle = 0        # the open job handle, kept for the process lifetime
_kernel32 = None
_watchdog_ok: bool | None = None  # None = not yet attempted; True/False = last spawn
_active_watchdogs: set = set()  # live detached watchdog Popen handles
_PROC_SET_QUOTA = 0x0100    # PROCESS_SET_QUOTA
_PROC_TERMINATE = 0x0001    # PROCESS_TERMINATE
_PROC_QUERY_INFORMATION = 0x0400
_JOB_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOBOBJECT_EXTENDED_LIMIT_INFORMATION = 9
_ERROR_MORE_DATA = 24
_ERROR_BAD_LENGTH = 136
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87


def _job_structs():
    import ctypes
    _c = ctypes

    # Native JOBOBJECT_BASIC_LIMIT_INFORMATION (winnt.h / ReactOS NDK).
    # 64 bytes on x64: two LONGLONG time limits + DWORD LimitFlags + pad +
    # 2x SIZE_T working-set + DWORD ActiveProcessLimit + pad + ULONG_PTR
    # Affinity + 2x DWORD (PriorityClass, SchedulingClass).
    class _Basic(_c.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", _c.c_longlong),
            ("PerJobUserTimeLimit", _c.c_longlong),
            ("LimitFlags", _c.c_uint32),
            ("MinimumWorkingSetSize", _c.c_size_t),
            ("MaximumWorkingSetSize", _c.c_size_t),
            ("ActiveProcessLimit", _c.c_uint32),
            ("Affinity", _c.c_size_t),
            ("PriorityClass", _c.c_uint32),
            ("SchedulingClass", _c.c_uint32),
        ]

    # Native IO_COUNTERS (winnt.h / Microsoft Learn): 6x ULONGLONG = 48 bytes
    # on x64 -- NO quota fields (those belong to the larger KERNEL_IO_
    # COUNTERS). This is exactly what JOBOBJECT_EXTENDED_LIMIT_INFORMATION.
    # IoInfo is embedded.
    class _IO_COUNTERS(_c.Structure):
        _fields_ = [
            ("ReadOperationCount", _c.c_ulonglong),
            ("WriteOperationCount", _c.c_ulonglong),
            ("OtherOperationCount", _c.c_ulonglong),
            ("ReadTransferCount", _c.c_ulonglong),
            ("WriteTransferCount", _c.c_ulonglong),
            ("OtherTransferCount", _c.c_ulonglong),
        ]

    # Native JOBOBJECT_EXTENDED_LIMIT_INFORMATION: Basic (64) + IO_COUNTERS
    # (48) + 4x SIZE_T (32) = 144 bytes on x64. IoInfo is an embedded
    # IO_COUNTERS struct, not a pointer.
    class _Extended(_c.Structure):
        _fields_ = [
            ("BasicLimitInformation", _Basic),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", _c.c_size_t),
            ("JobMemoryLimit", _c.c_size_t),
            ("PeakProcessMemoryUsed", _c.c_size_t),
            ("PeakJobMemoryUsed", _c.c_size_t),
        ]

    return _c, _Extended


def _windows_kill_job() -> int:
    """Create (once) + return the process's KILL_ON_JOB_CLOSE job handle.

    Returns 0 when unavailable (non-Windows, or the limit cannot be applied)
    so callers fall back to the stale-registry reaper. The handle is kept
    open for the whole process lifetime so KILL_ON_JOB_CLOSE fires when the
    process exits — IF the limit was actually applied. A successful
    ``CreateJobObjectW`` alone is not enough: we must confirm the limit was
    set (via a read-back), because ``SetInformationJobObject`` can return 0
    (per MSDN, 0 = failure) and leave the flags unset, in which case the job
    would keep members but never kill them.
    """
    global _kill_job_handle, _kernel32
    if _kill_job_handle:
        return _kill_job_handle
    if os.name != "nt":
        return 0
    if _kernel32 is None:
        import ctypes
        k = ctypes.windll.kernel32
        k.CreateJobObjectW.restype = ctypes.c_void_p
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        k.SetInformationJobObject.restype = ctypes.c_int
        k.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        k.QueryInformationJobObject.restype = ctypes.c_int
        k.QueryInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        k.AssignProcessToJobObject.restype = ctypes.c_int
        k.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k.OpenProcess.restype = ctypes.c_void_p
        k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k.GetLastError.restype = ctypes.c_uint32
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        _kernel32 = k
    _c, _ext = _job_structs()
    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        return 0
    ext = _ext()
    ext.BasicLimitInformation.LimitFlags = _JOB_LIMIT_KILL_ON_JOB_CLOSE
    # KILL_ON_CLOSE only: no memory / active-process caps, so the size +
    # count fields stay 0 (unlimited) and process association is unrestricted.
    # A clean close of the handle then kills every member of the job.
    #
    # Gate on SetInformationJobObject's return value (0 = failure, nonzero =
    # success per MSDN) — NOT on a class-9 QueryInformationJobObject read-back:
    # on some Windows builds that Query blocks indefinitely on a KILL_ON_CLOSE
    # job and would hang the host. A 0 return with a *genuine* failure code
    # (access denied / invalid param / BAD_LENGTH = a struct-ABI bug) means the
    # limit was not applied -> bail to the reaper; anything else keeps the job,
    # and the e2e close-kills-member test is the authoritative proof the belt
    # actually fires (it fails if the job is inert).
    ok = _kernel32.SetInformationJobObject(
        job, _JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        _c.byref(ext), _c.sizeof(ext))
    err = _kernel32.GetLastError()
    if not ok and err in (_ERROR_ACCESS_DENIED, _ERROR_INVALID_PARAMETER,
                          _ERROR_BAD_LENGTH):
        logger.debug(
            "its-in-your-head: KILL_ON_CLOSE limit not applied on this system "
            "(Set err=%d); using the stale-registry reaper instead", err)
        _kernel32.CloseHandle(job)
        return 0
    _kill_job_handle = job
    return job


def _attach_to_kill_job(proc) -> None:
    """Assign a Popen to the kill-on-close job so it dies with its owner.

    No-op off-Windows or when the job is unavailable. Best-effort: a failure
    logs + leaves the worker running (losing only the hard-kill safety net).

    ``AssignProcessToJobObject`` needs a handle carrying PROCESS_SET_QUOTA
    (and PROCESS_TERMINATE); the Popen's own handle does not carry it, so we
    re-open the process by pid.
    """
    if os.name != "nt" or proc is None:
        return
    job = _windows_kill_job()
    if not job:
        return
    pid = getattr(proc, "pid", None)
    if not pid or _kernel32 is None:
        return
    handle = _kernel32.OpenProcess(
        _PROC_SET_QUOTA | _PROC_TERMINATE, False, pid)
    if not handle:
        return
    try:
        ok = _kernel32.AssignProcessToJobObject(job, handle)
    finally:
        _kernel32.CloseHandle(handle)
    if not ok:
        logger.debug(
            "its-in-your-head: AssignProcessToJobObject failed "
            "(err=%d); hard-kill safety net unavailable, "
            "stale-registry reaper remains the backstop",
            _kernel32.GetLastError())
    else:
        logger.debug("its-in-your-head: ONNX worker %d bound to kill-on-close job", pid)


# ---------------------------------------------------------------------------
# Stale-registry reaper (layer 2, environment-independent).
#
# The Job Object above is the hard-kill guard, but it needs the process tree
# to NOT already be job-governed — which it usually is under a gateway / MSYS
# host, where AssignProcessToJobObject returns ACCESS_DENIED. This reaper
# works everywhere: each owner records its worker pid under a file named by
# the owner pid; on the next spawn we terminate any worker whose owner is
# gone. A killed-by-PID is only taken after a command-line check proves the
# live pid is still our worker (guards against a recycled pid), so we never
# take down an unrelated process.
# ---------------------------------------------------------------------------
_WORKER_CMDLINE_MARKERS = ("worker.py", "its-in-your-head-onnx")
_ONNX_VENV_MARKER = "its-in-your-head-onnx"  # dedicated venv -> image-path signal


def _registry_dir(model_dir) -> Path | None:
    if not model_dir:
        return None
    d = Path(model_dir) / "onnx-workers"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    if not pid:
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
            STILL_ACTIVE = 259
            # OpenProcess alone is NOT a liveness test: a terminated-but-
            # unreaped process object is still openable. Use the exit code.
            h = k.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
            if not h:
                return False
            try:
                code = ctypes.c_uint32()
                if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                k.CloseHandle(h)
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return False


def _proc_is_our_worker(pid: int) -> bool:
    """True when `pid` still runs our ONNX worker. Fast + in-process (no
    shell): the onnx venv is dedicated to the worker, so matching the
    process IMAGE against it is a strong "this pid is our worker" signal.
    It also covers the death-then-PID-reuse window (a recycled pid now runs a
    different interpreter -> no match). False (safe: the reaper skips the
    kill) when the image cannot be read or does not match."""
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            k.OpenProcess.restype = ctypes.c_void_p
            h = k.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
            if not h:
                return False
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = ctypes.c_uint32(buf._length_)
                k.QueryFullProcessImageNameW.argtypes = [
                    ctypes.c_void_p, ctypes.c_uint32,
                    ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
                ok = k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
            finally:
                k.CloseHandle(h)
            if not ok:
                return False
            return _ONNX_VENV_MARKER in buf.value.lower()
        out = Path(f"/proc/{pid}/cmdline").read_text().replace("\x00", " ")
        return all(m in out.lower() for m in _WORKER_CMDLINE_MARKERS)
    except Exception:
        return False


def _terminate_pid(pid: int) -> bool:
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
            ok = bool(k.TerminateProcess(h, 1))
            k.CloseHandle(h)
            return ok
        import signal
        os.kill(pid, signal.SIGKILL)
        return True
    except Exception:
        return False


def _reap_stale_workers(model_dir) -> None:
    reg = _registry_dir(model_dir)
    if reg is None:
        return
    self_pid = os.getpid()
    for f in sorted(reg.iterdir()):
        if not f.name.endswith(".json"):
            continue
        try:
            data = json.loads(f.read_text())
            owner = int(f.stem)
            wpid = int(data.get("worker_pid", 0))
        except Exception:
            try:
                f.unlink()  # corrupt/unreadable entry: safe to drop
            except Exception:
                pass
            continue
        if _pid_alive(owner) or owner == self_pid:
            # Live owner: keep its entry + do not touch its worker.
            continue
        # Owner gone -> the recorded worker is a stale orphan (if still up).
        if wpid and wpid != self_pid and _pid_alive(wpid) \
                and _proc_is_our_worker(wpid):
            _terminate_pid(wpid)
            logger.info(
                "its-in-your-head: reaped stale ONNX worker pid %d "
                "(owner %d gone)", wpid, owner)
        try:
            f.unlink()  # drop the dead owner's entry
        except Exception:
            pass


def _record_worker(model_dir, worker_pid: int) -> None:
    reg = _registry_dir(model_dir)
    if reg is None:
        return
    f = reg / "{}.json".format(os.getpid())
    try:
        f.write_text(json.dumps({"worker_pid": int(worker_pid), "ts": time.time()}))
    except Exception:
        pass


def _forget_worker(model_dir) -> None:
    reg = _registry_dir(model_dir)
    if reg is None:
        return
    f = reg / "{}.json".format(os.getpid())
    try:
        f.unlink()
    except Exception:
        pass


def _watchdog_supported() -> bool:
    """A detached stdlib child outliving a hard-killed owner: Windows + POSIX."""
    return os.name in ("nt", "posix")


def _reaper_available() -> bool:
    """The stale-registry reaper is file-based + works on any filesystem."""
    return True


def _live_watchdog_count() -> int:
    """Number of detached watchdogs still running; prune the dead handles so
    the set stays bounded."""
    n = 0
    for wd in list(_active_watchdogs):
        if wd.poll() is None:
            n += 1
        else:
            _active_watchdogs.discard(wd)
    return n


def guard_status() -> str:
    """The hard-kill protection tier ENGAGED right now (an ACTIVE guard, not
    merely platform support).

    - ``"watchdog"``    : one or more detached watchdogs are alive + guarding
                          a worker (primary immediate cleanup, fires AT owner
                          death) + the reaper backstop.
    - ``"reaper-only"`` : no watchdog is currently active (none spawned yet,
                          all reaped/stopped, unsupported, or the spawn failed)
                          -> the reaper backstop carries.
    - ``"unavailable"`` : neither the watchdog nor the reaper is available.

    This names the MECHANISM actually engaged; it deliberately does NOT assert
    hard-kill is *verified*. A verified hard-kill additionally requires the
    real owner-termination test (``test_watchdog_terminates_on_owner_death``)
    to have passed on Windows.
    """
    if _live_watchdog_count():
        return "watchdog"
    if _reaper_available():
        return "reaper-only"
    return "unavailable"


def _spawn_watchdog(proc: subprocess.Popen | None,
                    owner_pid: int | None = None,
                    model_dir=None) -> subprocess.Popen | None:
    """Spawn a detached watchdog that terminates ``proc`` when ``owner_pid``
    dies.

    Detached (``DETACHED_PROCESS``/``start_new_session``) so a hard-killed
    owner (where atexit never runs) does not take the watchdog down with it.
    Best-effort: returns None + marks the watchdog tier unavailable (so
    ``guard_status()`` drops to ``reaper-only``) when unsupported or the
    spawn raises. It never touches the worker itself.
    """
    global _watchdog_ok
    if proc is None or proc.pid is None:
        _watchdog_ok = False
        return None
    if not _watchdog_supported():
        _watchdog_ok = False
        return None
    owner_pid = owner_pid or os.getpid()
    try:
        kwargs: dict = dict(stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
        if os.name == "nt":
            kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                       | subprocess.DETACHED_PROCESS)
        else:
            kwargs["start_new_session"] = True
        wd = subprocess.Popen(
            [sys.executable, str(WATCHDOG_PY), str(owner_pid), str(proc.pid)],
            **kwargs)
        _watchdog_ok = True
        _active_watchdogs.add(wd)
        return wd
    except Exception as exc:
        logger.debug("its-in-your-head: watchdog spawn failed: %s", exc)
        _watchdog_ok = False
        return None


def _safe_model_dirname(model_id: str) -> str:
    return model_id.replace("/", "--")


def resolve_data_root() -> Path:
    """Same resolution as storage/database.py / tools/fetch_onnx_model.py.

    The active profile's plugin-data dir, via plugin_storage — never a
    silent home-dir fallback: that would land outside the active
    profile. When Hermes storage is unavailable this is an explicit
    error, not a guess.
    """
    try:
        from plugins.plugin_storage import plugin_data_dir

        return plugin_data_dir(PLUGIN_ID)
    except Exception:
        try:
            from hermes_constants import get_hermes_home

            return Path(get_hermes_home()) / "plugin-data" / PLUGIN_ID
        except Exception:
            raise RuntimeError(
                "its-in-your-head: cannot resolve the plugin data dir: "
                "Hermes plugin storage is unavailable. Run under Hermes "
                "or pass an explicit model dir.")


def _worker_env() -> dict[str, str]:
    """Isolated env for the worker subprocess.

    The ONNX worker must not inherit Hermes runtime state: provider
    keys, *_TOKEN / *_SECRET, PYTHONPATH, PYTHONHOME. Copy only a
    minimal allowlist, each only when present in the parent:

    - always: PATH, HOME, LANG, LC_ALL
    - Windows: SYSTEMROOT, TEMP, TMP

    Regression: tests/test_worker_env_isolation.py
    """
    names = ("PATH", "HOME", "LANG", "LC_ALL")
    if os.name == "nt":
        names += ("SYSTEMROOT", "TEMP", "TMP")
    return {n: os.environ[n] for n in names if n in os.environ}


def resolve_model_dir(model_dir: str | Path | None = None) -> Path:
    if model_dir is not None:
        return Path(model_dir)
    return resolve_data_root() / "models" / _safe_model_dirname(_MODEL_ID_FOR_DIR)


def load_manifest(manifest_path: str | Path | None = None) -> dict:
    return json.loads(Path(manifest_path or MANIFEST_PATH).read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def check_model(model_dir: str | Path | None = None,
                manifest_path: str | Path | None = None) -> tuple[str, list[str]]:
    """Return ("verified"|"missing"|"hash-mismatch", errors). Zero network."""
    mdir = resolve_model_dir(model_dir)
    try:
        manifest = load_manifest(manifest_path)
    except Exception as exc:
        return "missing", [f"manifest unreadable: {exc}"]
    errors: list[str] = []
    name = manifest.get("default_variant") or "int8"
    entry = (manifest.get("variants") or {}).get(name)
    if entry is None:
        return "missing", [f"manifest has no variant {name!r}"]
    files = entry.get("files") or {}
    missing: list[str] = []
    mismatch: list[str] = []
    for rel, meta in files.items():
        path = mdir / rel
        if not path.is_file():
            missing.append(f"missing: {rel}")
            continue
        expected = (meta or {}).get("sha256")
        if expected and sha256_file(path) != expected:
            mismatch.append(f"mismatch: {rel}")
    if missing:
        return "missing", missing
    if mismatch:
        return "hash-mismatch", mismatch
    return "verified", []


@dataclass
class Tier1Failure(Exception):
    kind: str
    detail: str = ""

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.kind}: {self.detail}"


@dataclass
class EmbedResult:
    ok: bool
    vectors: list[list[float]] | None = None
    model_version: str | None = None
    dim: int | None = None
    latency_ms: float | None = None
    failure: Tier1Failure | None = None
    raw: dict | None = field(default=None, repr=False)


class _Death(Exception):
    pass


class _Timeout(Exception):
    pass


class OnnxWorkerClient:
    """Lazy persistent subprocess client. One restart on death, then degraded."""

    def __init__(self, model_dir=None, manifest_path=None, worker_path=None,
                 interpreter=None, timeout_s: float = DEFAULT_TIMEOUT_S,
                 pm_module="__auto__"):
        self.model_dir = resolve_model_dir(model_dir)
        self.manifest_path = Path(manifest_path) if manifest_path else MANIFEST_PATH
        self.worker_path = Path(worker_path) if worker_path else WORKER_PY
        self.interpreter = str(interpreter) if interpreter else None
        self.timeout_s = float(timeout_s)
        if pm_module == "__auto__":
            self._pm = pm if _PM_AVAILABLE else None
        else:
            self._pm = pm_module
        self._proc: subprocess.Popen | None = None
        self._watchdog: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._next_id = 0
        self._restart_count = 0
        self._degraded = False
        self._last_failure: Tier1Failure | None = None
        self._ping: dict | None = None

    # -- runtime / interpreter -------------------------------------------------
    def _runtime_available(self) -> bool:
        if self.interpreter is not None:
            return Path(self.interpreter).is_file()
        return self._pm is not None

    def _resolve_interpreter(self) -> str:
        if self.interpreter is not None:
            if not Path(self.interpreter).is_file():
                raise Tier1Failure("runtime-unavailable", f"interpreter not found: {self.interpreter}")
            return self.interpreter
        if self._pm is None:
            raise Tier1Failure("runtime-unavailable", "pm is not importable")
        try:
            lookup = self._pm.environment_python
        except AttributeError:
            raise Tier1Failure(
                "runtime-unavailable",
                "pm facade lacks environment_python; "
                "Tier-1 ONNX requires the Hermes PM API")
        try:
            # READ-ONLY lookup: the interpreter of the env provisioned by
            # an explicit /mirror engine enable (or maintainer --repin),
            # or None when the env is not installed. No provisioning and
            # no state writes on the encode path — the Tier-0 fallback
            # owns that case.
            python = lookup(PM_ENV_NAME)
        except Exception as exc:
            # A corrupt selection record / mid-write venv degrades to
            # Tier-0 instead of crashing the encode path.
            raise Tier1Failure(
                "runtime-unavailable",
                f"PM env read failed: {type(exc).__name__}: {exc}") from exc
        if python is None:
            raise Tier1Failure(
                "runtime-unavailable",
                "PM env not provisioned; run /mirror engine enable")
        if not Path(str(python)).is_file():
            raise Tier1Failure("runtime-unavailable", f"PM env interpreter not found: {python}")
        self.interpreter = str(python)
        return str(python)

    # -- spawn / handshake ------------------------------------------------------
    def _spawn(self) -> None:
        interp = self._resolve_interpreter()
        if not self.worker_path.is_file():
            raise Tier1Failure("worker-missing", f"worker not found: {self.worker_path}")
        model_status, merr = check_model(self.model_dir, self.manifest_path)
        if model_status == "missing":
            raise Tier1Failure("model-missing", "; ".join(merr) or "model missing")
        if model_status == "hash-mismatch":
            raise Tier1Failure("hash-mismatch", "; ".join(merr) or "hash mismatch")
        try:
            # Isolated env: the worker never inherits Hermes provider
            # keys, *_TOKEN / *_SECRET, PYTHONPATH or PYTHONHOME.
            self._proc = subprocess.Popen(
                [interp, str(self.worker_path), str(self.model_dir), str(self.manifest_path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
                env=_worker_env())
            # Best-effort: bind this worker to the process's KILL_ON_JOB_CLOSE
            # job so it is reaped even if the owner is hard-killed (taskkill).
            _attach_to_kill_job(self._proc)
            # Reap orphans from dead owners, then record this worker so the
            # next owner reaps it if we die without a clean shutdown.
            _reap_stale_workers(self.model_dir)
            _record_worker(self.model_dir, self._proc.pid)
            # Primary immediate cleanup: a detached watchdog that kills this
            # worker the moment the owner (this process) dies, hard kill
            # included. Best-effort -> guard_status() drops to reaper-only.
            self._watchdog = _spawn_watchdog(self._proc, owner_pid=os.getpid())
        except Exception as exc:
            raise Tier1Failure("spawn-failed", f"{type(exc).__name__}: {exc}") from exc
        # ping handshake on start
        resp = self._call_raw({"op": "ping"}, timeout=self.timeout_s)
        if "error" in resp:
            self._kill()
            raise Tier1Failure("protocol-error", resp["error"])
        if not resp.get("ok"):
            self._kill()
            raise Tier1Failure("protocol-error", f"ping not ok: {resp}")
        self._ping = resp
        self._degraded = False

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
        self._stop_watchdog()

    def _stop_watchdog(self) -> None:
        """Terminate + reap the watchdog for this worker. On a clean shutdown
        the worker dies first, so the watchdog self-exits; this just reaps
        the handle + drops it from the live set so we never hold a stale one."""
        wd, self._watchdog = self._watchdog, None
        if wd is None:
            return
        if wd.poll() is None:
            try:
                wd.kill()
                wd.wait(timeout=2)
            except Exception:
                pass
        _active_watchdogs.discard(wd)

    def _readline(self, timeout: float) -> str:
        assert self._proc is not None and self._proc.stdout is not None
        holder: dict[str, Any] = {}

        def _reader():
            try:
                holder["line"] = self._proc.stdout.readline()  # type: ignore[union-attr]
            except Exception as exc:
                holder["error"] = exc

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise _Timeout(f"no response within {timeout}s")
        if "error" in holder:
            raise _Death(f"stdout read failed: {holder['error']}")
        line = holder.get("line") or ""
        if not line:
            raise _Death("worker stdout EOF (worker died)")
        return line

    def _call_raw(self, payload: dict, timeout: float | None = None) -> dict:
        if not self._alive():
            raise _Death("worker not running")
        self._next_id += 1
        req = {"request_id": f"r{self._next_id}", **payload}
        try:
            assert self._proc is not None and self._proc.stdin is not None
            self._proc.stdin.write(json.dumps(req) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise _Death(f"write failed: {exc}") from exc
        line = self._readline(timeout or self.timeout_s)
        try:
            resp = json.loads(line)
        except json.JSONDecodeError as exc:
            raise _Death(f"invalid JSON response: {exc}") from exc
        if not isinstance(resp, dict):
            raise _Death(f"non-object response: {resp!r}")
        return resp

    # -- request with one restart ---------------------------------------------
    def _request(self, payload: dict, timeout: float | None = None) -> EmbedResult | dict:
        """Return response dict or EmbedResult(failure). Used by embed/ping."""
        with self._lock:
            return self._request_locked(payload, timeout)

    def _request_locked(self, payload: dict, timeout: float | None = None):
        attempts = 0
        while True:
            attempts += 1
            try:
                if not self._alive():
                    # If we have already used our one restart, stay degraded.
                    if self._degraded:
                        f = self._last_failure or Tier1Failure("worker-dead", "worker degraded")
                        return EmbedResult(ok=False, failure=f)
                    self._spawn()
                resp = self._call_raw(payload, timeout=timeout)
                if "error" in resp:
                    f = Tier1Failure("protocol-error", str(resp["error"]))
                    self._last_failure = f
                    return EmbedResult(ok=False, failure=f)
                self._last_failure = None
                return resp
            except Tier1Failure as f:
                self._last_failure = f
                if isinstance(f, Tier1Failure):
                    return EmbedResult(ok=False, failure=f)
                raise
            except (_Death, _Timeout) as exc:
                kind = "timeout" if isinstance(exc, _Timeout) else "worker-dead"
                self._kill()
                if not self._degraded and self._restart_count < 1:
                    self._restart_count += 1
                    continue  # one automatic restart, then retry once
                f = Tier1Failure(kind, f"{type(exc).__name__}: {exc}")
                self._last_failure = f
                self._degraded = True
                logger.warning("its-in-your-head: ONNX worker degraded after one restart: %s", f)
                return EmbedResult(ok=False, failure=f)
            except Exception as exc:  # pragma: no cover - defensive
                f = Tier1Failure("protocol-error", f"{type(exc).__name__}: {exc}")
                self._last_failure = f
                return EmbedResult(ok=False, failure=f)

    # -- public API -------------------------------------------------------------
    def ping(self) -> EmbedResult:
        res = self._request({"op": "ping"})
        if isinstance(res, EmbedResult):
            return res
        return EmbedResult(ok=True, model_version=res.get("model_version"),
                           raw=res)

    def embed(self, texts: list[str]) -> EmbedResult:
        res = self._request({"op": "embed", "texts": list(texts)})
        if isinstance(res, EmbedResult):
            return res
        vectors = res.get("vectors")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            f = Tier1Failure("protocol-error", f"bad vectors shape: {res.get('vectors')!r}"[:300])
            self._last_failure = f
            return EmbedResult(ok=False, failure=f)
        return EmbedResult(ok=True, vectors=[[float(x) for x in v] for v in vectors],
                           model_version=res.get("model_version"),
                           dim=res.get("dim"), latency_ms=res.get("latency_ms"), raw=res)

    def start(self) -> EmbedResult:
        """Start worker (if needed) and handshake. Returns ping result."""
        return self.ping()

    def status(self) -> dict:
        model_status, _ = check_model(self.model_dir, self.manifest_path)
        return {
            "runtime": "ok" if self._runtime_available() else "unavailable",
            "model": model_status,
            "worker": "running" if self._alive() else "dead",
            "last_failure": str(self._last_failure) if self._last_failure else None,
            "degraded": self._degraded,
            "model_version": (self._ping or {}).get("model_version"),
            "interpreter": self.interpreter,
            "model_dir": str(self.model_dir),
        }

    def shutdown(self) -> None:
        with self._lock:
            if not self._alive():
                self._proc = None
                return
            try:
                assert self._proc is not None and self._proc.stdin is not None
                self._proc.stdin.write(json.dumps({"op": "shutdown"}) + "\n")
                self._proc.stdin.flush()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=5)  # type: ignore[union-attr]
            except Exception:
                self._kill()
            self._proc = None
            _forget_worker(self.model_dir)
            self._stop_watchdog()


_shared: OnnxWorkerClient | None = None
_shared_lock = threading.Lock()


def get_shared_client(**kwargs) -> OnnxWorkerClient:
    """Process-wide client so the subprocess survives across encode() calls."""
    global _shared
    if _shared is None:
        with _shared_lock:
            if _shared is None:
                _shared = OnnxWorkerClient(**kwargs)
    return _shared


def reset_shared_client() -> None:
    global _shared
    with _shared_lock:
        if _shared is not None:
            try:
                _shared.shutdown()
            except Exception:
                pass
        _shared = None


# ---------------------------------------------------------------------------
# Clean-exit leak guard: on a NORMAL interpreter/process exit, reap the shared
# ONNX worker. The hard-kill path (taskkill / crash, where atexit never runs)
# is covered by the KILL_ON_JOB_CLOSE job object assigned in _spawn().
# ---------------------------------------------------------------------------
import atexit  # noqa: E402

atexit.register(reset_shared_client)
