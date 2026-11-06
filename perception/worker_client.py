"""Tier-1 ONNX worker client (P3): persistent subprocess over JSON-lines.

Contract (from onnx-worker/worker.py):
  python worker.py <model_dir> <manifest.json>
  req {"op":"embed","texts":[...],"request_id":"r1"}
    -> {"request_id":"r1","vectors":[[f,...]],"model_version":"...","dim":384,"latency_ms":...}
  req {"op":"ping","request_id":"p1"} -> {"request_id":"p1","ok":true,"model_version":"...","provider":"..."}
  req {"op":"shutdown"} -> worker exits
  any error -> {"request_id":...,"error":"<class>: <msg>"}
Startup verifies sha256 of every manifest file; non-zero exit on failure.

Runtime env is the verified PM API:
  pm.client.ensure_project_environment("its-in-your-head-onnx",
      project=onnx-worker dir, explicit=True) -> interpreter path itself.
``pm`` import is guarded: unavailable means runtime unavailable, never raises
at import time. The client never downloads a model (enable flow owns that).
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
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


def _safe_model_dirname(model_id: str) -> str:
    return model_id.replace("/", "--")


def resolve_data_root() -> Path:
    """Same resolution as storage/database.py / tools/fetch_onnx_model.py."""
    try:
        from plugins.plugin_storage import plugin_data_dir

        return plugin_data_dir(PLUGIN_ID)
    except Exception:
        try:
            from hermes_constants import get_hermes_home

            return get_hermes_home() / "plugin-data" / PLUGIN_ID
        except Exception:
            from pathlib import Path as _P

            return _P.home() / ".hermes" / "plugin-data" / PLUGIN_ID


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
            # Verified: returns the interpreter path itself.
            python = self._pm.client.ensure_project_environment(
                PM_ENV_NAME, project=ONNX_WORKER_DIR, explicit=True)
        except Exception as exc:
            raise Tier1Failure("runtime-unavailable", f"PM env ensure failed: {type(exc).__name__}: {exc}") from exc
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
            self._proc = subprocess.Popen(
                [interp, str(self.worker_path), str(self.model_dir), str(self.manifest_path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1)
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
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass

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
