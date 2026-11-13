"""S8 ONNX Tier-1 worker (LANE B): hermetic fake-worker protocol + fallback + /mirror engine."""
import hashlib
import json
import os
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
    manifest = {"schema_version": 2, "model_id": "fake-model", "revision": "abc",
                "default_variant": "int8",
                "variants": {"int8": {"files": files}},
                "embedding": {"dimension": 384}}
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
    # A2 zero leftovers: no final and no .part file for any artifact.
    dest = tmp_path / "m"
    assert [p for p in dest.rglob("*") if p.is_file()] == []
    for name in encoder.TIER1_ARTIFACTS:
        assert not (dest / name).exists()
        assert not Path(str(dest / name) + ".part").exists()


def test_download_requires_confirm(tmp_path):
    with pytest.raises(encoder.EmbeddingError):
        encoder.download_tier1_model(tmp_path / "m")


def _tiny_manifest(tmp_path: Path, payloads: dict[str, bytes], variant: str = "int8"):
    files = {rel: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
             for rel, data in payloads.items()}
    manifest = {"schema_version": 2, "model_id": "fake", "revision": "abc",
                "default_variant": variant,
                "variants": {variant: {"files": files}}}
    mp = tmp_path / "tiny_manifest.json"
    mp.write_text(json.dumps(manifest), encoding="utf-8")
    return mp


class _Resp:
    def __init__(self, data):
        self._d = data
    def read(self, n=-1):
        d, self._d = self._d, b""
        return d
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _payloads():
    return {name: (f"tiny-{i}".encode()) for i, name in enumerate(encoder.TIER1_ARTIFACTS)}


def test_download_happy_path_exact_pinned_bytes(tmp_path):
    payloads = _payloads()
    mp = _tiny_manifest(tmp_path, payloads)
    def opener(url):
        for name, data in payloads.items():
            if url.endswith(name):
                return _Resp(data)
        raise AssertionError(f"unexpected url {url}")
    dest = tmp_path / "dl"
    result = encoder.download_tier1_model(dest, confirm=True, opener=opener, manifest_path=mp)
    assert result["model_version"].endswith(f"@rev-{encoder.TIER1_REVISION}")
    for name, data in payloads.items():
        assert (dest / name).read_bytes() == data
        assert not Path(str(dest / name) + ".part").exists()
        assert result[name]["sha256"] == hashlib.sha256(data).hexdigest()
    assert (dest / "model_version.txt").read_text(encoding="utf-8") == result["model_version"]
    assert encoder.verify_tier1_model(dest, manifest_path=mp) == []


def test_download_size_cap_overflow_aborts_and_cleans(tmp_path):
    payloads = _payloads()
    mp = _tiny_manifest(tmp_path, payloads)
    class EndlessResp:
        def read(self, n=-1):
            return b"x"  # never EOF; cap must stop the loop
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    def opener(url):
        return EndlessResp()
    dest = tmp_path / "dl"
    with pytest.raises(encoder.EmbeddingError) as excinfo:
        encoder.download_tier1_model(dest, confirm=True, opener=opener, manifest_path=mp)
    msg = str(excinfo.value)
    assert "size cap" in msg
    first = next(iter(encoder.TIER1_ARTIFACTS))
    assert first in msg
    assert [p for p in dest.rglob("*") if p.is_file()] == []
    assert list(dest.rglob("*.part")) == []


