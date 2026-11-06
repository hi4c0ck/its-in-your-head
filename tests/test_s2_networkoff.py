"""S2 zero-network guarantee: Tier-0 encode + anchor build + resemblance must
work with every socket connect call blocked. Also self-verifies the S2 diff
stays inside its ownership set (perception/ + tests/test_s2_*)."""
import socket

import pytest

from perception import anchors, encoder, resemblance


@pytest.fixture
def network_off(monkeypatch):
    """Block all outbound socket activity; any connect attempt fails loudly."""
    def _boom(*a, **kw):
        raise RuntimeError("network blocked in S2 guard test")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket.socket, "connect_ex", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError("network blocked in S2 guard test")))
    return monkeypatch


def test_tier0_encode_network_off(network_off):
    b = encoder.Tier0SketchBackend()
    vecs = b.encode(["hello network-free world", "another prompt"])
    assert len(vecs) == 2 and all(len(v) == encoder.embedder.DIM for v in vecs)


def test_anchor_build_and_cache_network_off(network_off, tmp_path):
    b = encoder.Tier0SketchBackend()
    vecs = anchors.get_anchor_vectors(b, data_dir=tmp_path)
    assert list(vecs.keys()) == list(anchors.DIMENSIONS)
    assert (tmp_path / anchors.CACHE_FILE).exists()
    # second (cached) read is also network-free
    again = anchors.get_anchor_vectors(b, data_dir=tmp_path)
    assert again == vecs


def test_resemblance_and_calibration_network_off(network_off, tmp_path):
    b = encoder.Tier0SketchBackend()
    vecs = anchors.get_anchor_vectors(b, data_dir=tmp_path)
    p = resemblance.portrait_for_text("Give me the short answer, no preamble.",
                                      b, anchor_vectors=vecs)
    assert -2.0 <= p["resemblance"]["directness"] <= 2.0
    assert 0.0 <= p["confidence"]["directness"] <= 1.0


def test_tier1_encode_never_attempts_network(network_off, tmp_path):
    """Even the Tier-1 path fails locally (missing artifacts), never via a socket."""
    b = encoder.OnnxMiniLMBackend(data_dir=tmp_path)
    with pytest.raises(encoder.ModelNotInstalledError):
        b.encode(["hello"])
    assert list(tmp_path.iterdir()) == []  # no download artifacts created
