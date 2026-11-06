"""S8 ONNX Tier-1 worker (LANE B): hermetic fake-worker protocol + fallback + /mirror engine."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

from perception import encoder, worker_client
from perception.worker_client import OnnxWorkerClient

ROOT = Path(__file__).resolve().parent.parent

FAKE_WORKER = '''
import json, sys, os, time, math
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except Exception as e:
        print(json.dumps({"request_id": None, "error": f"JSONDecodeError: {e}"}), flush=True)
        continue
    op = req.get("op")
    rid = req.get("request_id")
    if op == "shutdown":
        break
    if op == "ping":
        print(json.dumps({"request_id": rid, "ok": True,
                          "model_version": "fake-model@rev-abc", "provider": "CPUExecutionProvider"}), flush=True)
        continue
    if op == "embed":
        texts = req.get("texts", [])
        if any(t == "__crash__" for t in texts):
            os._exit(1)
        if any(t == "__sleep__" for t in texts):
            time.sleep(5)
            pass
        if any(t == "__error__" for t in texts):
            print(json.dumps({"request_id": rid, "error": "ValueError: boom"}), flush=True)
            continue
        vectors = []
        for t in texts:
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


def _h(d: Path, rel: str, data: bytes = b"x"):
    p = d / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _manifest(tmp_path: Path, files_data: dict[str, bytes] | None = None):
    """Create model_dir + manifest with matching hashes for listed files."""
    mdir = tmp_path / "model"
    files_data = files_data if files_data is not None else {"onnx/model_int8.onnx": b"fake-model", "tokenizer.json": b"{}"}
    files = {}
    for rel, data in files_data.items():
        files[rel] = {"sha256": _h(mdir, rel, data), "size": len(data)}
    manifest = {"schema_version": 1, "model_id": "fake-model", "revision": "abc",
                "files": files, "embedding": {"dimension": 384}}
    mp = tmp_path / "manifest.json"
    mp.write_text(json.dumps(manifest), encoding="utf-8")
    return mdir, mp


def _fake_client(tmp_path: Path, **kw):
    mdir, mp = _manifest(tmp_path)
    fake = tmp_path / "fake_worker.py"
    fake.write_text(FAKE_WORKER, encoding="utf-8")
    kw.setdefault("interpreter", sys.executable)
    c = OnnxWorkerClient(model_dir=mdir, manifest_path=mp, worker_path=fake,
                         timeout_s=kw.pop("timeout_s", 10), **kw)
    return c, mdir, mp


def test_fake_worker_embed_shape(tmp_path):
    c, _, _ = _fake_client(tmp_path)
    try:
        res = c.embed(["hello", "world!"])
        assert res.ok, res.failure
        assert len(res.vectors) == 2
        assert all(len(v) == 3 for v in res.vectors)
        import math
        for v in res.vectors:
            assert abs(math.sqrt(sum(x * x for x in v)) - 1.0) < 1e-6
        assert res.model_version == "fake-model@rev-abc"
        assert res.dim == 3
        assert res.latency_ms == 1.5
    finally:
        c.shutdown()


def test_fake_worker_ping_shape(tmp_path):
    c, _, _ = _fake_client(tmp_path)
    try:
        res = c.ping()
        assert res.ok
        assert res.model_version == "fake-model@rev-abc"
        assert res.raw["provider"] == "CPUExecutionProvider"
    finally:
        c.shutdown()


def test_fake_worker_error_shape_typed_failure(tmp_path):
    c, _, _ = _fake_client(tmp_path)
    try:
        res = c.embed(["__error__"])
        assert not res.ok
        assert res.failure is not None
        assert res.failure.kind == "protocol-error"
        assert "boom" in res.failure.detail
    finally:
        c.shutdown()


def test_fake_worker_shutdown_clean_exit(tmp_path):
    c, _, _ = _fake_client(tmp_path)
    assert c.ping().ok
    c.shutdown()
    assert c.status()["worker"] == "dead"
    c.shutdown()  # idempotent


def test_pm_unavailable_status_and_tier0_fallback(tmp_path, monkeypatch):
    c, _, _ = _fake_client(tmp_path)
    # Force pm unavailable + no interpreter: runtime must be unavailable.
    c2 = OnnxWorkerClient(model_dir=tmp_path / "m", manifest_path=tmp_path / "nope.json",
                          pm_module=None)
    st = c2.status()
    assert st["runtime"] == "unavailable"
    backend = encoder.create_backend({"perception.encoder": "tier-1-onnx"}, client=c2)
    vecs = backend.encode(["hello world"])
    tier0 = encoder.Tier0SketchBackend().encode(["hello world"])
    assert vecs == tier0
    assert backend.last_failure is not None
    assert backend.model_version == encoder.TIER0_MODEL_VERSION


def test_model_missing_status_and_fallback(tmp_path):
    mdir = tmp_path / "empty-model"
    mdir.mkdir()
    _, mp = _manifest(tmp_path)
    fake = tmp_path / "fake_worker.py"
    fake.write_text(FAKE_WORKER, encoding="utf-8")
    c = OnnxWorkerClient(model_dir=mdir, manifest_path=mp, worker_path=fake,
                         interpreter=sys.executable, timeout_s=5)
    st = c.status()
    assert st["model"] == "missing"
    backend = encoder.OnnxMiniLMBackend(client=c)  # client implies fallback
    vecs = backend.encode(["hello world"])
    assert vecs == encoder.Tier0SketchBackend().encode(["hello world"])
    assert backend.last_failure is not None
    assert "model-missing" in backend.last_failure
    assert backend.model_version == encoder.TIER0_MODEL_VERSION


def test_hash_mismatch_refuses(tmp_path):
    mdir, mp = _manifest(tmp_path)
    # Corrupt a shipped file so its hash differs from the manifest.
    (mdir / "tokenizer.json").write_bytes(b"corrupt")
    fake = tmp_path / "fake_worker.py"
    fake.write_text(FAKE_WORKER, encoding="utf-8")
    c = OnnxWorkerClient(model_dir=mdir, manifest_path=mp, worker_path=fake,
                         interpreter=sys.executable, timeout_s=5)
    st = c.status()
    assert st["model"] == "hash-mismatch", st
    res = c.embed(["hello"])
    assert not res.ok and res.failure.kind == "hash-mismatch"


def test_worker_crash_restarts_once_then_degrades(tmp_path):
    c, _, _ = _fake_client(tmp_path)
    try:
        assert c.ping().ok
        res = c.embed(["__crash__"])  # crash -> one restart -> crash again
        assert not res.ok
        assert res.failure.kind == "worker-dead"
        assert c.status()["degraded"] is True
        # Tier-0 fallback via backend using the same degraded client
        backend = encoder.OnnxMiniLMBackend(client=c)
        vecs = backend.encode(["hello world"])
        assert vecs == encoder.Tier0SketchBackend().encode(["hello world"])
        assert backend.model_version == encoder.TIER0_MODEL_VERSION
    finally:
        c.shutdown()


def test_timeout_falls_back_to_tier0(tmp_path):
    c, _, _ = _fake_client(tmp_path, timeout_s=0.6)
    try:
        res = c.embed(["__sleep__"])
        assert not res.ok
        assert res.failure.kind == "timeout"
        backend = encoder.OnnxMiniLMBackend(client=c)
        vecs = backend.encode(["hello world"])
        assert vecs == encoder.Tier0SketchBackend().encode(["hello world"])
    finally:
        c.shutdown()


def test_worker_success_reports_tier1_version(tmp_path):
    c, _, _ = _fake_client(tmp_path)
    try:
        backend = encoder.OnnxMiniLMBackend(client=c)
        vecs = backend.encode(["hello"])
        assert len(vecs) == 1 and len(vecs[0]) == 3
        assert backend.model_version == "fake-model@rev-abc"
        assert backend.last_failure is None
    finally:
        c.shutdown()


def test_tier0_default_path_unchanged():
    backend = encoder.create_backend()
    assert isinstance(backend, encoder.Tier0SketchBackend)
    assert backend.encode(["hello world"]) == encoder.Tier0SketchBackend().encode(["hello world"])
    b2 = encoder.create_backend({"perception.encoder": "tier-0"})
    assert isinstance(b2, encoder.Tier0SketchBackend)


def test_factory_tier1_honors_c4(tmp_path):
    b = encoder.create_backend({"perception.encoder": "tier-1-onnx"}, data_dir=tmp_path / "d")
    assert isinstance(b, encoder.OnnxMiniLMBackend)
    assert b._fallback_to_tier0 is True


def _ctx_double():
    class Ctx:
        def __init__(self):
            self.config = {}
            self.commands = {}
        def get_config(self, key, default=None):
            return self.config.get(key, default)
        def set_config(self, key, value):
            self.config[key] = value
        def register_command(self, name, fn, **kw):
            self.commands[name] = fn
    return Ctx()


def _commands():
    import commands
    import importlib
    return importlib.reload(commands)


def _set_ephemeral(api, value):
    try:
        api._ephemeral_settings.clear()
    except Exception:
        pass
    try:
        api._ephemeral_settings["perception.encoder"] = value
    except Exception:
        pass


def test_engine_status_text_shape(tmp_path):
    import commands
    import dashboard.plugin_api as api
    ctx = _ctx_double()
    commands.register_commands(ctx)
    _set_ephemeral(api, "tier-0")
    out = ctx.commands["mirror"]("engine status")
    assert "Tier 0" in out and "Tier 1" in out
    assert "ONNX runtime" in out and "Model" in out and "Worker" in out
    assert "perception.encoder" in out
    assert "tier-0" in out


def test_engine_enable_success_flips_setting(tmp_path, monkeypatch):
    import commands
    ctx = _ctx_double()
    commands.register_commands(ctx)
    monkeypatch.setattr(commands, "_op_platform_check", lambda: "Windows/3.14.0")
    monkeypatch.setattr(commands, "_op_pm_env", lambda: sys.executable)
    monkeypatch.setattr(commands, "_op_download_verify", lambda i: str(tmp_path))
    monkeypatch.setattr(commands, "_op_smoke", lambda i, m: "ping+embed ok dim=384")
    out = ctx.commands["mirror"]("engine enable")
    assert "enabled Tier 1" in out and "tier-1-onnx" in out
    assert ctx.config.get("perception.encoder") == "tier-1-onnx"


@pytest.mark.parametrize("step", ["_op_platform_check", "_op_pm_env", "_op_download_verify", "_op_smoke"])
def test_engine_enable_each_failure_leaves_tier0(tmp_path, monkeypatch, step):
    import commands
    ctx = _ctx_double()
    commands.register_commands(ctx)
    monkeypatch.setattr(commands, "_op_platform_check", lambda: "Windows/3.14.0")
    monkeypatch.setattr(commands, "_op_pm_env", lambda: sys.executable)
    monkeypatch.setattr(commands, "_op_download_verify", lambda i: str(tmp_path))
    monkeypatch.setattr(commands, "_op_smoke", lambda i, m: "ok")
    def _boom(*a, **k):
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(commands, step, _boom)
    out = ctx.commands["mirror"]("engine enable")
    assert "tier-0 intact" in out and "stopped" in out
    assert ctx.config.get("perception.encoder") != "tier-1-onnx"


def test_engine_disable_stops_worker_keeps_model(tmp_path, monkeypatch):
    import commands
    ctx = _ctx_double()
    commands.register_commands(ctx)
    keep = tmp_path / "model.onnx"
    keep.write_bytes(b"keepme")
    holder = {"shutdown_called": False}
    class FakeClient:
        def shutdown(self):
            holder["shutdown_called"] = True
    monkeypatch.setattr(commands, "_engine_client", lambda: FakeClient())
    out = ctx.commands["mirror"]("engine disable")
    assert holder["shutdown_called"] is True
    assert "Tier 0" in out and "kept" in out
    assert ctx.config.get("perception.encoder") == "tier-0"
    assert keep.read_bytes() == b"keepme"


def test_engine_unknown_subcommand(tmp_path):
    import commands
    ctx = _ctx_double()
    commands.register_commands(ctx)
    out = ctx.commands["mirror"]("engine bogus")
    assert "/mirror" in out


def test_download_hash_gate_refuses_mismatch(tmp_path):
    # opener returns wrong bytes for every URL -> post-download hash gate refuses.
    class Resp:
        def __init__(self, data):
            self._d = data
        def read(self, n=-1):
            d, self._d = self._d, b""
            return d
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    def opener(url):
        return Resp(b"not-the-real-model-bytes")
    with pytest.raises(encoder.EmbeddingError):
        encoder.download_tier1_model(tmp_path / "m", confirm=True, opener=opener)


def test_download_requires_confirm(tmp_path):
    with pytest.raises(encoder.EmbeddingError):
        encoder.download_tier1_model(tmp_path / "m")


def test_worker_client_never_downloads_at_encode(tmp_path):
    # encode() must not touch the network: model missing -> immediate failure, no files created.
    c, mdir, mp = _fake_client(tmp_path)
    client2 = OnnxWorkerClient(model_dir=tmp_path / "nomodel", manifest_path=mp,
                               worker_path=tmp_path / "fake_worker.py",
                               interpreter=sys.executable, timeout_s=5)
    before = {str(p) for p in tmp_path.rglob("*")}
    backend = encoder.OnnxMiniLMBackend(client=client2)
    backend.encode(["hello"])
    after = {str(p) for p in tmp_path.rglob("*")}
    assert after == before
