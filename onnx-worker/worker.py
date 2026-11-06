"""Isolated ONNX inference worker for its-in-your-head Tier-1.

JSON-lines protocol on stdin/stdout (pinned, see plan P3):

  {"op":"embed","texts":[...],"request_id":"r1"}
    -> {"request_id":"r1","vectors":[[f,...]],"model_version":"...",
        "dim":384,"latency_ms":...}
  {"op":"ping","request_id":"p1"}
    -> {"request_id":"p1","ok":true,"model_version":"...",
        "provider":"CPUExecutionProvider"}
  {"op":"shutdown"} -> worker exits
  any error -> {"request_id":<id or null>,"error":"<class>: <msg>"}

Startup: reads the model dir + manifest (argv), verifies sha256 of every
manifest file, loads the ONNX session lazily on first embed with
CPUExecutionProvider. Tokenization uses the `tokenizers` library against
tokenizer.json (no `transformers` dependency). Pooling is mean over the
attention mask; vectors are L2-normalized (manifest `embedding` block).

Usage: python worker.py <model_dir> <manifest.json>
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
import time
from pathlib import Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify_manifest(model_dir: Path, manifest: dict) -> None:
    for rel, meta in manifest["files"].items():
        expected = meta.get("sha256")
        if not expected:
            raise ValueError(f"manifest has no sha256 for {rel}")
        path = model_dir / rel
        if not path.is_file():
            raise FileNotFoundError(f"manifest file missing: {rel}")
        actual = sha256_file(path)
        if actual != expected:
            raise ValueError(
                f"sha256 mismatch for {rel}: expected {expected}, got {actual}"
            )


class Worker:
    def __init__(self, model_dir: str, manifest_path: str):
        self.model_dir = Path(model_dir)
        self.manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        verify_manifest(self.model_dir, self.manifest)
        emb = self.manifest["embedding"]
        self.dim = int(emb["dimension"])
        self.max_len = int(emb["max_sequence_length"])
        self.model_version = (
            f"{self.manifest['model_id']}@rev-{self.manifest['revision']}"
        )
        self.provider = self.manifest.get("execution_provider", "CPUExecutionProvider")
        self._session = None
        self._tokenizer = None

    def _ensure(self):
        if self._session is None:
            import onnxruntime as ort

            model = self.model_dir / "onnx" / "model_int8.onnx"
            opts = ort.SessionOptions()
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self._session = ort.InferenceSession(
                str(model), sess_options=opts, providers=[self.provider]
            )
        if self._tokenizer is None:
            from tokenizers import Tokenizer

            self._tokenizer = Tokenizer.from_file(
                str(self.model_dir / "tokenizer.json")
            )
        return self._session

    def _embed_one(self, text: str) -> list[float]:
        import numpy as np

        session = self._ensure()
        enc = self._tokenizer.encode(text, add_special_tokens=True)  # type: ignore[union-attr]
        ids = enc.ids[: self.max_len] or [0]
        mask = enc.attention_mask[: self.max_len] or [1]
        types = enc.type_ids[: self.max_len] or [0]
        feeds = {
            "input_ids": np.array([ids], dtype=np.int64),
            "attention_mask": np.array([mask], dtype=np.int64),
            "token_type_ids": np.array([types], dtype=np.int64),
        }
        (last_hidden,) = session.run(None, feeds)  # type: ignore[assignment]
        m = np.array(mask, dtype=np.float32).reshape(1, -1, 1)
        pooled = (last_hidden * m).sum(axis=1) / m.sum(axis=1)  # type: ignore[operator]
        vec = pooled[0]
        n = float(np.linalg.norm(vec))
        if n > 0:
            vec = vec / n
        return [float(v) for v in vec]

    def handle(self, req: dict) -> dict | None:
        op = req.get("op")
        rid = req.get("request_id")
        if op == "shutdown":
            return None
        if op == "ping":
            return {
                "request_id": rid,
                "ok": True,
                "model_version": self.model_version,
                "provider": self.provider,
            }
        if op == "embed":
            texts = req.get("texts")
            if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
                raise ValueError("embed requires 'texts': list[str]")
            t0 = time.perf_counter()
            vectors = [self._embed_one(t) for t in texts]
            return {
                "request_id": rid,
                "vectors": vectors,
                "model_version": self.model_version,
                "dim": self.dim,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 3),
            }
        raise ValueError(f"unknown op: {op!r}")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: worker.py <model_dir> <manifest.json>", file=sys.stderr)
        return 2
    try:
        worker = Worker(argv[1], argv[2])
    except Exception as exc:  # startup failure is reported on stderr, exit non-zero
        print(f"worker startup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            print(json.dumps({"request_id": None,
                              "error": f"JSONDecodeError: {exc}"}), flush=True)
            continue
        try:
            resp = worker.handle(req)
        except Exception as exc:
            resp = {"request_id": req.get("request_id"),
                    "error": f"{type(exc).__name__}: {exc}"}
        if resp is None:
            break
        print(json.dumps(resp), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
