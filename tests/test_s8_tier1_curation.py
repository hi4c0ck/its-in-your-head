"""S8 tier-1 curation round (L9): owned-vocabulary separation of the two
remaining confusion pairs.

Pins:
- the L9 target lists (prompt banks in anchors.py, agent lists in
  patterns.py) for context_richness, analytical_depth, directness, scope;
- the owned-vocabulary split: each pair's members own disjoint verbs/nouns;
- the NON-target patterns (exploration, iteration), byte-identical to
  ae83108 (same reference values as the S7 pins, which were taken at
  e3ae8bc and unchanged through ae83108);
- the probe's NEAR_CURATED2 set (separate from the original 18 and L7 5).
"""
import importlib.util
from pathlib import Path

from perception import patterns
from perception.anchors import ANCHORS, DIMENSIONS

_PROBE_PATH = Path(__file__).resolve().parents[1] / "tools" / "probe_pattern_quality.py"
_spec = importlib.util.spec_from_file_location("probe_pattern_quality", _PROBE_PATH)
assert _spec is not None and _spec.loader is not None
_probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_probe)
NEAR = _probe.NEAR
NEAR_CURATED = _probe.NEAR_CURATED
NEAR_CURATED2 = _probe.NEAR_CURATED2

# L9 prompt-side banks (tier-1-aware rewrite: supply vs reasoning, result vs
# territory).
L9_PROMPT = {
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
        "Here is the context: the background, setup, and history, all supplied.",
        "Here is the full background and prior attempts, all supplied up front.",
        "Pasted below: the full trace and the logs we collected.",
        "Attached: the background, versions, and setup details, all given.",
        "Given up front: the environment, OS, constraints, and what was decided.",
        "Supplied material: docs, repro steps, and what was tried before.",
        "Context included: what we tried, what failed, and the current setup.",
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

# L9 agent-side lists (first-person mirror of the same owned vocabulary).
L9_AGENT = {
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

# ae83108 reference values for the non-target patterns (unchanged since e3ae8bc).
AE_AGENT_NON_TARGET = {
    "exploration": [
        "I am brainstorming alternatives before deciding",
        "I am listing several options for the user to pick from",
        "I am looking for angles the user may have missed",
        "I am widening the search before narrowing down",
    ],
    "iteration": [
        "I am revising my previous answer with the new feedback",
        "I am keeping the good parts and tightening the rest",
        "I am taking another pass over the draft",
        "I am polishing the result step by step",
    ],
}
AE_PROMPT_NON_TARGET = {
    "exploration": [
        "Brainstorm every option you can think of.",
        "What else could we try? List alternatives.",
        "Think sideways — what are we missing?",
        "Explore creative angles before deciding.",
        "Give me ten ideas, even wild ones.",
        "What if we took a completely different approach?",
        "Cast a wide net and see what turns up.",
    ],
    "iteration": [
        "Revise that, try again with feedback.",
        "Iterate on the last version, keep the good parts.",
        "One more pass — tighten the wording.",
        "Refine this draft, then refine again.",
        "Take another swing at it.",
        "Loop back and polish the result.",
        "Keep going, improve it step by step.",
    ],
}
# Negative banks were never touched by L7 or L9; pin the target dims' too
# via the ae83108 values for the non-targets and shape for all.
AE_NEGATIVE_NON_TARGET = {
    "exploration": [
        "Just do the one thing. No options.",
        "Pick the obvious answer and stop.",
        "Don't overthink it.",
        "Execute the plan as written.",
        "One try, that is all we need.",
        "No more ideas, just ship it.",
        "Stick to the script, nothing else.",
    ],
    "iteration": [
        "One shot, get it right first time.",
        "Final answer, no revisions.",
        "Don't revisit this again.",
        "Perfect on the first try or nothing.",
        "Single pass only.",
        "Once is enough, move on for good.",
        "No loops, no second drafts.",
    ],
}


def _agent_list(name):
    entry = next(e for e in patterns.PATTERN_BANK if e["name"] == name)
    return entry["agent_paraphrases"]


def _joined(strings):
    return " ".join(strings).lower()


# -- pinned L9 lists -----------------------------------------------------------

def test_l9_prompt_banks_pinned():
    for name, expected in L9_PROMPT.items():
        assert ANCHORS[name]["positive"] == expected, name


def test_l9_agent_lists_pinned():
    for name, expected in L9_AGENT.items():
        assert _agent_list(name) == expected, name


def test_bank_shape_kept_seven_positive_seven_negative():
    for dim in DIMENSIONS:
        assert len(ANCHORS[dim]["positive"]) == 7, dim
        assert len(ANCHORS[dim]["negative"]) == 7, dim


# -- owned-vocabulary split ----------------------------------------------------

def test_context_richness_owns_supply_analytical_depth_owns_reasoning():
    cr = _joined(ANCHORS["context_richness"]["positive"])
    ad = _joined(ANCHORS["analytical_depth"]["positive"])
    # supply / given-material vocabulary lives only on the CR side
    for word in ("supplied", "pasted", "attached", "given up front"):
        assert word in cr, word
    # reasoning-demand vocabulary lives only on the AD side
    for word in ("derive", "prove", "root cause", "trade-offs", "hypothesis"):
        assert word in ad, word
    # the two territories must not bleed into each other
    for word in ("derive", "prove", "root cause"):
        assert word not in cr, word
    for word in ("supplied", "pasted"):
        assert word not in ad, word


def test_scope_owns_territory_directness_owns_result_form():
    sc = _joined(ANCHORS["scope"]["positive"])
    di = _joined(ANCHORS["directness"]["positive"])
    # territory nouns are scope-owned
    for word in ("module", "area", "endpoint", "out of scope"):
        assert word in sc, word
    # result/form nouns are directness-owned
    for word in ("number", "answer", "verdict", "bottom line"):
        assert word in di, word
    # territory nouns are not directness-owned
    for word in ("module", "endpoint", "out of scope"):
        assert word not in di, word
    # result nouns are not scope-owned
    for word in ("number", "verdict", "bottom line"):
        assert word not in sc, word


# -- non-target patterns byte-identical to ae83108 ------------------------------

def test_non_target_agent_lists_byte_identical_to_ae83108():
    for name, expected in AE_AGENT_NON_TARGET.items():
        assert _agent_list(name) == expected, name


def test_non_target_prompt_banks_byte_identical_to_ae83108():
    for name, expected in AE_PROMPT_NON_TARGET.items():
        assert ANCHORS[name]["positive"] == expected, name
    for name, expected in AE_NEGATIVE_NON_TARGET.items():
        assert ANCHORS[name]["negative"] == expected, name


# -- probe sets: original + L7 pristine, L9 separate ----------------------------

def test_probe_original_and_l7_sets_untouched():
    assert sum(len(v) for v in NEAR.values()) == 18
    assert all(len(v) == 3 for v in NEAR.values())
    assert sum(len(v) for v in NEAR_CURATED.values()) == 5


def test_probe_curated2_set_pinned():
    assert NEAR_CURATED2 == {
        "context_richness": [
            ("Supplied up front: background and setup; take it as given.",
             "analytical_depth"),
        ],
        "analytical_depth": [
            ("Given the supplied logs, derive the mechanism and prove the cause.",
             "context_richness"),
        ],
        "scope": [
            ("Just this module only, bluntly: what is the verdict?",
             "directness"),
        ],
        "directness": [
            ("No preamble: only the number, and leave the rest of the system alone.",
             "scope"),
        ],
    }
