"""S3 quality gates: transitions false-positive corpus, portrait stability,
renderer determinism. >12 tests; quality B/C + contract rules."""
import math

import pytest

from perception import anchors, encoder, portrait, renderer, transitions
from perception.resemblance import Calibration


@pytest.fixture(scope="module")
def backend():
    return encoder.Tier0SketchBackend()


@pytest.fixture(scope="module")
def vecs(backend):
    return anchors.build_anchor_vectors(backend)


# ---------------------------------------------------------------- quality B
def test_b_now_implement_it_is_continuation(backend):
    t = transitions.classify_texts("Build the auth module for the web app",
                                   "Now implement it.", backend=backend)
    assert t.label == transitions.CONTINUATION


def test_b_another_project_is_new_topic(backend):
    t = transitions.classify_texts("Build the auth module for the web app",
                                   "What about another project?", backend=backend)
    assert t.label == transitions.NEW_TOPIC


def test_b_thanks_is_positive_feedback(backend):
    t = transitions.classify_texts("Build the auth module for the web app",
                                   "Thanks.", backend=backend)
    assert t.label == transitions.POSITIVE_FEEDBACK


def test_b_completely_wrong_is_correction(backend):
    t = transitions.classify_texts("Build the auth module for the web app",
                                   "That is completely wrong.", backend=backend)
    assert t.label == transitions.CORRECTION


def test_implement_followup_never_positive_even_with_praise(backend):
    # "Great, now implement it." is an imperative follow-up, not praise.
    t = transitions.classify_texts("Plan the dashboard", "Great, now implement it.",
                                   backend=backend)
    assert t.label == transitions.CONTINUATION
    assert t.label != transitions.POSITIVE_FEEDBACK


def test_silence_is_not_approval():
    # No message at all -> ambiguous, never positive feedback.
    for empty in (None, "", "   "):
        t = transitions.classify_texts("Build the auth module", empty)
        assert t.label == transitions.AMBIGUOUS
        assert t.label != transitions.POSITIVE_FEEDBACK
    # Empty / zero feature vector at the pure-function level too.
    t = transitions.classify_features([1.0, 0.0], [0.0, 0.0])
    assert t.label == transitions.AMBIGUOUS


def test_ambiguous_mid_similarity(backend):
    # No phrase flags, mid-range cosine -> ambiguous (feature level too).
    t = transitions.classify_texts("Add tests for the encoder", "Add tests",
                                   backend=backend)
    assert t.label == transitions.AMBIGUOUS
    v_prev = [1.0, 0.2, 0.0]
    v_curr = [0.4, 0.9, 0.1]
    t2 = transitions.classify_features(v_prev, v_curr)
    assert t2.label == transitions.AMBIGUOUS


def test_long_gap_biases_to_new_topic():
    prev = [1.0, 0.0, 0.0]
    curr = [0.4, 0.9, 0.1]  # mid cosine, no phrases
    assert transitions.classify_features(prev, curr).label == transitions.AMBIGUOUS
    far = transitions.classify_features(prev, curr, time_gap_seconds=2 * 86400)
    assert far.label == transitions.NEW_TOPIC


def test_transition_is_pure_and_deterministic(backend):
    a = transitions.classify_texts("Fix the login bug", "Now implement it.", backend=backend)
    b = transitions.classify_texts("Fix the login bug", "Now implement it.", backend=backend)
    assert a == b
    assert a.to_dict() == b.to_dict()


def test_correction_outranks_continuation_phrases(backend):
    t = transitions.classify_texts("Build it", "That is completely wrong, now implement it.",
                                   backend=backend)
    assert t.label == transitions.CORRECTION


# ---------------------------------------------------------------- quality C
def _extreme_scores(s_value):
    return {d: {"S": s_value, "C": -s_value, "R": 2 * s_value} for d in anchors.DIMENSIONS}


def test_one_criticism_capped_delta(vecs):
    p = portrait.Portrait()
    before = dict(p.snapshot()["values"])
    p.update_observed(_extreme_scores(-1.0))  # one maximally negative observation
    after = p.snapshot()["values"]
    for d in anchors.DIMENSIONS:
        assert abs(after[d] - before[d]) <= portrait.MAX_STEP + 1e-9
        assert 0.0 <= after[d] <= 1.0


def test_three_independent_problems_exceed_cap():
    p = portrait.Portrait()
    start = p.snapshot()["values"]["directness"]
    for _ in range(3):
        p.update_observed(_extreme_scores(-1.0))
    after = p.snapshot()["values"]["directness"]
    assert abs(after - start) > portrait.MAX_STEP
    assert after < start  # moved toward the negative evidence


def test_ema_formula_exact():
    # Direct EMA helper: P_new = (1-alpha)*P_old + alpha*O
    assert portrait.ema_step(0.5, 1.0, 0.2) == pytest.approx(0.6)
    # alpha_for is bounded by evidence count: more evidence -> smaller alpha
    a0 = portrait.alpha_for(0, 1.0, 90)
    a_many = portrait.alpha_for(90, 1.0, 90)
    assert 0 < a_many < a0 <= portrait.BASE_ALPHA
    # consistency in [0,1] scales alpha monotonically in its band
    assert portrait.alpha_for(0, 1.0, 90) > portrait.alpha_for(0, 0.0, 90)


