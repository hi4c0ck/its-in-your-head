"""EmbeddingBackend interface + Tier-0/Tier-1 backends (S2).

Zero-network contract:
- Tier-0 (`Tier0SketchBackend`) wraps the root ``embedder.py`` sketch via
  import (never edited here); no I/O, no network.
- Tier-1 (`OnnxMiniLMBackend`) loads a pinned local ONNX file from the plugin
  data dir at first ``encode()``. ``encode()`` itself never touches the
  network; the only network path is the explicit
  ``download_tier1_model(data_dir, *, confirm=True)`` one-shot helper.
- Instantiation performs no I/O and no network (register-time safe, ARCHITECTURE.md).

Backend selection is the C4 settings key ``perception.encoder``
(``tier-0`` | ``tier-1-onnx``, default ``tier-0``). This module only *reads*
the key through ``resolve_backend_name``; the schema entry lands in plugin.yaml
(parent-owned) — see contract_flags in the S2 hand-back.
"""
from __future__ import annotations

import hashlib
import math
import struct
import urllib.request
from pathlib import Path
from typing import Protocol, runtime_checkable

try:  # hermes namespaced import (gateway) vs top-level import (tests)
    from .. import embedder
except Exception:  # ImportError when repo root is on sys.path directly
    import embedder  # type: ignore[no-redef]

PLUGIN_ID = "its-in-your-head"

# C4 settings key (proposed; plugin.yaml is parent-owned, do not edit here).
BACKEND_SETTING_KEY = "perception.encoder"
DEFAULT_BACKEND = "tier-0"
BACKENDS = ("tier-0", "tier-1-onnx")

# Tier-0 model version: the sketch has no weights; bump only if its algorithm
# changes (which would require an embedder.py change + contract flag).
TIER0_MODEL_VERSION = "tier0-sketch-v1"

# Tier-1 pinned artifact (version-pinned; re-embed on any bump).
# Decided model (plan P1): onnx-community ONNX export of the L12 multilingual
# MiniLM — the L6 multilingual has no published ONNX artifacts. Revision is
# the immutable HF commit SHA recorded in onnx/manifest.json; artifact keys
# mirror the manifest `files` block (int8 default, fp32 alternative).
TIER1_MODEL_ID = "onnx-community/paraphrase-multilingual-MiniLM-L12-v2-ONNX"
TIER1_REVISION = "d4c06bf0d7680171ac30042a1387e1fdb7a90021"
TIER1_MODEL_VERSION = f"{TIER1_MODEL_ID}@rev-{TIER1_REVISION[:12]}"
TIER1_ARTIFACTS = {
    "onnx/model_int8.onnx": None,   # sha256 in onnx/manifest.json
    "onnx/model.onnx": None,        # fp32 alternative
    "tokenizer.json": None,
    "tokenizer_config.json": None,
    "config.json": None,
    "special_tokens_map.json": None,
}
# Opt-in download base (used ONLY by download_tier1_model, never at encode-time).
# The vendored/local path takes precedence; this is the explicit-user-action path.
# Artifact keys are manifest-relative (e.g. "onnx/model_int8.onnx"), so the
# base is the repo root, not .../onnx/.
TIER1_HF_BASE = "https://huggingface.co/{model}/resolve/{rev}/{name}"
BATCH_SIZE = 64
# Exact pinned-size cap for downloads (A1): stream may not exceed the
# manifest size by more than this tolerance (0 == exact pin).
DOWNLOAD_SIZE_TOLERANCE = 0


class EmbeddingError(RuntimeError):
    pass


class ModelNotInstalledError(EmbeddingError):
    """Raised when Tier-1 artifacts are absent locally (never auto-download)."""


class DependencyError(EmbeddingError):
    """Raised when the optional onnxruntime dependency is missing."""


@runtime_checkable
class EmbeddingBackend(Protocol):
    """Batched text -> L2-normalized vectors."""

    @property
    def model_version(self) -> str: ...

    def encode(self, texts: list[str]) -> list[list[float]]: ...


class Tier0SketchBackend:
    """Tier-0 wrapper around the locked root embedder.py sketch."""

    name = "tier-0"

    @property
    def model_version(self) -> str:
        return TIER0_MODEL_VERSION

    @property
    def dim(self) -> int:
        return embedder.DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        return encode_batched(self._one, list(texts), BATCH_SIZE)

    def _one(self, text: str | None) -> list[float]:
        raw = embedder.sketch(text)
        return list(struct.unpack(f"<{embedder.DIM}f", raw))


