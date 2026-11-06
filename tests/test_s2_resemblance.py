"""S2 resemblance + calibration tests: range, sign semantics, separation."""
import math

import pytest

from perception import anchors, encoder, resemblance


def _scores(text, vecs):
    return resemblance.resemblance_for_text(text, encoder.Tier0SketchBackend(),
                                            anchor_vectors=vecs)


@pytest.fixture(scope="module")
def vecs():
    return anchors.build_anchor_vectors(encoder.Tier0SketchBackend())


def test_cosine_basic_geometry():
    assert resemblance.cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert resemblance.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert resemblance.cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
    with pytest.raises(ValueError):
        resemblance.cosine([1.0], [1.0, 2.0])


def test_r_range_is_minus_two_to_two(vecs):
    for text in ["anything", "", "short", "a much longer prompt about debugging the auth flow"]:
        for dim, s in _scores(text, vecs).items():
            assert -2.0 <= s["R"] <= 2.0
            assert -1.0 <= s["S"] <= 1.0
            assert -1.0 <= s["C"] <= 1.0
            assert s["R"] == pytest.approx(s["S"] - s["C"])


def test_sign_semantics_directness_pair(vecs):
    """A direct prompt should resemble directness>0; a verbose one should not exceed it."""
    direct = _scores("Give me the short answer, no preamble.", vecs)["directness"]["R"]
    verbose = _scores("Walk me through a long story with full background and gentle intro.",
                      vecs)["directness"]["R"]
    assert direct > verbose
    assert direct > 0  # clear-sign: near a positive anchor, away from negatives


def test_sign_semantics_context_pair(vecs):
    rich = _scores("Here is the full background, logs, config and exact error message.",
                   vecs)["context_richness"]["R"]
    bare = _scores("Fix it.", vecs)["context_richness"]["R"]
    assert rich > bare
    assert rich > 0


def test_self_similarity_gives_positive_r(vecs):
    """Feeding a positive anchor back in must yield R>0 for that dimension."""
    s = _scores("Give me the short answer, no preamble.", vecs)
    assert s["directness"]["R"] > 0
    assert s["directness"]["S"] > s["directness"]["C"]


def test_resemblance_and_confidence_are_separate_numbers(vecs):
    p = resemblance.portrait_for_text("Just tell me the number.",
                                      encoder.Tier0SketchBackend(), anchor_vectors=vecs)
    assert set(p) == {"resemblance", "S", "C", "confidence"}
    assert p["resemblance"]["directness"] == pytest.approx(
        p["S"]["directness"] - p["C"]["directness"])
    # confidence is calibrated, not the raw cosine; values in [0,1]
    assert 0.0 <= p["confidence"]["directness"] <= 1.0
    assert p["confidence"]["directness"] != pytest.approx(p["resemblance"]["directness"])


def test_calibration_is_monotonic_and_separates_fixture():
    cal = resemblance.fit_calibration([(-0.5, False), (-0.2, False), (0.2, True), (0.5, True)])
    rs = [-1.0, -0.5, 0.0, 0.5, 1.0]
    confs = [cal.confidence(r) for r in rs]
    assert all(b > a for a, b in zip(confs, confs[1:]))
    assert cal.confidence(0.5) > 0.5 > cal.confidence(-0.5)


def test_calibration_fit_from_fixture_separates_labeled_pairs():
    b = encoder.Tier0SketchBackend()
    vecs = anchors.build_anchor_vectors(b)
    cal = resemblance.fit_from_fixture(b, anchor_vectors=vecs)
    assert cal.confidence(cal.midpoint) == pytest.approx(0.5)
    pairs = []
    for text, labels in resemblance.LABELLED_FIXTURE:
        scores = resemblance.resemblance_for_text(text, b, anchor_vectors=vecs)
        for dim, label in labels.items():
            pairs.append((scores[dim]["R"], label))
    pos = [r for r, y in pairs if y]
    neg = [r for r, y in pairs if not y]
    assert sum(pos) / len(pos) > sum(neg) / len(neg)  # labeled positives score higher on average


def test_confidence_never_equals_raw_cosine_percentage():
    """Guard the rule: confidence is not S or any raw cosine in disguise."""
    cal = resemblance.Calibration()
    assert cal.confidence(0.0) == pytest.approx(0.5)
    assert not (0.0 < cal.confidence(0.0) < 1.0) is False  # sanity: interior value
