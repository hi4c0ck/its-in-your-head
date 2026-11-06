"""User-side: verify an installed Tier-1 model dir against the manifest.

Re-hashes every file listed in onnx/manifest.json under the given model dir
and refuses (exit 1) on any missing file or sha256 mismatch. Used by the
Tier-1 enable flow (and by the worker bootstrap) before inference starts.
Zero network. Exit 0 = verified.

Usage:
  python tools/verify_onnx_model.py <model_dir>
  python tools/verify_onnx_model.py --model-dir <path>
  python tools/verify_onnx_model.py --manifest <path> <model_dir>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = ROOT / "onnx" / "manifest.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify(model_dir: Path, manifest: dict) -> list[str]:
    errors = []
    for rel, meta in manifest["files"].items():
        expected = meta.get("sha256")
        path = model_dir / rel
        if not path.is_file():
            errors.append(f"missing: {rel}")
            continue
        if not expected:
            errors.append(f"manifest has no sha256 for {rel}")
            continue
        actual = sha256_file(path)
        if actual != expected:
            errors.append(f"mismatch: {rel} expected {expected} got {actual}")
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model_dir_positional", nargs="?", type=Path)
    ap.add_argument("--model-dir", type=Path, default=None)
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = ap.parse_args(argv)
    model_dir = args.model_dir or args.model_dir_positional
    if model_dir is None:
        ap.error("model_dir required")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    errors = verify(model_dir, manifest)
    if errors:
        print("VERIFY FAILED:")
        for e in errors:
            print(f"  {e}")
        return 1
    print(f"verified {len(manifest['files'])} files under {model_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
