"""S2 anchor-bank tests: shape, versioning, cache invalidation (disk-backed)."""
import json

import pytest

from perception import anchors, encoder


def test_bank_covers_all_six_dimensions():
    bank = anchors.default_bank()
    assert bank.dimensions == anchors.DIMENSIONS
    assert len(bank.dimensions) == 6
    assert set(bank.dimensions) == {"directness", "context_richness", "exploration",
                                     "analytical_depth", "iteration", "scope"}


def test_each_dimension_has_5_to_10_pos_and_neg_anchors():
    bank = anchors.default_bank()
    anchors.validate_bank(bank)  # must not raise
    for dim in bank.dimensions:
        pos, neg = bank.positive(dim), bank.negative(dim)
        assert 5 <= len(pos) <= 10
        assert 5 <= len(neg) <= 10
        assert pos != neg


def test_bank_is_deterministic_across_calls():
    assert anchors.default_bank().positive("directness") == anchors.default_bank().positive("directness")
    assert anchors.ANCHOR_BANK_VERSION == "1.0.0"


def test_build_anchor_vectors_shape_and_order(tmp_path):
    b = encoder.Tier0SketchBackend()
    vecs = anchors.build_anchor_vectors(b)
    assert list(vecs.keys()) == list(anchors.DIMENSIONS)
    for dim in anchors.DIMENSIONS:
        assert len(vecs[dim]["positive"]) == len(anchors.default_bank().positive(dim))
        assert len(vecs[dim]["negative"]) == len(anchors.default_bank().negative(dim))
        assert all(len(v) == encoder.embedder.DIM for v in vecs[dim]["positive"] + vecs[dim]["negative"])


def test_cache_roundtrip_and_hit(tmp_path):
    b = encoder.Tier0SketchBackend()
    v1 = anchors.get_anchor_vectors(b, data_dir=tmp_path)
    assert (tmp_path / anchors.CACHE_FILE).exists()
    v2 = anchors.get_anchor_vectors(b, data_dir=tmp_path)
    assert v1 == v2  # hit path returns same vectors (JSON roundtrip)


def test_cache_invalidated_on_model_version_change(tmp_path):
    b = encoder.Tier0SketchBackend()
    anchors.get_anchor_vectors(b, data_dir=tmp_path)

    class Bump(encoder.Tier0SketchBackend):
        @property
        def model_version(self):
            return "tier0-sketch-v2"

    cache_path = tmp_path / anchors.CACHE_FILE
    before = cache_path.read_bytes()
    v = anchors.get_anchor_vectors(Bump(), data_dir=tmp_path)
    after = json.loads(cache_path.read_text(encoding="utf-8"))
    assert after["model_version"] == "tier0-sketch-v2"
    assert after["anchor_bank_version"] == anchors.ANCHOR_BANK_VERSION


def test_cache_invalidated_on_bank_version_change(tmp_path):
    b = encoder.Tier0SketchBackend()
    anchors.get_anchor_vectors(b, data_dir=tmp_path)
    bank = anchors.AnchorBank(version="2.0.0")
    loaded = anchors.load_cache_if_valid(tmp_path / anchors.CACHE_FILE, bank, b)
    assert loaded is None  # stale version must not be used
    anchors.get_anchor_vectors(b, bank=bank, data_dir=tmp_path)
    data = json.loads((tmp_path / anchors.CACHE_FILE).read_text(encoding="utf-8"))
    assert data["anchor_bank_version"] == "2.0.0"


def test_corrupt_cache_is_rebuilt_not_crashed(tmp_path):
    b = encoder.Tier0SketchBackend()
    (tmp_path / anchors.CACHE_FILE).write_text("{ not json", encoding="utf-8")
    vecs = anchors.get_anchor_vectors(b, data_dir=tmp_path)
    assert list(vecs.keys()) == list(anchors.DIMENSIONS)


def test_missing_cache_file_rebuilds(tmp_path):
    assert anchors.load_cache_if_valid(tmp_path / "nope.json",
                                        anchors.default_bank(),
                                        encoder.Tier0SketchBackend()) is None
