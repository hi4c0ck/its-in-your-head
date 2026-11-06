"""S2 Tier-1 model manifest tests (LANE A, hermetic — no network).

Covers: manifest schema/shape, verify_onnx_model hash-mismatch refusal on
fake files, encoder constants consistent with the manifest, and
TIER1_MODEL_VERSION format. The manifest's file hashes may still be null
(placeholder) in a fresh checkout — those tests only assert shape, never
real hashes (no model binaries in git).
"""
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "onnx" / "manifest.json"
sys.path.insert(0, str(ROOT))

from perception import encoder  # noqa: E402


def _load_tool(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "tools" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _fake_model_dir(tmp_path):
    """Create fake artifacts + a variant manifest matching them."""
    files = {}
    for rel in ["onnx/model_int8.onnx", "tokenizer.json"]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(f"fake-{rel}".encode())
        files[rel] = {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                      "size": p.stat().st_size}
    manifest = {"default_variant": "int8",
                "variants": {"int8": {"files": files}}}
    return tmp_path, manifest


# Pre-existing pins (schema v1 `files` map) that must survive the v2 variant
# restructure byte-identical: (sha256, size) per artifact.
EXPECTED_PINS = {
    "onnx/model_int8.onnx": ("0029fce9c82365d8a2bf20e03a476e84785a872d8d423c8bac0fd0f350df88dc", 118049319),
    "onnx/model.onnx": ("6fef515473041f6f40d7e5f630106e59ff50b22e1b1fccae39199b1d9f8ec03a", 470216988),
    "tokenizer.json": ("cad551d5600a84242d0973327029452a1e3672ba6313c2a3c3d69c4310e12719", 17082987),
    "tokenizer_config.json": ("3a8d9bef4da7fc3ac71da789f225b47dc7599a28cfdcc2140463771927dc0726", 1457),
    "config.json": ("376a54b28d37e8502389a87d6c8ff59247fc361cd8d980a47b36b6cae876de5e", 613),
    "special_tokens_map.json": ("8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835", 964),
}


# --- manifest schema/shape -------------------------------------------------

def test_manifest_exists_and_is_json():
    assert MANIFEST_PATH.is_file()
    _manifest()


def test_manifest_schema_version_and_identity():
    m = _manifest()
    assert m["schema_version"] == 2
    assert m["plugin_id"] == "its-in-your-head"
    assert m["backend"] == "onnxruntime"
    assert m["model_id"] == "onnx-community/paraphrase-multilingual-MiniLM-L12-v2-ONNX"
    assert m["base_model"] == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    assert m["execution_provider"] == "CPUExecutionProvider"
    assert m["runtime"] == {"package": "onnxruntime", "constraint": ">=1.31.1,<1.32"}


def test_manifest_license_recorded():
    m = _manifest()
    assert m["license"].lower() == "apache-2.0"


def test_manifest_revision_is_immutable_sha():
    m = _manifest()
    assert re.fullmatch(r"[0-9a-f]{40}", m["revision"]), m["revision"]


def test_manifest_variant_layout():
    m = _manifest()
    assert m["default_variant"] == "int8"
    variants = m["variants"]
    assert set(variants) == {"int8", "fp32"}
    int8_files = set(variants["int8"]["files"])
    assert int8_files == {"onnx/model_int8.onnx", "tokenizer.json",
                          "tokenizer_config.json", "config.json",
                          "special_tokens_map.json"}  # int8 default (~135 MB)
    assert set(variants["fp32"]["files"]) == {"onnx/model.onnx"}  # fp32 only
    for variant in variants.values():
        for rel, meta in variant["files"].items():
            assert set(meta) == {"sha256", "size"}
            sha = meta["sha256"]
            assert sha is None or re.fullmatch(r"[0-9a-f]{64}", sha)
            size = meta["size"]
            assert size is None or (isinstance(size, int) and size > 0)
    assert "files" not in m  # v1 flat map is gone


def test_manifest_pins_unchanged_by_variant_restructure():
    m = _manifest()
    seen = {}
    for variant in m["variants"].values():
        for rel, meta in variant["files"].items():
            seen[rel] = (meta["sha256"], meta["size"])
    assert seen == EXPECTED_PINS  # all 6 pre-existing pins byte-identical
    # int8 default total ~135 MB; fp32 graph ~470 MB standalone.
    int8_total = sum(seen[r][1] for r in m["variants"]["int8"]["files"])
    assert 130_000_000 < int8_total < 140_000_000, int8_total
    assert seen["onnx/model.onnx"][1] == 470216988


def test_manifest_embedding_block():
    m = _manifest()
    assert m["embedding"] == {"dimension": 384, "max_sequence_length": 128,
                              "pooling": "mean_attention_mask", "normalize": True}


# --- encoder constants vs manifest ------------------------------------------

def test_encoder_constants_match_manifest():
    m = _manifest()
    assert encoder.TIER1_MODEL_ID == m["model_id"]
    assert encoder.TIER1_REVISION == m["revision"]
    assert encoder.TIER1_DEFAULT_VARIANT == m["default_variant"] == "int8"
    assert set(encoder.TIER1_ARTIFACTS) == set(m["variants"]["int8"]["files"])
    union = set()
    for name, files in encoder.TIER1_VARIANTS.items():
        assert set(files) == set(m["variants"][name]["files"])
        union |= set(files)
    assert union == {"onnx/model_int8.onnx", "onnx/model.onnx", "tokenizer.json",
                     "tokenizer_config.json", "config.json",
                     "special_tokens_map.json"}


def test_tier1_model_version_format():
    short = encoder.TIER1_REVISION[:12]
    assert re.fullmatch(r"[0-9a-f]{12}", short)
    assert encoder.TIER1_MODEL_VERSION == f"{encoder.TIER1_MODEL_ID}@rev-{short}"
    assert encoder.TIER1_MODEL_VERSION.startswith("onnx-community/")


def test_tier1_hf_base_matches_artifact_keys():
    url = encoder.TIER1_HF_BASE.format(
        model=encoder.TIER1_MODEL_ID, rev=encoder.TIER1_REVISION,
        name="onnx/model_int8.onnx")
    assert url.endswith("/resolve/" + encoder.TIER1_REVISION + "/onnx/model_int8.onnx")
    assert "/onnx/onnx/" not in url


# --- verify_onnx_model against fake files -----------------------------------

def test_verify_accepts_matching_fake_files(tmp_path):
    verify_mod = _load_tool("verify_onnx_model")
    model_dir, manifest = _fake_model_dir(tmp_path)
    assert verify_mod.verify(model_dir, manifest) == []


def test_verify_refuses_hash_mismatch(tmp_path):
    verify_mod = _load_tool("verify_onnx_model")
    model_dir, manifest = _fake_model_dir(tmp_path)
    (model_dir / "tokenizer.json").write_bytes(b"tampered")
    errors = verify_mod.verify(model_dir, manifest)
    assert any("mismatch" in e and "tokenizer.json" in e for e in errors)


def test_verify_refuses_missing_file(tmp_path):
    verify_mod = _load_tool("verify_onnx_model")
    model_dir, manifest = _fake_model_dir(tmp_path)
    (model_dir / "onnx" / "model_int8.onnx").unlink()
    errors = verify_mod.verify(model_dir, manifest)
    assert any("missing" in e for e in errors)


def test_verify_refuses_missing_manifest_hash(tmp_path):
    verify_mod = _load_tool("verify_onnx_model")
    model_dir, manifest = _fake_model_dir(tmp_path)
    manifest["variants"]["int8"]["files"]["tokenizer.json"]["sha256"] = None
    errors = verify_mod.verify(model_dir, manifest)
    assert any("no sha256" in e for e in errors)


def test_verify_cli_exit_codes(tmp_path):
    verify_mod = _load_tool("verify_onnx_model")
    model_dir, manifest = _fake_model_dir(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert verify_mod.main(["--model-dir", str(model_dir),
                            "--manifest", str(manifest_path)]) == 0
    (model_dir / "tokenizer.json").write_bytes(b"tampered")
    assert verify_mod.main(["--model-dir", str(model_dir),
                            "--manifest", str(manifest_path)]) == 1


# --- fetch --check refuses mismatching pre-existing hashes -------------------

def test_fetch_check_refuses_mismatch(tmp_path):
    fetch_mod = _load_tool("fetch_onnx_model")
    model_dir, manifest = _fake_model_dir(tmp_path)
    manifest["variants"]["int8"]["files"]["tokenizer.json"]["sha256"] = "0" * 64
    errors = fetch_mod.check_hashes(model_dir, manifest)
    assert any("mismatch" in e for e in errors)
    manifest["variants"]["int8"]["files"]["tokenizer.json"]["sha256"] = (
        hashlib.sha256((model_dir / "tokenizer.json").read_bytes()).hexdigest())
    assert fetch_mod.check_hashes(model_dir, manifest) == []


def test_safe_model_dirname():
    fetch_mod = _load_tool("fetch_onnx_model")
    assert fetch_mod.safe_model_dirname("a/b") == "a--b"


# --- worker / pyproject shape -------------------------------------------------

def test_worker_pyproject_constraints():
    text = (ROOT / "onnx-worker" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "its-in-your-head-onnx"' in text
    assert 'requires-python = ">=3.11,<3.15"' in text
    assert "onnxruntime>=1.31.1,<1.32" in text
    assert "onnxruntime-gpu" not in text
    assert (ROOT / "onnx-worker" / "worker.py").is_file()
    assert (ROOT / "onnx-worker" / "uv.lock").is_file()
    lock = (ROOT / "onnx-worker" / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "onnxruntime"' in lock
    assert "onnxruntime-gpu" not in lock


def test_worker_protocol_ops_documented():
    text = (ROOT / "onnx-worker" / "worker.py").read_text(encoding="utf-8")
    for op in ('"embed"', '"ping"', '"shutdown"'):
        assert op in text
    assert "CPUExecutionProvider" in text
    assert "mean_attention_mask" not in text  # pooling is code, not a string
    assert "tokenizers" in text  # tokenizer.json via tokenizers lib, no transformers


def test_manifest_no_model_binaries_in_repo():
    # Only manifest.json under onnx/ — never model binaries.
    assert sorted(p.name for p in (ROOT / "onnx").iterdir()) == ["manifest.json"]


# --- F3: fetch no-flag = verify-only; --repin stages + atomically promotes -----

def _f3_manifest_dir(tmp_path):
    """Fake pinned manifest + matching fake model dir under tmp_path."""
    model_dir = tmp_path / "model"
    files = {}
    for rel in ["onnx/model_int8.onnx", "tokenizer.json"]:
        p = model_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(f"fake-{rel}".encode())
        files[rel] = {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                      "size": p.stat().st_size}
    manifest = {"model_id": "fake/model", "revision": "b" * 40,
                "embedding": {"dimension": 384}, "default_variant": "int8",
                "variants": {"int8": {"files": files}}}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path, model_dir


def test_f3_no_flag_verify_only_leaves_manifest_identical(tmp_path, monkeypatch):
    import os as _os  # local import for replace spy typing
    fetch_mod = _load_tool("fetch_onnx_model")
    manifest_path, model_dir = _f3_manifest_dir(tmp_path)
    monkeypatch.setattr(fetch_mod, "MANIFEST_PATH", manifest_path)
    calls = []
    monkeypatch.setattr(fetch_mod, "resolve_revision",
                        lambda mid: calls.append("resolve") or (_ for _ in ()).throw(AssertionError("resolve_revision must not be called")))
    monkeypatch.setattr(fetch_mod, "download",
                        lambda url, dest: calls.append("download") or (_ for _ in ()).throw(AssertionError("download must not be called")))
    monkeypatch.setattr(fetch_mod, "run_smoke",
                        lambda *a, **k: calls.append("smoke") or (_ for _ in ()).throw(AssertionError("smoke must not run")))
    before = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert fetch_mod.main(["--model-dir", str(model_dir)]) == 0
    # --check is the same verify-only path
    assert fetch_mod.main(["--check", "--model-dir", str(model_dir)]) == 0
    assert calls == []
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == before
    assert not manifest_path.with_name("manifest.staged.json").exists()
    del _os


def test_f3_no_flag_mismatch_exits_1_no_resolve(tmp_path, monkeypatch):
    fetch_mod = _load_tool("fetch_onnx_model")
    manifest_path, model_dir = _f3_manifest_dir(tmp_path)
    monkeypatch.setattr(fetch_mod, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(fetch_mod, "resolve_revision",
                        lambda mid: (_ for _ in ()).throw(AssertionError("no resolve in verify-only")))
    (model_dir / "tokenizer.json").write_bytes(b"tampered")
    before = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert fetch_mod.main(["--model-dir", str(model_dir)]) == 1
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == before


def _f3_repin_harness(tmp_path, monkeypatch, fetch_mod, smoke_result=None, smoke_error=None):
    manifest_path, model_dir = _f3_manifest_dir(tmp_path)
    monkeypatch.setattr(fetch_mod, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(fetch_mod, "resolve_revision", lambda mid: "a" * 40)
    def _fake_download(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(f"staged-{dest.name}".encode())
    monkeypatch.setattr(fetch_mod, "download", _fake_download)
    monkeypatch.setattr(fetch_mod, "validate_onnx_signature",
                        lambda model_dir_, manifest_: {"inputs": {}, "outputs": {}, "hidden_size": 384, "tokenizer_class": "Fake"})
    if smoke_error is not None:
        def _bad_smoke(model_dir_, manifest_path_):
            raise smoke_error
        monkeypatch.setattr(fetch_mod, "run_smoke", _bad_smoke)
    else:
        monkeypatch.setattr(fetch_mod, "run_smoke",
                            lambda model_dir_, manifest_path_: smoke_result or {"ok": True})
    return manifest_path, model_dir


def test_f3_repin_success_promotes_atomically(tmp_path, monkeypatch):
    import os as _os
    fetch_mod = _load_tool("fetch_onnx_model")
    manifest_path, model_dir = _f3_repin_harness(tmp_path, monkeypatch, fetch_mod)
    staged = manifest_path.with_name("manifest.staged.json")
    real_replace = _os.replace
    captured = {}
    def _spy_replace(src, dst):
        captured["src"] = Path(src)
        captured["dst"] = Path(dst)
        captured["bytes"] = Path(src).read_bytes()
        return real_replace(src, dst)
    monkeypatch.setattr(_os, "replace", _spy_replace)
    assert fetch_mod.main(["--repin", "--model-dir", str(model_dir)]) == 0
    assert captured["dst"] == manifest_path and captured["src"] == staged
    # byte-wise: promoted manifest == staged bytes captured pre-rename
    assert manifest_path.read_bytes() == captured["bytes"]
    promoted = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert promoted["revision"] == "a" * 40
    assert not staged.exists()
    # v0.1.3: the RUNTIME artifacts are promoted into the live dir too —
    # live bytes == staged bytes, so the promoted manifest's pins match
    # what the runtime will actually load.
    assert (model_dir / "onnx/model_int8.onnx").read_bytes() == b"staged-model_int8.onnx"
    assert (model_dir / "tokenizer.json").read_bytes() == b"staged-tokenizer.json"
    # Integration chain per the review: promoted manifest verifies the live
    # dir, and the no-flag (verify-only) run passes against it.
    assert fetch_mod.check_hashes(model_dir, promoted) == []
    assert fetch_mod.main(["--model-dir", str(model_dir)]) == 0
    # The staging dir is cleaned up after a successful promotion.
    assert not (Path(str(model_dir) + ".staged")).exists()


def test_f3_repin_smoke_failure_cleans_up(tmp_path, monkeypatch):
    fetch_mod = _load_tool("fetch_onnx_model")
    manifest_path, model_dir = _f3_repin_harness(
        tmp_path, monkeypatch, fetch_mod, smoke_error=RuntimeError("smoke boom"))
    before = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    before_bytes = manifest_path.read_bytes()
    assert fetch_mod.main(["--repin", "--model-dir", str(model_dir)]) == 1
    assert manifest_path.read_bytes() == before_bytes
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == before
    assert not manifest_path.with_name("manifest.staged.json").exists()
    assert not (Path(str(model_dir) + ".staged")).exists()


def test_f3_repin_verify_failure_cleans_up(tmp_path, monkeypatch):
    fetch_mod = _load_tool("fetch_onnx_model")
    manifest_path, model_dir = _f3_repin_harness(tmp_path, monkeypatch, fetch_mod)
    monkeypatch.setattr(fetch_mod, "check_hashes",
                        lambda model_dir_, manifest_, variant=None: ["mismatch: tokenizer.json expected x got y"])
    before_bytes = manifest_path.read_bytes()
    assert fetch_mod.main(["--repin", "--model-dir", str(model_dir)]) == 1
    assert manifest_path.read_bytes() == before_bytes
    assert not manifest_path.with_name("manifest.staged.json").exists()
    assert not (Path(str(model_dir) + ".staged")).exists()


def test_f3_repin_crash_before_manifest_leaves_mixed_state(tmp_path, monkeypatch):
    import os as _os
    """A crash AFTER artifact promotion but BEFORE the manifest rename must
    leave: pinned manifest byte-identical, live dir holding the NEW bytes
    (the documented mixed state), and the no-flag verify honestly reporting
    exit 1 (new live bytes vs old pins)."""
    fetch_mod = _load_tool("fetch_onnx_model")
    manifest_path, model_dir = _f3_repin_harness(tmp_path, monkeypatch, fetch_mod)
    pinned_bytes = manifest_path.read_bytes()
    real_replace = _os.replace

    def _crash_on_manifest(src, dst):
        if Path(dst) == manifest_path:
            raise OSError("simulated crash before manifest promotion")
        return real_replace(src, dst)

    monkeypatch.setattr(_os, "replace", _crash_on_manifest)
    assert fetch_mod.main(["--repin", "--model-dir", str(model_dir)]) == 1
    # Pinned manifest untouched.
    assert manifest_path.read_bytes() == pinned_bytes
    # Live artifacts already promoted (the documented mixed state).
    assert (model_dir / "onnx/model_int8.onnx").read_bytes() == b"staged-model_int8.onnx"
    # No-flag verify honestly flags the mismatch against the OLD pins.
    assert fetch_mod.main(["--model-dir", str(model_dir)]) == 1
    # Staged artifacts cleaned up on the failure path.
    assert not manifest_path.with_name("manifest.staged.json").exists()
    assert not (Path(str(model_dir) + ".staged")).exists()


def test_f3_repin_removes_stale_live_artifacts(tmp_path, monkeypatch):
    """Files the OLD variant set carried but the promoted set dropped must be
    removed from the live dir, so it matches the promoted manifest exactly."""
    fetch_mod = _load_tool("fetch_onnx_model")
    manifest_path, model_dir = _f3_repin_harness(tmp_path, monkeypatch, fetch_mod)
    pinned = json.loads(manifest_path.read_text(encoding="utf-8"))
    pinned["variants"]["int8"]["files"]["onnx/legacy.txt"] = {"sha256": None, "size": None}
    manifest_path.write_text(json.dumps(pinned, indent=2) + "\n", encoding="utf-8")
    (model_dir / "onnx/legacy.txt").write_bytes(b"old")
    # The staged variant resolves to the smaller set (new revision dropped
    # onnx/legacy.txt).
    def _variant_files(m, v=None):
        files = m["variants"]["int8"]["files"]
        if m.get("revision") == "a" * 40:
            # Mutate the manifest's OWN map so the staged manifest (and the
            # pin-recompute loop over it) sees the smaller set.
            files.pop("onnx/legacy.txt", None)
        return "int8", files

    monkeypatch.setattr(fetch_mod, "variant_files", _variant_files)
    assert fetch_mod.main(["--repin", "--model-dir", str(model_dir)]) == 0
    assert not (model_dir / "onnx/legacy.txt").exists()
    # The remaining live files are the promoted ones.
    assert (model_dir / "tokenizer.json").read_bytes() == b"staged-tokenizer.json"
