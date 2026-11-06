"""Maintainer-only: pinned Tier-1 model fetch + manifest hash publication.

Explicit maintainer action (deliberate network use, NOT the zero-network
encode-time rule). Steps:

1. Resolve the immutable HF commit SHA for MANIFEST["model_id"] via the HF
   API and pin it in onnx/manifest.json (revision pin first — no hashes are
   ever published from an unpinned download).
2. Download the manifest `files` into a staging dir under the Hermes
   user-data root (same resolution as storage/database.py:
   plugin_data_dir("its-in-your-head")/models/<safe model id>), NEVER into
   the plugin install dir.
3. Compute sha256 + size for every manifest file.
4. Validate ONNX I/O signatures (input names/ranks, output rank-3) and
   tokenizer config consistency (tokenizer.json present, config.json
   model_type/hidden size vs embedding dimension).
5. Run CPU smoke inference (worker.py embed of two texts -> 384-dim
   L2-normalized vectors) in the PM-ensured onnx-worker env.
6. Write the computed hashes back into onnx/manifest.json.

--check mode: recomputes hashes of the installed/staged model dir and
REFUSES (exit 1) if any pre-existing manifest hash mismatches; it never
overwrites a mismatching hash.

Usage:
  python tools/fetch_onnx_model.py           # full fetch (default variant) + smoke + manifest update
  python tools/fetch_onnx_model.py --variant fp32   # fetch only the fp32 variant graph
  python tools/fetch_onnx_model.py --check   # verify staged files against manifest
  python tools/fetch_onnx_model.py --model-dir <path>  # override staging dir
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "onnx" / "manifest.json"
PLUGIN_ID = "its-in-your-head"
HF_API = "https://huggingface.co/api/models/{model}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def resolve_data_root() -> Path:
    """Same resolution as storage/database.py (plugin_storage.plugin_data_dir)."""
    try:
        from plugins.plugin_storage import plugin_data_dir

        return plugin_data_dir(PLUGIN_ID)
    except Exception:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "plugin-data" / PLUGIN_ID


def safe_model_dirname(model_id: str) -> str:
    return model_id.replace("/", "--")


def resolve_revision(model_id: str) -> str:
    with urllib.request.urlopen(HF_API.format(model=model_id), timeout=60) as resp:
        info = json.load(resp)
    sha = info.get("sha")
    if not sha:
        raise RuntimeError(f"HF API returned no sha for {model_id}")
    return sha


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as f:  # noqa: S310
        while True:
            block = resp.read(1 << 20)
            if not block:
                break
            f.write(block)


def validate_onnx_signature(model_dir: Path, manifest: dict) -> dict:
    import onnxruntime as ort

    model = model_dir / "onnx" / "model_int8.onnx"
    session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
    inputs = {i.name: [d if isinstance(d, int) else None for d in i.shape]
              for i in session.get_inputs()}
    outputs = {o.name: [d if isinstance(d, int) else None for d in o.shape]
               for o in session.get_outputs()}
    expected_inputs = {"input_ids", "attention_mask", "token_type_ids"}
    missing = expected_inputs - set(inputs)
    if missing:
        raise ValueError(f"ONNX inputs missing {sorted(missing)}; got {sorted(inputs)}")
    for name, shape in inputs.items():
        if len(shape) != 2:
            raise ValueError(f"ONNX input {name} rank {len(shape)} != 2")
    for name, shape in outputs.items():
        if len(shape) != 3:
            raise ValueError(f"ONNX output {name} rank {len(shape)} != 3")

    cfg = json.loads((model_dir / "config.json").read_text(encoding="utf-8"))
    hidden = cfg.get("hidden_size")
    dim = manifest["embedding"]["dimension"]
    if hidden is not None and hidden != dim:
        raise ValueError(
            f"config.json hidden_size={hidden} != manifest dimension={dim}")
    tok_cfg = json.loads(
        (model_dir / "tokenizer_config.json").read_text(encoding="utf-8"))
    if "tokenizer_class" not in tok_cfg and "model_max_length" not in tok_cfg:
        raise ValueError("tokenizer_config.json lacks tokenizer_class/model_max_length")
    return {"inputs": inputs, "outputs": outputs,
            "hidden_size": hidden, "tokenizer_class": tok_cfg.get("tokenizer_class")}


def run_smoke(model_dir: Path, manifest_path: Path) -> dict:
    """CPU smoke inference through the PM-ensured onnx-worker env."""
    import pm.client

    project = ROOT / "onnx-worker"
    # ensure_project_environment returns the venv interpreter path itself
    # (verified: <root>/.../venv/Scripts/python.exe on Windows).
    python = pm.client.ensure_project_environment(
        "its-in-your-head-onnx", project=project, explicit=True)
    if not Path(python).is_file():
        raise RuntimeError(f"PM env interpreter not found: {python}")
    import subprocess

    req = {"op": "embed",
           "texts": ["hello world", "bonjour le monde"], "request_id": "smoke"}
    proc = subprocess.run(
        [str(python), str(project / "worker.py"), str(model_dir), str(manifest_path)],
        input=json.dumps(req) + "\n", capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"smoke worker exit {proc.returncode}: {proc.stderr.strip()}")
    resp = json.loads(proc.stdout.strip().splitlines()[0])
    if "error" in resp:
        raise RuntimeError(f"smoke worker error: {resp['error']}")
    vectors = resp["vectors"]
    if len(vectors) != 2 or any(len(v) != 384 for v in vectors):
        raise ValueError(f"smoke vectors shape wrong: {[len(v) for v in vectors]}")
    norms = [round(sum(x * x for x in v) ** 0.5, 6) for v in vectors]
    if any(abs(n - 1.0) > 1e-4 for n in norms):
        raise ValueError(f"smoke vectors not L2-normalized: norms={norms}")
    return {"vectors": 2, "dim": resp["dim"], "norms": norms,
            "latency_ms": resp.get("latency_ms"), "python": str(python)}


def variant_files(manifest: dict, variant: str | None = None) -> tuple[str, dict]:
    """Resolve (variant_name, files) — default: manifest's default_variant."""
    name = variant or manifest.get("default_variant") or "int8"
    entry = (manifest.get("variants") or {}).get(name)
    if entry is None:
        raise ValueError(f"manifest has no variant {name!r}")
    return name, entry.get("files") or {}


