"""S7 curation round (L7): target-pair sharpening + evidence inheritance.

Pins:
- the two confusion pairs' lists (agent-side curated in patterns.py,
  prompt-side curated in anchors.py) — so a later lane cannot silently
  re-merge the vocabulary;
- the NON-target patterns' lists, still byte-identical to e3ae8bc;
- the L5 open-item fix: a custom override of a reinforced name inherits
  the persisted evidence/updated_at instead of resetting to 0.
"""
import hashlib
import importlib.util
import math
import sqlite3
from pathlib import Path

import pytest

from perception import patterns
from perception.anchors import ANCHORS, DIMENSIONS

# tools/ is not a package (no __init__.py); load the probe script by path so
# the NEAR data sets are pinned without duplicating their content.
_PROBE_PATH = Path(__file__).resolve().parents[1] / "tools" / "probe_pattern_quality.py"
_spec = importlib.util.spec_from_file_location("probe_pattern_quality", _PROBE_PATH)
assert _spec is not None and _spec.loader is not None
_probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_probe)
NEAR = _probe.NEAR
NEAR_CURATED = _probe.NEAR_CURATED

DIM = 8


class FakeBackend:
    """Deterministic fake; same discipline as test_s6."""

    def __init__(self, model_version="fake-v1"):
        self.model_version = model_version
        self.calls = []

    def encode(self, texts):
        self.calls.append(list(texts))
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


# ---------------------------------------------------------------------------
# e3ae8bc reference values (byte-identical before the curation round)
# ---------------------------------------------------------------------------

E3_AGENT_EXPLORATION = [
    "I am brainstorming alternatives before deciding",
    "I am listing several options for the user to pick from",
    "I am looking for angles the user may have missed",
    "I am widening the search before narrowing down",
]
E3_AGENT_ITERATION = [
    "I am revising my previous answer with the new feedback",
    "I am keeping the good parts and tightening the rest",
    "I am taking another pass over the draft",
    "I am polishing the result step by step",
]
E3_PROMPT_EXPLORATION = [
    "Brainstorm every option you can think of.",
    "What else could we try? List alternatives.",
    "Think sideways — what are we missing?",
    "Explore creative angles before deciding.",
    "Give me ten ideas, even wild ones.",
    "What if we took a completely different approach?",
    "Cast a wide net and see what turns up.",
]
E3_PROMPT_ITERATION = [
    "Revise that, try again with feedback.",
    "Iterate on the last version, keep the good parts.",
    "One more pass — tighten the wording.",
    "Refine this draft, then refine again.",
    "Take another swing at it.",
    "Loop back and polish the result.",
    "Keep going, improve it step by step.",
]

# Curated agent-side lists (L7 rewrite; context-setting vs reasoning-density,
# boundary vs bluntness).
CURATED_AGENT = {
    "directness": [
        "I am answering bluntly with no preamble or pleasantries",
        "I am giving the short bottom line in plain words",
        "I am skipping the intro and leading with the answer",
        "I am not softening it with friendly filler",
    ],
    "context_richness": [
        "I am taking the supplied logs, trace, and background as given",
        "I am using the setup, constraints, and prior attempts already provided",
        "I am grounding myself in the pasted material before acting",
        "I am treating the attached details as my starting point",
    ],
    "analytical_depth": [
        "I am deriving why this happens and proving the mechanism",
        "I am analyzing the trade-offs and weighing the hypotheses with numbers",
        "I am tracing the root cause step by step from first principles",
        "I am explaining the underlying reason, not just giving a label",
    ],
    "scope": [
        "I am staying inside this module only, nothing wider",
        "I am stopping when that part is done and leaving the rest out",
        "I am limiting my change to just this one area",
        "I am not touching anything outside the boundary",
    ],
}

# Curated prompt-side banks (L7 rewrite; same signal discipline).
# CUR3 disclosure: the context_richness list moved — narrowed to
# user-SUPPLIED material (see anchors.py v1.1.0 note). The other three
# target lists are byte-identical to L7/L9.
CURATED_PROMPT = {
    "directness": [
        "Only the number, plainly.",
        "Just the answer, nothing extra.",
        "Only the verdict, bluntly, no preamble.",
        "The bottom line only, in plain words.",
        "Short answer only - skip the pleasantries.",
        "One plain line, straight to the point.",
        "Answer briefly, straight up, no fluff.",
    ],
    "context_richness": [
        "Attached: the failing config, the stack trace, and the versions, all given as-is.",
        "Pasted below: the full trace and the logs we collected.",
        "Here are the error logs and the version numbers, pasted in full.",
        "Supplied material: docs, repro steps, and what was tried before.",
        "Given up front: the environment, OS, constraints, and what was decided.",
        "Supplied: the dependency tree, the feature flags, and the failing case.",
        "Here is the data sample and the current schema, attached as-is.",
    ],
    "analytical_depth": [
        "Why does this happen? Derive the mechanism from first principles.",
        "Prove which hypothesis fits with evidence and numbers.",
        "Analyze the trade-offs numerically and compare pros and cons.",
        "Find the root cause: trace the mechanism and stress-test the logic.",
        "Break the failure down into first principles and do a post-mortem.",
        "Analyze the attached material in depth and explain the root cause.",
        "Weigh the evidence, rank the hypotheses by likelihood, and justify the verdict.",
    ],
    "scope": [
        "Just this module, nothing else.",
        "Only this part - leave the rest alone.",
        "Just this one file; stop when it is fixed.",
        "Keep the change inside this area, nothing outside.",
        "Out of scope: everything else stays untouched.",
        "Only this endpoint, nothing wider.",
        "Just this function and its test, nothing else.",
    ],
}


