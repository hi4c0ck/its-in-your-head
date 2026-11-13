"""Regression: ONNX worker env isolation + no PM provisioning on encode.

Review items (post-b83e13f):
- The worker subprocess must not inherit Hermes provider keys,
  *_TOKEN / *_SECRET, PYTHONPATH or PYTHONHOME: Popen gets an
  allowlisted env built by worker_client._worker_env().
- The encode path must not call ensure_project_environment (no
  provisioning, no installs); it uses the read-only
  pm.environment_python() lookup and degrades to Tier-0 when the env
  is not provisioned (only /mirror engine enable and maintainer
  --repin provision, both explicit).
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from perception import worker_client
from perception.worker_client import OnnxWorkerClient

ROOT = Path(__file__).resolve().parent.parent

SECRETS = {
    "IYH_TEST_PROVIDER_API_KEY": "sk-parent-secret",
    "IYH_TEST_AUTH_TOKEN": "tok-parent",
    "IYH_TEST_DB_SECRET": "sec-parent",
    "PYTHONPATH": str(ROOT),
    "PYTHONHOME": "C:\\fake-pythonhome",
}

FAKE_WORKER = '''
import json, sys, math
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    op = req.get("op")
    rid = req.get("request_id")
    if op == "shutdown":
        break
    if op == "ping":
        print(json.dumps({"request_id": rid, "ok": True,
                          "model_version": "fake-model@rev-abc",
                          "provider": "CPUExecutionProvider"}), flush=True)
        continue
    if op == "embed":
        vectors = []
        for t in req.get("texts", []):
            base = (len(t) % 10) / 100.0
            v = [base, 0.6, 0.8]
            n = math.sqrt(sum(x*x for x in v)) or 1.0
            vectors.append([x/n for x in v])
        print(json.dumps({"request_id": rid, "vectors": vectors,
                          "model_version": "fake-model@rev-abc", "dim": 3,
                          "latency_ms": 1.5}), flush=True)
        continue
    print(json.dumps({"request_id": rid, "error": f"ValueError: unknown op: {op!r}"}), flush=True)
'''


# -- 1. isolated worker env ------------------------------------------------------

def test_worker_env_excludes_secrets_and_python(monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    env = worker_client._worker_env()
    for k in SECRETS:
        assert k not in env, f"{k} leaked into the worker env"
    # allowlist names are present with parent values iff the parent has them
    for name in ("PATH", "HOME", "LANG", "LC_ALL"):
        if name in os.environ:
            assert env.get(name) == os.environ[name]
        else:
            assert name not in env
    if os.name == "nt":
        for name in ("SYSTEMROOT", "TEMP", "TMP"):
            if name in os.environ:
                assert env.get(name) == os.environ[name]
            else:
                assert name not in env


def test_popen_isolation_end_to_end(monkeypatch):
    # A secret set in the parent must be invisible to a child started
    # with the same env dict the client hands to Popen.
    monkeypatch.setenv("IYH_TEST_PARENT_SECRET", "s3cret")
    code = ("import os, sys; sys.stdout.write("
            "'LEAK' if os.environ.get('IYH_TEST_PARENT_SECRET') else 'CLEAN')")
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True,
        env=worker_client._worker_env())
    assert out.returncode == 0, out.stderr
    assert out.stdout == "CLEAN"


def test_embed_worker_started_under_isolated_env(tmp_path):
    # The fake worker must start and serve with the allowlisted env —
    # the venv python resolves its site-packages without PYTHONPATH.
    mdir, mp = _manifest(tmp_path)
    fake = _fake_worker(tmp_path)
    pm_fake = _FakePm(python=sys.executable)
    c = OnnxWorkerClient(model_dir=mdir, manifest_path=mp, worker_path=fake,
                         interpreter=None, timeout_s=10, pm_module=pm_fake)
    try:
        res = c.embed(["hello", "world!"])
        assert res.ok, res.failure
        assert len(res.vectors) == 2
        assert pm_fake.ensure_calls == 0
    finally:
        c.shutdown()


# -- 2. no provisioning on the encode path ---------------------------------------

class _FakePm:
    """Mimics the public pm facade; any provisioning attempt is a failure."""

    def __init__(self, python=None, read_error=None):
        self._python = python
        self._read_error = read_error
        self.ensure_calls = 0
        self.lookup_calls = 0

    def environment_python(self, name):
        assert name == worker_client.PM_ENV_NAME
        self.lookup_calls += 1
        if self._read_error is not None:
            raise self._read_error
        return self._python

    def ensure_project_environment(self, *a, **k):
        self.ensure_calls += 1
        raise AssertionError("provisioning must never run on the encode path")


def _manifest(tmp_path: Path):
    mdir = tmp_path / "model"
    (mdir / "onnx").mkdir(parents=True)
    (mdir / "onnx" / "model_int8.onnx").write_bytes(b"fake-model")
    (mdir / "tokenizer.json").write_bytes(b"{}")
    files = {
        "onnx/model_int8.onnx": {"sha256": hashlib.sha256(b"fake-model").hexdigest(), "size": 10},
        "tokenizer.json": {"sha256": hashlib.sha256(b"{}").hexdigest(), "size": 2},
    }
    manifest = {"schema_version": 2, "model_id": "fake-model", "revision": "abc",
                "default_variant": "int8", "variants": {"int8": {"files": files}},
                "embedding": {"dimension": 384}}
    mp = tmp_path / "manifest.json"
    mp.write_text(json.dumps(manifest), encoding="utf-8")
    return mdir, mp


def _fake_worker(tmp_path: Path) -> Path:
    fake = tmp_path / "fake_worker.py"
    fake.write_text(FAKE_WORKER, encoding="utf-8")
    return fake


def _client(tmp_path: Path, pm_fake, timeout_s: float = 5):
    mdir, mp = _manifest(tmp_path)
    return OnnxWorkerClient(model_dir=mdir, manifest_path=mp,
                            worker_path=_fake_worker(tmp_path),
                            interpreter=None, timeout_s=timeout_s,
                            pm_module=pm_fake)


def test_encode_degrades_when_env_not_provisioned(tmp_path):
    pm_fake = _FakePm(python=None)
    c = _client(tmp_path, pm_fake)
    res = c.embed(["hello"])
    assert not res.ok
    assert res.failure.kind == "runtime-unavailable"
    assert "not provisioned" in res.failure.detail
    assert pm_fake.ensure_calls == 0
    assert pm_fake.lookup_calls == 1


def test_encode_degrades_when_selection_record_corrupt(tmp_path):
    pm_fake = _FakePm(python=None, read_error=RuntimeError("corrupt active.json"))
    c = _client(tmp_path, pm_fake)
    res = c.embed(["hello"])
    assert not res.ok
    assert res.failure.kind == "runtime-unavailable"
    assert "PM env read failed" in res.failure.detail
    assert pm_fake.ensure_calls == 0


def test_pm_facing_lacks_readonly_api_degrades(tmp_path):
    class BarePm:  # legacy facade without environment_python
        def ensure_project_environment(self, *a, **k):
            return sys.executable
    c = _client(tmp_path, BarePm())
    res = c.embed(["hello"])
    assert not res.ok
    assert res.failure.kind == "runtime-unavailable"
    assert "lacks environment_python" in res.failure.detail


# -- 3. data dir: no silent home-dir catalog ---------------------------------------

def test_data_root_uses_active_profile_plugin_data(monkeypatch):
    # In the Hermes venv the canonical resolver is plugin_data_dir;
    # it must agree with hermes_constants for the active profile. CI runs a
    # bare Python env with no hermes_constants, so this Hermes-runtime-specific
    # assertion is skipped there (the no-Hermes path is covered by the next
    # test, which blocks the imports and expects the explicit error).
    try:
        import hermes_constants  # noqa: F401
    except ImportError:
        pytest.skip(
            "hermes_constants unavailable (bare / CI env) — this assertion is "
            "Hermes-runtime-specific; the no-Hermes path is covered by "
            "test_data_root_is_explicit_error_without_hermes")
    from hermes_constants import get_hermes_home

    root = worker_client.resolve_data_root()
    expected = Path(get_hermes_home()) / "plugin-data" / worker_client.PLUGIN_ID
    assert root == expected


def test_data_root_is_explicit_error_without_hermes(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *a, **k):
        if name.startswith("plugins") or name.startswith("hermes_constants"):
            raise ImportError("blocked for test")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    with pytest.raises(RuntimeError, match="plugin storage is unavailable"):
        worker_client.resolve_data_root()
