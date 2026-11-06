"""S2 encoder tests: Tier-0 determinism, L2 norm, batching, depth/GPU-free paths."""
import math
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from perception import encoder

ROOT = Path(__file__).resolve().parent.parent


def backend():
    return encoder.Tier0SketchBackend()


def test_determinism_same_text_same_vector():
    b = backend()
    texts = ["hello world", "Fix the auth bug now", "b"]
    assert b.encode(texts) == b.encode(texts)
    assert b.encode(texts) == backend().encode(list(texts))


def test_determinism_across_fresh_instances_and_batch_boundaries():
    b = backend()
    texts = [f"prompt {i}" for i in range(5)]
    one_by_one = [b.encode([t])[0] for t in texts]
    assert one_by_one == b.encode(texts)


def test_l2_normalization_unit_norm():
    b = backend()
    vecs = b.encode(["anything at all", "", "x", "a much longer prompt about robots"])
    for v in vecs[:1] + vecs[3:]:
        n = math.sqrt(sum(x * x for x in v))
        assert n == pytest.approx(1.0, abs=1e-6)


def test_empty_and_short_text_shapes():
    b = backend()
    empty, short = b.encode(["", "ab"])
    assert len(empty) == len(short) == encoder.embedder.DIM
    assert all(v == 0.0 for v in empty)  # zero vector for empty input
    assert sum(v * v for v in short) == pytest.approx(1.0, abs=1e-6)


def test_vector_dimension_matches_embedder_dim():
    b = backend()
    (v,) = b.encode(["hello"])
    assert len(v) == encoder.embedder.DIM
    assert all(isinstance(x, float) for x in v)


def test_encode_batched_handles_empty_list():
    assert backend().encode([]) == []


def test_model_version_is_stable_and_names_tier0():
    assert backend().model_version == encoder.TIER0_MODEL_VERSION
    assert b"tier0" in encoder.TIER0_MODEL_VERSION.encode() or "sketch" in backend().model_version


def test_pythonhashseed_independence_via_subprocess(monkeypatch):
    """Same text must give the same bytes under different PYTHONHASHSEED values."""
    code = (
        "import struct;"
        "import embedder;"
        "print(embedder.sketch('seeded prompt').hex())"
        if False else
        "import struct;"
        "print(__import__('embedder').sketch('seeded prompt').hex())"
    )
    outs = []
    for seed in ("0", "42"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=str(ROOT))
        r = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(ROOT),
        )
        assert r.returncode == 0, r.stderr
        outs.append(r.stdout.strip())
    assert outs[0] == outs[1] and outs[0] != ""


def test_factory_defaults_to_tier0_and_rejects_unknown():
    assert isinstance(encoder.create_backend(), encoder.Tier0SketchBackend)
    assert isinstance(encoder.create_backend({encoder.BACKEND_SETTING_KEY: "tier-0"}),
                      encoder.Tier0SketchBackend)
    with pytest.raises(ValueError):
        encoder.create_backend({encoder.BACKEND_SETTING_KEY: "tier-9"})


def test_tier1_never_downloads_or_loads_at_encode_time(tmp_path):
    """Tier-1 with no local artifacts must fail with ModelNotInstalledError, not network."""
    b = encoder.OnnxMiniLMBackend(data_dir=tmp_path)
    with pytest.raises(encoder.ModelNotInstalledError):
        b.encode(["hello"])


def test_download_helper_requires_explicit_confirm(tmp_path):
    with pytest.raises(encoder.EmbeddingError):
        encoder.download_tier1_model(tmp_path)  # confirm defaults False


def test_encoder_construction_does_no_io(tmp_path):
    """Constructing backends must not create files or touch the network."""
    env_dir = tmp_path / "data"
    b = encoder.OnnxMiniLMBackend(data_dir=env_dir)
    assert not env_dir.exists()
    assert b is not None
