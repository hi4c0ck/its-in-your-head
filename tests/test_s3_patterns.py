"""S3 pattern bank + amplification scoring (D3). Byte-sized fakes only."""
import hashlib
import math

import pytest

from perception import patterns
from perception.anchors import ANCHORS, DIMENSIONS
from perception.portrait import Portrait

DIM = 8


class FakeBackend:
    """Deterministic local fake: records every call, never touches network."""

    def __init__(self, model_version="fake-v1"):
        self.model_version = model_version
        self.calls = []

    def encode(self, texts):
        self.calls.append(("encode", list(texts)))
        return [_vec(t) for t in texts]


def _vec(text):
    out = []
    for i in range(DIM):
        h = hashlib.sha256(f"{text}:{i}".encode()).digest()
        out.append((h[0] / 127.5) - 1.0)
    return out


def _norm(v):
    return math.sqrt(sum(x * x for x in v))


@pytest.fixture(autouse=True)
def _fresh_cache():
    patterns._BANK_CACHE.clear()
    yield
    patterns._BANK_CACHE.clear()


def _hand_bank():
    return {
        "alpha": {"prompt_anchor": [1.0, 0.0], "agent_anchor": [1.0, 0.0]},
        "beta": {"prompt_anchor": [0.0, 1.0], "agent_anchor": [0.0, 1.0]},
    }


# -- preset bank determinism ------------------------------------------------ #

def test_preset_bank_reuses_anchor_prompt_lists():
    assert len(patterns.PATTERN_BANK) == len(DIMENSIONS)
    for entry in patterns.PATTERN_BANK:
        dim = entry["name"]
        assert entry["prompt_paraphrases"] is ANCHORS[dim]["positive"]
        assert 4 <= len(entry["agent_paraphrases"]) <= 6
        assert all(s.strip() for s in entry["agent_paraphrases"])


def test_build_is_deterministic_and_local():
    backend = FakeBackend()
    first = patterns.build_pattern_bank(backend, backend.model_version)
    patterns._BANK_CACHE.clear()
    second = patterns.build_pattern_bank(backend, backend.model_version)
    assert first == second
    assert set(first) == set(DIMENSIONS)
    for anchors in first.values():
        assert _norm(anchors["prompt_anchor"]) == pytest.approx(1.0)
        assert _norm(anchors["agent_anchor"]) == pytest.approx(1.0)
    # Every recorded call hit the local encode path; nothing else exists
    # on the fake (no network surface to call).
    assert backend.calls and all(c[0] == "encode" for c in backend.calls)
    assert not hasattr(backend, "fetch") and not hasattr(backend, "request")


def test_model_bump_never_reuses_cache():
    backend = FakeBackend("v1")
    patterns.build_pattern_bank(backend, "v1")
    n_calls = len(backend.calls)
    patterns.build_pattern_bank(backend, "v1")  # cache hit: no encode
    assert len(backend.calls) == n_calls
    patterns.build_pattern_bank(backend, "v2")  # new space: re-encode
    assert len(backend.calls) > n_calls


# -- amplification is cosine-only, never magnitude -------------------------- #

def test_amplification_ignores_vector_magnitude():
    bank = _hand_bank()
    # Same directions, different magnitudes -> identical amplification.
    r1 = patterns.score_turn([2.0, 0.0], [3.0, 0.0], bank)
    r2 = patterns.score_turn([5.0, 0.0], [7.0, 0.0], bank)
    a1 = {p["name"]: p for p in r1["patterns"]}
    a2 = {p["name"]: p for p in r2["patterns"]}
    assert a1["alpha"]["amplification"] == pytest.approx(
        a2["alpha"]["amplification"])
    # Equal magnitudes, different directions -> different amplification.
    r3 = patterns.score_turn([2.0, 0.0], [0.0, 3.0], bank)
    a3 = {p["name"]: p for p in r3["patterns"]}
    assert a3["alpha"]["amplification"] != pytest.approx(
        a1["alpha"]["amplification"])
    assert a3["alpha"]["agent_affinity"] == pytest.approx(0.0)


# -- competitor margin ------------------------------------------------------- #

def test_competitor_margin_small_for_near_duplicates():
    near = {
        "a": {"prompt_anchor": [1.0, 0.0], "agent_anchor": [1.0, 0.0]},
        "b": {"prompt_anchor": [1.0, 0.0], "agent_anchor": [0.999, 0.01]},
    }
    out = patterns.score_turn([1.0, 0.0], [1.0, 0.0], near)
    top = {p["name"]: p for p in out["patterns"]}["a"]
    assert top["margin"] is not None and top["margin"] < 0.05