def test_confidence_in_unit_range_and_distinct_from_resemblance(vecs):
    p = portrait.Portrait()
    p.update_observed_from_text("Give me the short answer, no preamble.",
                                encoder.Tier0SketchBackend(), anchor_vectors=vecs)
    snap = p.snapshot()
    for d in anchors.DIMENSIONS:
        conf = snap["confidences"][d]
        res = snap["resemblance"][d]
        assert 0.0 <= conf <= 1.0
        assert conf != pytest.approx(res)          # never raw cosine/R as %
        assert conf != pytest.approx(snap["values"][d])


def test_observed_vs_interpreted_tracks_separate():
    p = portrait.Portrait()
    p.update_interpreted({"directness": 0.9, "scope": 0.1})
    snap = p.snapshot()
    assert snap["interpreted"]["values"]["directness"] > 0.5
    assert snap["observed"]["values"]["directness"] == pytest.approx(0.5)  # untouched
    p.update_observed(_extreme_scores(1.0))
    snap2 = p.snapshot()
    assert snap2["observed"]["values"]["directness"] > 0.5
    assert snap2["interpreted"]["values"]["scope"] < 0.5  # untouched by observed


def test_bounded_evidence_window_and_no_raw_text(vecs):
    secret = "Give me the short answer, no preamble."
    p = portrait.Portrait(max_evidence=3)
    for i in range(5):
        p.update_observed_from_text(secret, encoder.Tier0SketchBackend(),
                                    anchor_vectors=vecs)
    assert len(p.evidence) == 3  # FIFO-bounded at the window
    assert all(isinstance(e, dict) and all(isinstance(v, float) for v in e.values())
               for e in p.evidence)
    blob = repr(p.snapshot()) + repr(p.evidence)
    assert secret not in blob
    assert "preamble" not in blob


def test_retain_days_drives_window():
    assert portrait.evidence_window_for(10) == 10
    assert portrait.Portrait(retain_days=7).window == 7
    assert portrait.Portrait().window == portrait.DEFAULT_RETAIN_DAYS


# ---------------------------------------------------------------- renderer
def test_svg_determinism_byte_identical():
    vals = {d: 0.2 + 0.1 * i for i, d in enumerate(anchors.DIMENSIONS)}
    confs = {d: 0.9 - 0.05 * i for i, d in enumerate(anchors.DIMENSIONS)}
    assert renderer.render_svg(vals, confs) == renderer.render_svg(vals, confs)
    assert renderer.render_svg([0.5] * 6) == renderer.render_svg([0.5] * 6)


def test_svg_no_external_refs_no_scripts():
    s = renderer.render_svg({d: 0.5 for d in anchors.DIMENSIONS},
                            {d: 0.5 for d in anchors.DIMENSIONS})
    low = s.lower()
    assert "<script" not in low
    assert "href" not in low and "xlink" not in low
    assert "http" not in low.replace("http://www.w3.org/2000/svg", "")
    assert s.startswith("<svg") and s.endswith("</svg>")


def test_svg_geometry_smooth_and_monotonic():
    lo = {d: 0.0 for d in anchors.DIMENSIONS}
    hi = {d: 1.0 for d in anchors.DIMENSIONS}
    mid = renderer.interpolate_portrait(lo, hi, 0.5)
    assert all(v == pytest.approx(0.5) for v in mid.values())
    assert renderer.interpolate_portrait(lo, hi, 0.0) == lo
    assert renderer.interpolate_portrait(lo, hi, 1.0) == hi
    q1 = renderer.interpolate_portrait(lo, hi, 0.25)["directness"]
    q3 = renderer.interpolate_portrait(lo, hi, 0.75)["directness"]
    assert 0.0 < q1 < 0.5 < q3 < 1.0
    # same silhouette input from a snapshot renders identically
    p = portrait.Portrait()
    assert renderer.render_portrait_svg(p.snapshot()) == \
        renderer.render_portrait_svg(p.snapshot())


def test_svg_rejects_wrong_arity_and_clamps():
    with pytest.raises(ValueError):
        renderer.render_svg([0.5, 0.5])
    s = renderer.render_svg({d: 5.0 for d in anchors.DIMENSIONS})  # clamped, no crash
    assert s == renderer.render_svg({d: 1.0 for d in anchors.DIMENSIONS})
    assert renderer.render_svg({d: -1.0 for d in anchors.DIMENSIONS}) == \
        renderer.render_svg({d: 0.0 for d in anchors.DIMENSIONS})


def test_portrait_values_always_normalized(vecs):
    p = portrait.Portrait()
    for text in ["Brainstorm every option, even wild ones.", "Fix it.",
                 "Keep this narrow: only the auth module."]:
        p.update_observed_from_text(text, encoder.Tier0SketchBackend(), anchor_vectors=vecs)
        snap = p.snapshot()
        assert all(0.0 <= v <= 1.0 for v in snap["values"].values())
        assert all(0.0 <= c <= 1.0 for c in snap["confidences"].values())
        assert set(snap["values"]) == set(anchors.DIMENSIONS)