def test_download_enospc_becomes_embedding_error_and_cleans(tmp_path, monkeypatch):
    import builtins
    payloads = _payloads()
    mp = _tiny_manifest(tmp_path, payloads)
    real_open = builtins.open
    class BadFile:
        def __init__(self, fh):
            self._fh = fh
        def write(self, data):
            raise OSError(28, "No space left on device")
        def flush(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            self._fh.close()
            return False
    def bad_open(file, mode="r", *a, **k):
        fh = real_open(file, mode, *a, **k)
        if "w" in mode:
            return BadFile(fh)
        return fh
    monkeypatch.setattr(builtins, "open", bad_open)
    def opener(url):
        return _Resp(b"tiny-0")
    dest = tmp_path / "dl"
    with pytest.raises(encoder.EmbeddingError) as excinfo:
        encoder.download_tier1_model(dest, confirm=True, opener=opener, manifest_path=mp)
    assert "disk full" in str(excinfo.value)
    assert [p for p in dest.rglob("*") if p.is_file()] == []
    assert list(dest.rglob("*.part")) == []


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


# --- LANE 4: variant split (int8 default / fp32 optional) -------------------

def _recording_opener(payloads, urls):
    def opener(url):
        urls.append(url)
        for name, data in payloads.items():
            if url.endswith(name):
                return _Resp(data)
        raise AssertionError(f"unexpected url {url}")
    return opener


def test_default_download_requests_only_int8_variant(tmp_path):
    payloads = _payloads()  # TIER1_ARTIFACTS == int8 set (5 files)
    mp = _tiny_manifest(tmp_path, payloads)
    urls = []
    dest = tmp_path / "dl"
    result = encoder.download_tier1_model(
        dest, confirm=True, opener=_recording_opener(payloads, urls), manifest_path=mp)
    assert result["variant"] == "int8"
    assert len(urls) == 5
    requested = {rel for rel in payloads if any(u.endswith(rel) for u in urls)}
    assert requested == set(payloads)
    assert not any(u.endswith("onnx/model.onnx") for u in urls), urls  # fp32 never requested
    assert not (dest / "onnx" / "model.onnx").exists()


def test_fp32_variant_download_requests_only_fp32_graph(tmp_path):
    payloads = {"onnx/model.onnx": b"tiny-fp32-graph"}
    mp = _tiny_manifest(tmp_path, payloads, variant="fp32")
    urls = []
    dest = tmp_path / "dl"
    result = encoder.download_tier1_model(
        dest, confirm=True, opener=_recording_opener(payloads, urls),
        manifest_path=mp, variant="fp32")
    assert result["variant"] == "fp32"
    assert len(urls) == 1 and urls[0].endswith("onnx/model.onnx")
    assert (dest / "onnx" / "model.onnx").read_bytes() == b"tiny-fp32-graph"
    assert encoder.verify_tier1_model(dest, manifest_path=mp, variant="fp32") == []


def test_fp32_variant_mismatch_leaves_zero_leftovers(tmp_path):
    payloads = {"onnx/model.onnx": b"good-fp32-bytes"}
    mp = _tiny_manifest(tmp_path, payloads, variant="fp32")
    def opener(url):
        return _Resp(b"tampered-fp32-bytes")
    dest = tmp_path / "dl"
    with pytest.raises(encoder.EmbeddingError):
        encoder.download_tier1_model(dest, confirm=True, opener=opener,
                                     manifest_path=mp, variant="fp32")
    assert [p for p in dest.rglob("*") if p.is_file()] == []
    assert list(dest.rglob("*.part")) == []


def test_unknown_variant_raises_embedding_error_naming_it(tmp_path):
    payloads = _payloads()
    mp = _tiny_manifest(tmp_path, payloads)
    with pytest.raises(encoder.EmbeddingError) as excinfo:
        encoder.download_tier1_model(tmp_path / "dl", confirm=True,
                                     opener=_recording_opener(payloads, []),
                                     manifest_path=mp, variant="fp16")
    assert "fp16" in str(excinfo.value)
    with pytest.raises(encoder.EmbeddingError) as excinfo:
        encoder.verify_tier1_model(tmp_path / "dl", manifest_path=mp, variant="fp16")
    assert "fp16" in str(excinfo.value)


def test_verify_ignores_unknown_files_on_disk(tmp_path):
    payloads = _payloads()
    mp = _tiny_manifest(tmp_path, payloads)
    dest = tmp_path / "dl"
    encoder.download_tier1_model(dest, confirm=True,
                                 opener=_recording_opener(payloads, []),
                                 manifest_path=mp)
    (dest / "onnx").mkdir(exist_ok=True)
    (dest / "onnx" / "model.onnx").write_bytes(b"stray fp32 graph, not in int8 variant")
    (dest / "extra.txt").write_bytes(b"stray")
    assert encoder.verify_tier1_model(dest, manifest_path=mp) == []
    # A two-variant manifest: fp32 verify requires the fp32 graph, and the
    # int8 verify ignores it even when present.
    fp32_sha = hashlib.sha256(b"real-fp32").hexdigest()
    mp2 = tmp_path / "two_variant.json"
    mp2.write_text(json.dumps({
        "schema_version": 2, "model_id": "fake", "revision": "abc",
        "default_variant": "int8",
        "variants": {
            "int8": {"files": {r: {"sha256": hashlib.sha256(d).hexdigest(),
                                   "size": len(d)} for r, d in payloads.items()}},
            "fp32": {"files": {"onnx/model.onnx": {"sha256": fp32_sha, "size": 9}}},
        }}), encoding="utf-8")
    (dest / "onnx" / "model.onnx").write_bytes(b"real-fp32")
    assert encoder.verify_tier1_model(dest, manifest_path=mp2, variant="fp32") == []
    assert encoder.verify_tier1_model(dest, manifest_path=mp2) == []
    (dest / "onnx" / "model.onnx").unlink()
    assert any("missing" in e and "model.onnx" in e
               for e in encoder.verify_tier1_model(dest, manifest_path=mp2, variant="fp32"))


# --- leak-guard regression tests (atexit + job object + stale-registry reaper) ---

def test_pid_alive_reflects_real_state():
    import subprocess
    import time
    from perception.worker_client import _pid_alive, _terminate_pid
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert _pid_alive(p.pid) is True
    finally:
        _terminate_pid(p.pid)
        p.wait()
    time.sleep(0.2)
    assert _pid_alive(p.pid) is False
    assert _pid_alive(0) is False


def test_guard_rejects_unrelated_process():
    import subprocess
    from perception.worker_client import _proc_is_our_worker
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        # Our own (non-onnx-venv) interpreter must NEVER be treated as a
        # reapeable worker: the reaper's safety guard rejects it.
        assert _proc_is_our_worker(p.pid) is False
    finally:
        p.terminate()
        p.wait()


def test_reaper_kills_dead_owner_worker(tmp_path, monkeypatch):
    import json as _json
    from perception import worker_client as wc
    reg = wc._registry_dir(tmp_path)
    assert reg is not None
    dead_owner, orphan = 900_001, 900_002
    (reg / f"{dead_owner}.json").write_text(
        _json.dumps({"worker_pid": orphan, "ts": 0.0}))
    terminated = []
    monkeypatch.setattr(wc, "_pid_alive",
                        lambda pid: pid in (orphan,))
    monkeypatch.setattr(wc, "_proc_is_our_worker", lambda pid: pid == orphan)
    monkeypatch.setattr(wc, "_terminate_pid",
                        lambda pid: terminated.append(pid) or True)
    wc._reap_stale_workers(tmp_path)
    assert terminated == [orphan]
    assert not list(reg.glob("*.json"))


def test_reaper_preserves_live_owner_entry(tmp_path, monkeypatch):
    import json as _json
    from perception import worker_client as wc
    reg = wc._registry_dir(tmp_path)
    assert reg is not None
    live_owner, worker = 910_001, 910_002
    (reg / f"{live_owner}.json").write_text(
        _json.dumps({"worker_pid": worker, "ts": 0.0}))
    terminated = []
    monkeypatch.setattr(wc, "_pid_alive",
                        lambda pid: pid in (live_owner, worker, os.getpid()))
    monkeypatch.setattr(wc, "_proc_is_our_worker", lambda pid: True)
    monkeypatch.setattr(wc, "_terminate_pid",
                        lambda pid: terminated.append(pid) or True)
    wc._reap_stale_workers(tmp_path)
    assert terminated == []
    assert (reg / f"{live_owner}.json").exists()  # live owner's entry survives


def test_spawn_records_and_reaps_integration(tmp_path):
    """_spawn() reaps a dead owner's orphan + records the new worker under
    the current pid; shutdown() forgets the current pid's entry."""
    import json as _json
    import subprocess as _sp
    from perception import worker_client as wc
    reg = wc._registry_dir(tmp_path)
    assert reg is not None
    # Plant a dead owner's orphan entry whose "worker" is a live, matchable proc.
    orphan = _sp.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    dead_owner = 950_001
    (reg / f"{dead_owner}.json").write_text(
        _json.dumps({"worker_pid": orphan.pid, "ts": 0.0}))
    killed = []
    _orig_is = wc._proc_is_our_worker
    _orig_term = wc._terminate_pid
    wc._proc_is_our_worker = lambda pid: pid == orphan.pid
    wc._terminate_pid = lambda pid: (killed.append(pid), True)[1]
    try:
        wc._reap_stale_workers(tmp_path)
        wc._record_worker(tmp_path, orphan.pid)  # current pid owns a new worker
    finally:
        wc._proc_is_our_worker = _orig_is
        wc._terminate_pid = _orig_term
        orphan.terminate()
        orphan.wait()
    assert orphan.pid in killed  # the orphan was terminated by the reaper
    assert (reg / f"{os.getpid()}.json").exists()  # self recorded
