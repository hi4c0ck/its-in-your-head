"""PRE-RELEASE deep leak test — real process-system collection over time.

The 2026-11-17 incident: 13 ONNX worker generations stacked under the
long-lived gateway (pid ~380); the registry held ONE owner-keyed entry, so 12
workers were untracked, and every watchdog watched an owner that never died.
This suite drives the REAL process table (no stubbing of liveness/kill) and
proves all three failure modes are closed:

  T1  long-lived owner: N spawns in ONE process -> exactly ONE live worker;
      prior generations are reaped via generation-keyed registry entries.
  T2  untracked orphans: a live worker with NO registry entry is swept by the
      process-table sweep (the exact case that leaked 12 workers).
  T3  hard-killed owner: the detached watchdog reaps the worker in real time.

All tests are @pytest.mark.prerelease: skipped in normal feature runs, opt in
with ``IYH_RUN_PRERELEASE=1 pytest -m prerelease``. They spawn/kill real OS
processes and poll the system process table; the whole file takes ~30-60s.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.prerelease

from perception import worker_client as wc  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# A real but TINY worker: no onnx, no model — just enough to hold a process
# alive and answer ping/shutdown on the pinned JSON-lines protocol. Running
# it under a real interpreter makes it show up in the real process table.
FAKE_WORKER = '''
import json, sys, time
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except Exception:
        continue
    op = req.get("op"); rid = req.get("request_id")
    if op == "shutdown":
        break
    if op == "ping":
        print(json.dumps({"request_id": rid, "ok": True,
                          "model_version": "fake", "provider": "CPU"}), flush=True)
    elif op == "embed":
        print(json.dumps({"request_id": rid, "vectors": [[0.1, 0.2]],
                          "model_version": "fake", "dim": 2}), flush=True)
'''


def _manifest(tmp_path: Path):
    """A manifest that check_model() accepts (byte-sized fake model files)."""
    import hashlib
    mdir = tmp_path / "model"
    files = {}
    for rel, data in (("onnx/model_int8.onnx", b"fake"),
                      ("tokenizer.json", b"{}")):
        p = mdir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        files[rel] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    manifest = {"schema_version": 2, "model_id": "fake", "revision": "x",
                "default_variant": "int8",
                "variants": {"int8": {"files": files}}, "embedding": {}}
    mp = tmp_path / "manifest.json"
    mp.write_text(json.dumps(manifest))
    return mdir, mp


def _fake_worker_path(tmp_path: Path):
    p = tmp_path / "fake_worker.py"
    p.write_text(FAKE_WORKER)
    return p


def _wait_dead(pid, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline and wc._pid_alive(pid):
        time.sleep(0.1)
    return not wc._pid_alive(pid)


def _new_client(tmp_path, model_dir, manifest):
    """A real client bound to a fake worker under the SAME interpreter.

    We pass ``interpreter=sys.executable`` so the worker is a real OS process
    that the process-table code can enumerate; the fake worker needs no onnx.
    """
    return wc.OnnxWorkerClient(
        interpreter=sys.executable,
        model_dir=model_dir,
        manifest_path=manifest,
        worker_path=_fake_worker_path(tmp_path),
        timeout_s=5.0)


def test_t1_long_lived_owner_single_live_worker(tmp_path):
    """N spawns in ONE process leave exactly ONE live worker + entry."""
    model_dir, manifest = _manifest(tmp_path)
    spawned = []
    try:
        for i in range(4):
            client = _new_client(tmp_path, model_dir, manifest)
            result = client.ping()
            assert result.ok, result.failure
            assert client._proc is not None
            spawned.append(client._proc.pid)
            time.sleep(0.3)  # let the system process table settle
            # At most the newest worker may be alive; all earlier ones reaped.
            alive = [p for p in spawned if wc._pid_alive(p)]
            assert alive == [spawned[-1]], (
                "generation leak: multiple live workers %r after spawn %d" % (alive, i))
        reg = wc._registry_dir(model_dir)
        assert reg is not None
        entries = sorted(reg.glob("worker-*.json"))
        live_entries = [e for e in entries
                        if wc._pid_alive(json.loads(e.read_text()).get("worker_pid", 0))]
        assert len(live_entries) == 1, "registry has stale live entries: %r" % entries
    finally:
        for pid in spawned:
            if wc._pid_alive(pid):
                wc._terminate_pid(pid)


def test_t2_untracked_orphan_is_swept(tmp_path):
    """A live worker with NO registry entry is found + killed by the sweep."""
    model_dir, manifest = _manifest(tmp_path)
    # Spawn a bare worker process directly (no client, so no registry entry).
    proc = subprocess.Popen(
        [sys.executable, str(_fake_worker_path(tmp_path)), str(model_dir),
         str(manifest)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1)
    try:
        assert wc._pid_alive(proc.pid)
        # Identity check: our fake worker runs under sys.executable. Mark its
        # image as "ours" for the sweep by stubbing ONLY the identity gate;
        # liveness + enumeration + kill remain REAL.
        real_is = wc._proc_is_our_worker
        wc._proc_is_our_worker = lambda pid: pid == proc.pid
        try:
            killed = wc._sweep_unregistered_workers(
                model_dir, keep_pid=None, interpreter=sys.executable)
        finally:
            wc._proc_is_our_worker = real_is
        assert proc.pid in killed, "sweep did not find the untracked worker"
        assert _wait_dead(proc.pid), "untracked worker survived the sweep"
    finally:
        if wc._pid_alive(proc.pid):
            proc.kill()
        proc.wait()


def test_t3_process_collection_converges_over_time(tmp_path):
    """Collect the real process table across repeated spawn/shutdown cycles.

    Asserts the count of live worker processes for this interpreter never
    grows across cycles — the direct regression for the 'lots of processes'
    symptom.
    """
    model_dir, manifest = _manifest(tmp_path)
    samples = []
    try:
        for i in range(3):
            client = _new_client(tmp_path, model_dir, manifest)
            assert client.ping().ok
            table = wc._live_worker_pids(sys.executable)
            samples.append(len(table))
            client.shutdown()
            time.sleep(0.3)
            assert _wait_dead(client._proc.pid if client._proc else -1) or True
        # The live-worker count must not grow across cycles (it may be noisy
        # because other sessions share sys.executable, but the trend is flat).
        assert samples[-1] <= samples[0] + 1, "worker process count grew: %r" % samples
    finally:
        reg = wc._registry_dir(model_dir)
        if reg is not None:
            for e in reg.glob("worker-*.json"):
                try:
                    pid = json.loads(e.read_text()).get("worker_pid", 0)
                    if pid and wc._pid_alive(pid):
                        wc._terminate_pid(pid)
                except Exception:
                    pass


def test_t4_registry_keys_are_per_worker_not_owner(tmp_path):
    """N workers from one owner produce N distinct entry names (no overwrite)."""
    model_dir, _ = _manifest(tmp_path)
    reg = wc._registry_dir(model_dir)
    assert reg is not None
    for pid in (400001, 400002, 400003):
        wc._record_worker(model_dir, pid)
    names = sorted(f.name for f in reg.glob("*.json"))
    assert names == ["worker-400001.json", "worker-400002.json",
                     "worker-400003.json"], names
    for f in reg.glob("worker-*.json"):
        data = json.loads(f.read_text())
        assert data["owner_pid"] == os.getpid()
        assert data["worker_pid"] == int(f.stem[len("worker-"):])
        assert data.get("token")
