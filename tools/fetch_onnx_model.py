"""Maintainer-only: pinned Tier-1 model fetch + manifest hash publication.

Explicit maintainer action (deliberate network use, NOT the zero-network
encode-time rule). Two modes:

VERIFY-ONLY (default, no flags — F3):
  python tools/fetch_onnx_model.py            # verify ONLY (no network)
  python tools/fetch_onnx_model.py --check    # same as above (back-compat)
  python tools/fetch_onnx_model.py --model-dir <path> [--variant <name>]

  Verifies the staged model dir against the PINNED onnx/manifest.json
  (sha256 + size, default variant: manifest default_variant, "int8").
  NEVER resolves a new revision, NEVER downloads, NEVER rewrites the
  manifest. Exit 0 = every pinned file verified; exit 1 = any pin
  mismatch/missing (each error printed). No smoke inference in this mode.

EXPLICIT REPIN:
  python tools/fetch_onnx_model.py --repin [--variant <name>] [--model-dir <path>]

  1. Resolve the current HF revision (network; the only network use).
  2. Stage a NEW manifest NEXT TO the pinned one,
     onnx/manifest.staged.json, with the resolved revision and fresh
     sha256+size pins for the chosen variant set.
  3. Download+verify the variant set into a staging dir
     (<model_dir>.staged, same parent) — never into the live model dir.
  4. Validate the ONNX signature (int8 only) and run CPU smoke inference
     against the STAGED pins.
  5. ONLY after verify+smoke pass: promote — artifacts FIRST (each staged
     file os.replace()'d into the live model dir, overwriting stale
     same-named files; stale live files the new variant set dropped are
     removed), manifest LAST: os.replace(manifest.staged.json ->
     manifest.json) on the same volume (single rename; readers never see
     a partial file; the old pinned bytes are swapped in one step).
     On ANY failure the pinned manifest stays byte-identical and staged
     artifacts (the staged manifest file and the staging dir) are
     removed. Exit 0 promoted, exit 1 any failure.
  CRASH STATE: a crash AFTER some artifact promotions but BEFORE the
  manifest promotion leaves a mixed live dir (new bytes where promoted,
  old bytes elsewhere) against the OLD manifest — a no-flag verify
  honestly reports exit 1; remediation: re-run --repin (the leftover
  staging dir is reused) or re-download the pinned variant set via the
  runtime installer.

  The runtime installer (perception/encoder.py) is untouched by this tool:
  it stays byte-pinned and reads the promoted manifest only.

Flag/exit-code contract:
  (none)/--check  verify-only  exit 0 verified / 1 mismatch-or-missing
  --repin         repin        exit 0 promoted / 1 any failure
  --check+--repin is an error (exit 1).
  Unknown variant: exit 1 (no revision resolved, manifest untouched).
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
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
        size = meta.get("size")
        if size is not None and path.stat().st_size != size:
            errors.append(
                f"size mismatch: {rel} expected {size} got {path.stat().st_size}")
            continue
        if expected is None:
            continue
        actual = sha256_file(path)
        if actual != expected:
            errors.append(f"mismatch: {rel} expected {expected} got {actual}")
    return errors


def staged_manifest_path(pinned: Path | None = None) -> Path:
    """Staged manifest lives next to the pinned one (same volume)."""
    return Path(pinned or MANIFEST_PATH).with_name("manifest.staged.json")


def staging_model_dir(model_dir: Path) -> Path:
    """Download target for a repin; sibling of the live model dir."""
    return Path(str(model_dir) + ".staged")


def _verify_only(model_dir: Path, manifest: dict, variant: str) -> int:
    print(f"model_dir: {model_dir}")
    print(f"variant: {variant}")
    errors = check_hashes(model_dir, manifest, variant)
    if errors:
        print("HASH CHECK FAILED:")
        for e in errors:
            print(f"  {e}")
        return 1
    print("hash check OK")
    return 0


def _cleanup_staged(staged: Path, staging_dir: Path) -> None:
    try:
        staged.unlink(missing_ok=True)  # type: ignore[call-arg]
    except TypeError:  # pragma: no cover - very old Python
        if staged.is_file():
            staged.unlink()
    if staging_dir.is_dir():
        shutil.rmtree(staging_dir)


def _repin(model_dir: Path, manifest: dict, variant: str) -> int:
    pinned = Path(MANIFEST_PATH)
    staged = staged_manifest_path(pinned)
    staging_dir = staging_model_dir(model_dir)
    try:
        revision = resolve_revision(manifest["model_id"])
        print(f"resolved revision: {revision}")
        staged_manifest = copy.deepcopy(manifest)
        staged_manifest["revision"] = revision
        _name, files = variant_files(staged_manifest, variant)
        base = f"https://huggingface.co/{manifest['model_id']}/resolve/{revision}"
        for rel in files:
            dest = staging_dir / rel
            if dest.is_file():
                print(f"skip (present): {rel}")
                continue
            print(f"download: {rel}")
            download(f"{base}/{rel}", dest)
        for rel in files:
            path = staging_dir / rel
            staged_manifest["variants"][variant]["files"][rel] = {
                "sha256": sha256_file(path),
                "size": path.stat().st_size}
            print(f"sha256 {rel}: "
                  f"{staged_manifest['variants'][variant]['files'][rel]['sha256']} "
                  f"({staged_manifest['variants'][variant]['files'][rel]['size']} bytes)")
        errors = check_hashes(staging_dir, staged_manifest, variant)
        if errors:
            print("HASH CHECK FAILED:")
            for e in errors:
                print(f"  {e}")
            _cleanup_staged(staged, staging_dir)
            return 1
        print("hash check OK (staged)")
        if variant == "int8":
            sig = validate_onnx_signature(staging_dir, staged_manifest)
            print(f"ONNX signature OK: inputs={sig['inputs']} outputs={sig['outputs']} "
                  f"hidden_size={sig['hidden_size']} tokenizer={sig['tokenizer_class']}")
        else:
            print(f"ONNX signature skipped for variant {variant!r} "
                  f"(runtime graph is the int8 variant)")
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(
            json.dumps(staged_manifest, indent=2) + "\n", encoding="utf-8")
        print(f"staged manifest written: {staged}")
        smoke = run_smoke(staging_dir, staged)
        print(f"smoke OK: {json.dumps(smoke)}")
        # v0.1.3 PROMOTION ORDER: artifacts first, manifest LAST. The live
        # model dir must hold the NEW bytes before the manifest starts
        # pinning the new hashes (a manifest-only promotion left the
        # runtime dir with old bytes that no longer match the promoted
        # manifest). os.replace is a same-volume atomic rename and
        # overwrites an existing destination (MoveFileEx REPLACE_EXISTING
        # on Windows) — readers never see a partial file.
        _name, files = variant_files(staged_manifest, variant)
        for rel in files:
            src = staging_dir / rel
            dest = model_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dest)
            print(f"promoted artifact: {rel} -> {dest}")
        # Stale live artifacts: files the OLD variant set carried but the
        # promoted set no longer does (a newer revision dropped them).
        old_files = ((manifest.get("variants") or {}).get(variant) or {}).get("files") or {}
        for rel in set(old_files) - set(files):
            stale = model_dir / rel
            if stale.is_file():
                stale.unlink()
                print(f"removed stale live artifact: {rel}")
        # Manifest promoted LAST (single atomic rename, same volume).
        os.replace(staged, pinned)
        print(f"manifest promoted atomically: {staged} -> {pinned}")
        _cleanup_staged(staged, staging_dir)
        print(f"staged dir cleaned: {staging_dir}")
        return 0
    except Exception as exc:  # any failure: pinned manifest byte-identical
        print(f"repin FAILED: {exc}")
        _cleanup_staged(staged, staging_dir)
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="verify staged files against committed manifest hashes "
                         "(same as bare no-flag mode)")
    ap.add_argument("--repin", action="store_true",
                    help="explicitly repin: resolve HF revision, stage+verify+"
                         "smoke, then promote artifacts into the live model "
                         "dir and atomically promote manifest.staged.json "
                         "(artifacts first, manifest last)")
    ap.add_argument("--model-dir", type=Path, default=None)
    ap.add_argument("--variant", default=None,
                    help="manifest variant to verify/repin (default: manifest default_variant)")
    args = ap.parse_args(argv)
    if args.check and args.repin:
        print("error: --check and --repin are mutually exclusive")
        return 1

    manifest = json.loads(Path(MANIFEST_PATH).read_text(encoding="utf-8"))
    try:
        variant, _files = variant_files(manifest, args.variant)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    if args.model_dir is not None:
        model_dir = args.model_dir
    else:
        data_root = resolve_data_root()
        print(f"data_root: {data_root}")
        model_dir = (
            data_root / "models" / safe_model_dirname(manifest["model_id"]))

    if args.repin:
        return _repin(model_dir, manifest, variant)
    return _verify_only(model_dir, manifest, variant)


if __name__ == "__main__":
    raise SystemExit(main())