class OnnxMiniLMBackend:
    """Tier-1: local ONNX multilingual MiniLM via the isolated worker.

    * ``model_path`` resolution order: explicit arg -> ``data_dir`` artifacts.
    * Lazy: nothing is loaded in ``__init__`` (register-time safe).
    * ``encode()`` never downloads (zero-network at encode-time). When
      ``fallback_to_tier0`` is enabled (factory-created backends), any Tier-1
      failure degrades to Tier-0 for that call and is recorded; direct
      construction keeps the legacy raising behaviour.
    """

    name = "tier-1-onnx"

    def __init__(
        self,
        model_path: str | Path | None = None,
        tokenizer_path: str | Path | None = None,
        *,
        data_dir: str | Path | None = None,
        model_version: str = TIER1_MODEL_VERSION,
        max_seq_len: int = 256,
        client=None,
        fallback_to_tier0: bool = False,
        worker_client=None,
    ) -> None:
        self._model_path = Path(model_path) if model_path else None
        self._tokenizer_path = Path(tokenizer_path) if tokenizer_path else None
        self._data_dir = Path(data_dir) if data_dir else None
        self._model_version = model_version
        self._requested_version = model_version
        self.max_seq_len = max_seq_len
        self._session = None  # lazy; importing here would be register-time I/O-adjacent work
        self.client = client if client is not None else worker_client
        self._fallback_to_tier0 = bool(fallback_to_tier0 or self.client is not None)
        self._tier0 = Tier0SketchBackend()
        self.last_failure: str | None = None
        self.last_model_version: str | None = None
        self._effective_version: str | None = None

    @property
    def model_version(self) -> str:
        return self._effective_version or self._requested_version

    def _record_tier0(self, failure: str) -> list[list[float]]:
        self.last_failure = failure
        self._effective_version = TIER0_MODEL_VERSION
        self.last_model_version = TIER0_MODEL_VERSION
        return self._tier0.encode([])  # placeholder replaced by caller with real texts

    def _get_client(self):
        if self.client is not None:
            return self.client
        try:
            try:
                from .worker_client import get_shared_client
            except Exception:
                from worker_client import get_shared_client  # type: ignore[no-redef]
        except Exception as exc:
            from .worker_client import Tier1Failure  # type: ignore
            raise Tier1Failure("runtime-unavailable", f"worker client import failed: {exc}")
        mdir = self._data_dir
        if self._model_path is not None:
            # Explicit single-file path: use its parent as the model dir.
            mdir = Path(self._model_path).parent
        return get_shared_client(model_dir=mdir)

    def resolve_paths(self) -> tuple[Path, Path]:
        model = self._model_path or (self._data_dir / "model.onnx" if self._data_dir else None)
        tok = self._tokenizer_path or (self._data_dir / "tokenizer.json" if self._data_dir else None)
        missing = [str(p) for p in (model, tok) if p is None or not p.is_file()]
        if missing:
            raise ModelNotInstalledError(
                f"Tier-1 model not installed locally: {missing}. "
                f"Install explicitly via download_tier1_model(data_dir, confirm=True) "
                f"({TIER1_MODEL_ID}@{TIER1_REVISION}); encode() never downloads."
            )
        return Path(model), Path(tok)  # type: ignore[arg-type]

    def _ensure_session(self):  # pragma: no cover - needs real onnxruntime + artifacts
        if self._session is not None:
            return self._session
        model, _tok = self.resolve_paths()
        try:
            import onnxruntime as ort  # optional dependency
        except ImportError as exc:
            raise DependencyError(
                "onnxruntime is an optional Tier-1 dependency: pip install onnxruntime"
            ) from exc
        self._session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
        return self._session

    def encode(self, texts: list[str]) -> list[list[float]]:
        texts = list(texts)
        # Direct legacy path (no worker client, no fallback): keep the
        # pre-LANE-B raising behaviour so existing tests stay green.
        if not self._fallback_to_tier0 and self.client is None:
            self.resolve_paths()  # fail fast with a clear error before any heavy load
            self._ensure_session()
            raise EmbeddingError("Tier-1 inference not vendored in this build; use Tier-0.")
        # Composite path: delegate to the worker client; ANY Tier-1 failure
        # falls back to Tier-0 for this call (never downloads).
        try:
            client = self._get_client()
            result = client.embed(texts)
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
            self._record_tier0(failure)
            return self._tier0.encode(texts)
        if getattr(result, "ok", False):
            self.last_failure = None
            version = getattr(result, "model_version", None) or self._requested_version
            self._effective_version = version
            self.last_model_version = version
            return result.vectors  # type: ignore[return-value]
        failure = str(getattr(result, "failure", None) or "tier-1 failure")
        self._record_tier0(failure)
        return self._tier0.encode(texts)

    def needs_reembed(self, cached_version: str | None) -> bool:
        """Migration hook: any model_version bump invalidates cached vectors."""
        return cached_version != self.model_version