def _agent_list(name):
    entry = next(e for e in patterns.PATTERN_BANK if e["name"] == name)
    return entry["agent_paraphrases"]


# -- curation direction pins -------------------------------------------------

def test_non_target_agent_lists_unchanged_from_e3ae8bc():
    assert _agent_list("exploration") == E3_AGENT_EXPLORATION
    assert _agent_list("iteration") == E3_AGENT_ITERATION


def test_non_target_prompt_banks_unchanged_from_e3ae8bc():
    assert ANCHORS["exploration"]["positive"] == E3_PROMPT_EXPLORATION
    assert ANCHORS["iteration"]["positive"] == E3_PROMPT_ITERATION


def test_target_agent_lists_are_the_curated_versions():
    for name, expected in CURATED_AGENT.items():
        assert _agent_list(name) == expected, name


def test_target_prompt_banks_are_the_curated_versions():
    for name, expected in CURATED_PROMPT.items():
        assert ANCHORS[name]["positive"] == expected, name


def test_all_dimensions_have_seven_positive_banks():
    for dim in DIMENSIONS:
        assert len(ANCHORS[dim]["positive"]) == 7, dim
        assert len(ANCHORS[dim]["negative"]) == 7, dim


def test_probe_script_near_sets_pristine_plus_curated():
    # Original L6 near set: 3 probes per dimension, byte-unchanged.
    assert sum(len(v) for v in NEAR.values()) == 18
    assert all(len(v) == 3 for v in NEAR.values())
    # L7 curated additions: 2 for context_richness, 1 each for the other
    # three target dimensions, 0 for the non-targets.
    assert NEAR_CURATED == {
        "context_richness": NEAR_CURATED["context_richness"],
        "analytical_depth": NEAR_CURATED["analytical_depth"],
        "scope": NEAR_CURATED["scope"],
        "directness": NEAR_CURATED["directness"],
    }
    assert len(NEAR_CURATED["context_richness"]) == 2
    assert len(NEAR_CURATED["analytical_depth"]) == 1
    assert len(NEAR_CURATED["scope"]) == 1
    assert len(NEAR_CURATED["directness"]) == 1


# -- evidence inheritance (L5 open item b) -----------------------------------

def _custom(name="scope", prompt=None, agent=None):
    return {
        "name": name,
        "prompt": prompt if prompt is not None else ["custom scope prompt"],
        "agent": agent if agent is not None else ["custom scope agent"],
    }


def _reinforced_scope(evidence=7, updated_at=1234.5):
    return {"scope": {
        "prompt_anchor": [1.0] * DIM,
        "agent_anchor": [1.0] * DIM,
        "evidence": evidence,
        "updated_at": updated_at,
    }}


def test_custom_override_inherits_reinforced_evidence_and_updated_at():
    backend = FakeBackend()
    bank = patterns.build_pattern_bank(
        backend,
        custom_patterns=[_custom()],
        reinforced=_reinforced_scope(),
    )
    entry = bank["scope"]
    expected_prompt = patterns._mean_normalized(
        backend.encode(["custom scope prompt"]))
    expected_agent = patterns._mean_normalized(
        backend.encode(["custom scope agent"]))
    assert entry["prompt_anchor"] == expected_prompt
    assert entry["agent_anchor"] == expected_agent
    assert entry["evidence"] == 7
    assert entry["updated_at"] == 1234.5


def test_custom_new_name_without_reinforcement_gets_zero_evidence():
    backend = FakeBackend()
    bank = patterns.build_pattern_bank(
        backend,
        custom_patterns=[_custom(name="brand_new")],
    )
    entry = bank["brand_new"]
    assert entry["evidence"] == 0
    assert entry["updated_at"] is None


def test_reinforced_new_name_roundtrip_preserves_vectors_and_updated_at():
    conn = sqlite3.connect(":memory:")
    entries = {"scope": {
        "prompt_anchor": [0.5] * DIM,
        "agent_anchor": [0.25] * DIM,
        "evidence": 3,
    }}
    patterns.persist_reinforced(conn, entries)
    loaded = patterns.load_reinforced(conn)
    assert loaded["scope"]["evidence"] == 3
    assert isinstance(loaded["scope"]["updated_at"], float)
    for key in ("prompt_anchor", "agent_anchor"):
        for a, b in zip(entries["scope"][key], loaded["scope"][key]):
            assert abs(a - b) < 1e-6


def test_preset_entries_stay_zero_evidence_when_no_reinforcement():
    backend = FakeBackend()
    bank = patterns.build_pattern_bank(backend)
    for name, entry in bank.items():
        assert entry["evidence"] == 0
        assert entry["updated_at"] is None
