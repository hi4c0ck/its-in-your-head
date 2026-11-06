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
TIER1_MODEL_ID = "sentence-transformers/paraphrase-multilingual-MiniLM-L6-v2"
TIER1_REVISION = "pinned-v1"  # replace with a real HF revision hash on first download
TIER1_MODEL_VERSION = f"{TIER1_MODEL_ID}@rev-{TIER1_REVISION}"
TIER1_ARTIFACTS = {
    "model.onnx": None,          # sha256 filled at first download / vendor time
    "tokenizer.json": None,
}
# Opt-in download base (used ONLY by download_tier1_model, never at encode-time).
# The vendored/local path takes precedence; this is the explicit-user-action path.
TIER1_HF_BASE = "https://huggingface.co/{model}/resolve/{rev}/onnx/{name}"
BATCH_SIZE = 64


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
    """Tier-1: local ONNX multilingual MiniLM, placeholder loader.

    * ``model_path`` resolution order: explicit arg -> ``data_dir`` artifacts.
    * Lazy: nothing is loaded in ``__init__`` (register-time safe).
    * ``encode()`` raises ``ModelNotInstalledError`` when artifacts are
      missing and ``DependencyError`` when ``onnxruntime`` is not installed —
      it never downloads (zero-network at encode-time).
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
    ) -> None:
        self._model_path = Path(model_path) if model_path else None
        self._tokenizer_path = Path(tokenizer_path) if tokenizer_path else None
        self._data_dir = Path(data_dir) if data_dir else None
        self._model_version = model_version
        self.max_seq_len = max_seq_len
        self._session = None  # lazy; importing here would be register-time I/O-adjacent work

    @property
    def model_version(self) -> str:
        return self._model_version

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
        self.resolve_paths()  # fail fast with a clear error before any heavy load
        self._ensure_session()
        # Real tokenization + pooling happens here once artifacts are vendored.
        # This placeholder path is intentionally unreachable in tests (no artifacts).
        raise EmbeddingError("Tier-1 inference not vendored in this build; use Tier-0.")

    def needs_reembed(self, cached_version: str | None) -> bool:
        """Migration hook: any model_version bump invalidates cached vectors."""
        return cached_version != self._model_version


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
) -> EmbeddingBackend:
    """Factory. No I/O, no network — construction is register-time safe."""
    name = resolve_backend_name(settings, backend_name)
    if name == "tier-0":
        return Tier0SketchBackend()
    return OnnxMiniLMBackend(model_path=model_path, data_dir=data_dir)


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

def download_tier1_model(
    data_dir: str | Path,
    *,
    confirm: bool = False,
    revision: str = TIER1_REVISION,
    opener=None,
) -> dict:
    """Explicit user action only. Requires ``confirm=True``.

    Downloads pinned artifacts into ``data_dir`` and records the installed
    model_version next to them; callers must re-embed anchors when the
    version changes (see ``OnnxMiniLMBackend.needs_reembed``)."""
    if not confirm:
        raise EmbeddingError(
            "Refusing to download Tier-1 model without confirm=True (explicit user action)."
        )
    dest = Path(data_dir)
    dest.mkdir(parents=True, exist_ok=True)
    installed = {}
    open_fn = opener or urllib.request.urlopen
    for name in TIER1_ARTIFACTS:
        url = TIER1_HF_BASE.format(model=TIER1_MODEL_ID, rev=revision, name=name)
        out = dest / name
        with open_fn(url) as resp, open(out, "wb") as f:  # noqa: S310 - pinned explicit download
            while True:
                block = resp.read(1 << 16)
                if not block:
                    break
                f.write(block)
        installed[name] = {"path": str(out), "sha256": sha256_file(out)}
    version = f"{TIER1_MODEL_ID}@rev-{revision}"
    (dest / "model_version.txt").write_text(version, encoding="utf-8")
    return {"model_version": version, **installed}