# --------------------------------------------------------------------------- #
# Settings + factory
# --------------------------------------------------------------------------- #

def resolve_backend_name(settings: dict | None = None, override: str | None = None) -> str:
    name = override
    if name is None and isinstance(settings, dict):
        name = settings.get(BACKEND_SETTING_KEY)
    name = str(name or DEFAULT_BACKEND).strip().lower()
    if name not in BACKENDS:
        raise ValueError(f"unknown {BACKEND_SETTING_KEY}={name!r}; expected one of {BACKENDS}")
    return name


def create_backend(
    settings: dict | None = None,
    backend_name: str | None = None,
    *,
    data_dir: str | Path | None = None,
    model_path: str | Path | None = None,
    client=None,
) -> EmbeddingBackend:
    """Factory. No I/O, no network — construction is register-time safe."""
    name = resolve_backend_name(settings, backend_name)
    if name == "tier-0":
        return Tier0SketchBackend()
    return OnnxMiniLMBackend(model_path=model_path, data_dir=data_dir,
                             client=client, fallback_to_tier0=True)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def encode_batched(one_fn, texts: list[str], batch_size: int = BATCH_SIZE) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, max(len(texts), 1), max(1, batch_size)):
        chunk = texts[i:i + batch_size]
        if not chunk and texts:
            break
        out.extend(one_fn(t) for t in chunk)
        if not texts:
            break
    return out


def l2_normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(v * v for v in vec))
    if n == 0:
        return list(vec)
    return [v / n for v in vec]


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# One-time explicit download (the ONLY network path in Tier-1)
# --------------------------------------------------------------------------- #

def _load_manifest(manifest_path=None) -> dict | None:
    try:
        import json

        candidate = Path(manifest_path) if manifest_path else (
            Path(__file__).resolve().parent.parent / "onnx" / "manifest.json")
        return json.loads(candidate.read_text(encoding="utf-8"))
    except Exception:
        return None


def verify_tier1_model(data_dir, *, manifest_path=None) -> list[str]:
    """Zero-network hash gate: re-hash manifest files under ``data_dir``.

    Returns a list of error strings (empty == verified). Mirrors
    tools/verify_onnx_model.py semantics."""
    manifest = _load_manifest(manifest_path)
    if manifest is None:
        return ["manifest unreadable"]
    dest = Path(data_dir)
    errors: list[str] = []
    for rel, meta in (manifest.get("files") or {}).items():
        path = dest / rel
        if not path.is_file():
            errors.append(f"missing: {rel}")
            continue
        expected = (meta or {}).get("sha256")
        if expected and sha256_file(path) != expected:
            errors.append(f"mismatch: {rel}")
    return errors


def _remove_quietly(path) -> None:
    try:
        Path(path).unlink(missing_ok=True)  # type: ignore[call-arg]
    except TypeError:  # very old Python without missing_ok
        try:
            Path(path).unlink()
        except OSError:
            pass
    except OSError:
        pass