def test_competitor_margin_large_for_far_pattern():
    out = patterns.score_turn([1.0, 0.0], [1.0, 0.0], _hand_bank())
    top = {p["name"]: p for p in out["patterns"]}["alpha"]
    assert top["margin"] == pytest.approx(1.0)
    assert out["patterns"][0]["name"] == "alpha"  # sorted agent desc


# -- confidence fusion ------------------------------------------------------- #

def test_confidence_monotone_in_n_and_saturates():
    vals = [patterns.confidence_from_evidence(n, 1.0, 0) for n in (0, 1, 5, 10)]
    assert vals == sorted(vals) and vals[0] == 0.0
    assert vals[-1] == pytest.approx(1.0)
    assert patterns.confidence_from_evidence(50, 1.0, 0) <= 1.0  # clipped


def test_confidence_zero_when_agreement_zero():
    assert patterns.confidence_from_evidence(10, 0.0, 0) == 0.0


def test_confidence_seven_day_half_life():
    fresh = patterns.confidence_from_evidence(5, 1.0, 0)
    week = patterns.confidence_from_evidence(5, 1.0, 7 * 24 * 60 * 60)
    assert week == pytest.approx(fresh / 2, rel=1e-9)
    # Epoch-timestamp form: age = now_s - age_s.
    ts = patterns.confidence_from_evidence(
        5, 1.0, 1_700_000_000, 1_700_000_000 + 7 * 24 * 60 * 60)
    assert ts == pytest.approx(week)


# -- reaction_vector None ---------------------------------------------------- #

def test_reaction_none_scores_prompt_side_only():
    out = patterns.score_turn([1.0, 0.0], None, _hand_bank())
    assert out["patterns"] and all(p["agent_affinity"] is None
                                   for p in out["patterns"])
    assert all(p["margin"] is None and p["amplification"] is None
               for p in out["patterns"])
    assert all(p["prompt_affinity"] is not None for p in out["patterns"])
    assert all(p["evidence"] == 0 and p["confidence"] is None
               for p in out["patterns"])
    assert all(s["agent"] is None and s["prompt"] is not None
               for s in out["dim_signals"].values())


def test_prompt_only_mode_ranks_by_prompt_affinity_not_bank_order():
    """P1 mode fix: with no reaction vector, all agent affinities are None;
    the prompt-only list must rank by prompt affinity, not retain bank
    order (dict insertion)."""
    bank = {
        "low": {"prompt_anchor": [0.0, 1.0], "agent_anchor": [0.0, 1.0]},
        "high": {"prompt_anchor": [1.0, 0.0], "agent_anchor": [1.0, 0.0]},
    }  # 'low' first in dict, but 'high' has the higher prompt affinity.
    out = patterns.score_turn([1.0, 0.0], None, bank)
    assert [p["name"] for p in out["patterns"]] == ["high", "low"]
    assert out["patterns"][0]["prompt_affinity"] == 1.0
    assert out["patterns"][1]["prompt_affinity"] == 0.0


# -- portrait third view ------------------------------------------------------ #

def _turn(top="alpha"):
    bank = _hand_bank()
    if top == "alpha":
        return patterns.score_turn([1.0, 0.0], [1.0, 0.0], bank)
    return patterns.score_turn([0.0, 1.0], [0.0, 1.0], bank)


def test_portrait_third_view_empty_state():
    snap = Portrait().snapshot()
    assert snap["perception_available"] is False
    assert snap["patterns"] is None and snap["dim_signals"] is None
    assert snap["repeated"] is False and snap["repeated_pattern"] is None


def test_portrait_repeated_needs_three_consecutive_turns():
    p = Portrait()
    p.update_patterns(_turn())
    p.update_patterns(_turn())
    snap = p.snapshot()
    assert snap["perception_available"] is True
    assert snap["repeated"] is False  # 2 consecutive is not repeated
    assert snap["patterns"][0]["name"] == "alpha"
    assert snap["patterns"][0]["evidence"] == 2
    p.update_patterns(_turn())
    snap = p.snapshot()
    assert snap["repeated"] is True
    assert snap["repeated_pattern"] == "alpha"
    assert snap["patterns"][0]["repeated"] is True
    p.update_patterns(_turn(top="beta"))  # streak breaks
    assert p.snapshot()["repeated"] is False


def test_update_interpreted_tuple_form_folds_confidence():
    p = Portrait()
    p.update_interpreted({"directness": (0.9, 0.8)})
    assert p.snapshot()["interpreted"]["confidence"]["directness"] > 0
    q = Portrait()
    q.update_interpreted({"directness": 0.9})  # bare float still works
    assert q.snapshot()["interpreted"]["confidence"]["directness"] == 0.0