def check_hashes(model_dir: Path, manifest: dict, variant: str | None = None) -> list[str]:
    errors = []
    _name, files = variant_files(manifest, variant)
    for rel, meta in files.items():
        expected = meta.get("sha256")
        path = model_dir / rel
        if not path.is_file():
            errors.append(f"missing: {rel}")
            continue
        if expected is None:
            continue
        actual = sha256_file(path)
        if actual != expected:
            errors.append(f"mismatch: {rel} expected {expected} got {actual}")
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="verify staged files against committed manifest hashes")
    ap.add_argument("--model-dir", type=Path, default=None)
    ap.add_argument("--variant", default=None,
                    help="manifest variant to fetch/check (default: manifest default_variant)")
    args = ap.parse_args(argv)

    data_root = resolve_data_root()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    try:
        variant, files = variant_files(manifest, args.variant)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    model_dir = args.model_dir or (
        data_root / "models" / safe_model_dirname(manifest["model_id"]))
    print(f"data_root: {data_root}")
    print(f"model_dir: {model_dir}")
    print(f"variant: {variant}")

    if args.check:
        errors = check_hashes(model_dir, manifest, variant)
        if errors:
            print("HASH CHECK FAILED:")
            for e in errors:
                print(f"  {e}")
            return 1
        print("hash check OK")
        return 0

    revision = resolve_revision(manifest["model_id"])
    print(f"resolved revision: {revision}")
    if manifest.get("revision") != revision:
        print(f"pinning revision {manifest.get('revision')} -> {revision}")
        manifest["revision"] = revision

    base = f"https://huggingface.co/{manifest['model_id']}/resolve/{revision}"
    for rel in files:
        dest = model_dir / rel
        if dest.is_file():
            print(f"skip (present): {rel}")
            continue
        print(f"download: {rel}")
        download(f"{base}/{rel}", dest)

    for rel in files:
        path = model_dir / rel
        manifest["variants"][variant]["files"][rel] = {
            "sha256": sha256_file(path),
            "size": path.stat().st_size}
        print(f"sha256 {rel}: {manifest['variants'][variant]['files'][rel]['sha256']} "
              f"({manifest['variants'][variant]['files'][rel]['size']} bytes)")

    if variant == "int8":
        sig = validate_onnx_signature(model_dir, manifest)
        print(f"ONNX signature OK: inputs={sig['inputs']} outputs={sig['outputs']} "
              f"hidden_size={sig['hidden_size']} tokenizer={sig['tokenizer_class']}")
    else:
        print(f"ONNX signature skipped for variant {variant!r} "
              f"(runtime graph is the int8 variant)")

    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"manifest updated: {MANIFEST_PATH}")

    smoke = run_smoke(model_dir, MANIFEST_PATH)
    print(f"smoke OK: {json.dumps(smoke)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