def download_tier1_model(
    data_dir: str | Path,
    *,
    confirm: bool = False,
    revision: str = TIER1_REVISION,
    opener=None,
    manifest_path=None,
) -> dict:
    """Explicit user action only. Requires ``confirm=True``.

    Downloads pinned artifacts into ``data_dir`` and records the installed
    model_version next to them; callers must re-embed anchors when the
    version changes (see ``OnnxMiniLMBackend.needs_reembed``). After download
    every manifest file is re-hashed; any mismatch refuses (EmbeddingError).

    Hardening (A1-A3):
    - A1: each stream is capped at the pinned manifest ``size`` (+ tolerance);
      overflow aborts with EmbeddingError. The cap lives in the copy loop,
      so it applies to any opener (urllib or injected).
    - A2: bytes land in ``<name>.part``; ``.part`` -> final rename happens
      only after hash verification passes. Any failure deletes every staged
      ``.part`` (and any final created this run), leaving zero bytes.
    - A3: OSError during the copy becomes EmbeddingError
      ('download interrupted (disk full?): ...'); no traceback leaks.
    ``model_version.txt`` is written only after all files are verified.
    """
    if not confirm:
        raise EmbeddingError(
            "Refusing to download Tier-1 model without confirm=True (explicit user action)."
        )
    dest = Path(data_dir)
    dest.mkdir(parents=True, exist_ok=True)
    installed = {}
    open_fn = opener or urllib.request.urlopen
    manifest = _load_manifest(manifest_path)
    pinned_files = (manifest or {}).get("files") or {}

    staged: list[tuple] = []  # (name, final_out, part_path)
    try:
        for name in TIER1_ARTIFACTS:
            url = TIER1_HF_BASE.format(model=TIER1_MODEL_ID, rev=revision, name=name)
            out = dest / name
            part = Path(str(out) + ".part")
            part.parent.mkdir(parents=True, exist_ok=True)
            staged.append((name, out, part))
            size = (pinned_files.get(name) or {}).get("size")
            cap = None
            if size is not None:
                try:
                    cap = int(size) + DOWNLOAD_SIZE_TOLERANCE
                except (TypeError, ValueError):
                    cap = None
            try:
                with open_fn(url) as resp, open(part, "wb") as f:  # noqa: S310 - pinned explicit download
                    count = 0
                    while True:
                        block = resp.read(1 << 16)
                        if not block:
                            break
                        count += len(block)
                        if cap is not None and count > cap:
                            raise EmbeddingError(
                                f"download size cap exceeded for {name}: "
                                f"received {count} bytes > pinned size {cap} "
                                f"(overflow); download aborted"
                            )
                        f.write(block)
                        f.flush()
            except EmbeddingError:
                raise
            except OSError as exc:
                raise EmbeddingError(
                    f"download interrupted (disk full?): {name}: {exc} "
                    f"— no partial files remain"
                ) from None

        # A2: hash-verify the staged .part files before any rename.
        if manifest is None:
            raise EmbeddingError("sha256 verification failed for Tier-1 model: ['manifest unreadable']")
        staged_by_name = {name: part for name, _out, part in staged}
        errors: list[str] = []
        for rel, meta in pinned_files.items():
            candidate = staged_by_name.get(rel, dest / rel)
            if not candidate.is_file():
                errors.append(f"missing: {rel}")
                continue
            expected = (meta or {}).get("sha256")
            if expected:
                try:
                    actual = sha256_file(candidate)
                except OSError as exc:
                    raise EmbeddingError(
                        f"download interrupted (disk full?): {rel}: {exc} "
                        f"— no partial files remain"
                    ) from None
                if actual != expected:
                    errors.append(f"mismatch: {rel}")
        if errors:
            raise EmbeddingError(f"sha256 verification failed for Tier-1 model: {errors}")

        # Verified: atomically promote .part -> final.
        try:
            for name, out, part in staged:
                part.replace(out)
                installed[name] = {"path": str(out), "sha256": sha256_file(out)}
        except OSError as exc:
            for _name, out, part in staged:
                _remove_quietly(part)
                _remove_quietly(out)
            raise EmbeddingError(
                f"download interrupted (disk full?): {exc} — no partial files remain"
            ) from None

        # Final gate on the promoted files (same semantics as before).
        errors = verify_tier1_model(dest, manifest_path=manifest_path)
        if errors:
            for _name, out, _part in staged:
                _remove_quietly(out)
            raise EmbeddingError(f"sha256 verification failed for Tier-1 model: {errors}")
    except EmbeddingError:
        for _name, _out, part in staged:
            _remove_quietly(part)
        raise
    except OSError as exc:
        for _name, _out, part in staged:
            _remove_quietly(part)
        raise EmbeddingError(
            f"download interrupted (disk full?): {exc} — no partial files remain"
        ) from None
    version = f"{TIER1_MODEL_ID}@rev-{revision}"
    (dest / "model_version.txt").write_text(version, encoding="utf-8")
    return {"model_version": version, **installed}
