#!/usr/bin/env python3
"""L6 pattern-quality probe: does the shipped preset bank + Tier-0 sketch
backend actually discriminate the six patterns?

Deterministic, stdlib + repo imports only. Zero network, no LLM, no
randomness, no timestamps. Re-runnable: same input -> identical report
body (sha256 printed at the end).

Run from the repo root:  python tools/probe_pattern_quality.py
Writes probe-report.md at the repo root of this clone + a console summary.
"""
from __future__ import annotations

import hashlib
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from perception import patterns as pat  # noqa: E402
from perception.anchors import ANCHORS, DIMENSIONS  # noqa: E402
from perception.encoder import Tier0SketchBackend, TIER0_MODEL_VERSION  # noqa: E402

# --------------------------------------------------------------------------
# Authored probe sets (inline; short, deterministic)
# --------------------------------------------------------------------------

# Fresh in-domain sentences (same spirit as each dimension, new wording).
FRESH_IN_DOMAIN: dict[str, list[str]] = {
    "directness": [
        "Just the answer, nothing extra.",
        "One line only, please.",
        "Straight up: what is it?",
    ],
    "context_richness": [
        "Attached: the log, the config, and my repro steps.",
        "Background included: what I tried and what failed.",
        "Full error trace pasted below, plus versions.",
    ],
    "exploration": [
        "Give me a few different directions to choose from.",
        "What other ways could we approach this?",
        "List some wild ideas before we settle.",
    ],
    "analytical_depth": [
        "Rank these by evidence and show your working.",
        "What is the root cause? Break it down.",
        "Compare costs and risks numerically.",
    ],
    "iteration": [
        "Take another pass and tighten it.",
        "Revise from the feedback, keep what works.",
        "Polish this draft once more.",
    ],
    "scope": [
        "Only the login module, nothing wider.",
        "One file, one fix, leave the rest.",
        "Keep it tiny and local.",
    ],
}

# NEAR probes: true intent = key pattern, but surface wording pulls toward
# a neighbouring pattern (the discrimination stress case).
# value: list of (text, pulls_toward)
NEAR: dict[str, list[tuple[str, str]]] = {
    "directness": [
        ("Only the number, and keep it to this one file too.", "scope"),
        ("Answer briefly and do not touch anything else.", "scope"),
        ("Short reply only, no big redesign talk.", "scope"),
    ],
    "context_richness": [
        ("Here are the logs; weigh what they imply step by step.", "analytical_depth"),
        ("Pasted the full trace and config for your analysis.", "analytical_depth"),
        ("All the background, ranked by likelihood.", "analytical_depth"),
    ],
    "exploration": [
        ("Brainstorm options and compare their trade-offs numerically.", "analytical_depth"),
        ("List every angle we have not weighed yet.", "analytical_depth"),
        ("Wild ideas first, then rank them by evidence.", "analytical_depth"),
    ],
    "analytical_depth": [
        ("Break down the logs we pasted into root causes.", "context_richness"),
        ("Analyze the attached config and error output in depth.", "context_richness"),
        ("Decompose the background and compare hypotheses by numbers.", "exploration"),
    ],
    "iteration": [
        ("Revise it once more, just the short version please.", "directness"),
        ("One more pass: make it brief and final.", "directness"),
        ("Polish step by step, keep every earlier good part.", "context_richness"),
    ],
    "scope": [
        ("Just this module, and answer in one sentence.", "directness"),
        ("Tiny fix only, bottom line, no extra areas touched.", "directness"),
        ("Keep it narrow and iterate only on this file.", "iteration"),
    ],
}

# L7 curation round: fresh near probes for the two target confusion pairs
# (context_richness <-> analytical_depth, scope <-> directness). Scored and
# reported SEPARATELY so the original 18 stay apples-to-apples vs L6.
NEAR_CURATED: dict[str, list[tuple[str, str]]] = {
    "context_richness": [
        ("Here is the context, now tell me the underlying reason.",
         "analytical_depth"),
        ("Setup, constraints, and history attached; derive what comes next.",
         "analytical_depth"),
    ],
    "analytical_depth": [
        ("Trace the mechanism from the pasted logs, step by step.",
         "context_richness"),
    ],
    "scope": [
        ("Only this part, then state the blunt result.", "directness"),
    ],
    "directness": [
        ("No preamble: just the verdict for this module only.", "scope"),
    ],
}

OUT_OF_DOMAIN: list[str] = [
    "What time is it in Berlin right now?",
    "Remind me to buy milk on Friday.",
    "Can you review this pull request for style issues?",
    "The meeting moved to 3pm, see you there.",
    "What is the capital of France?",
    "My train leaves at six, check the platform board.",
    "Spellcheck this paragraph for me.",
    "Ping me when the build finishes.",
]

# --------------------------------------------------------------------------
# Scoring helpers
# --------------------------------------------------------------------------


def fmt(x: float | None) -> str:
    return "-" if x is None else f"{x:.6f}"


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def rank_by(res: dict, key: str) -> list[dict]:
    """Rank score_turn patterns by affinity desc (ties: name)."""
    field = f"{key}_affinity"
    rows = [p for p in res["patterns"] if p[field] is not None]
    return sorted(rows, key=lambda p: (-p[field], p["name"]))


def score_prompt_only(vec, bank):
    """score_turn with no reaction; top-1 ranked by prompt_affinity."""
    res = pat.score_turn(vec, None, bank)
    ranked = rank_by(res, "prompt")
    top = ranked[0]
    runner = ranked[1] if len(ranked) > 1 else None
    return {
        "top1": top["name"],
        "top1_aff": top["prompt_affinity"],
        "runner_up": runner["name"] if runner else None,
        "runner_up_aff": runner["prompt_affinity"] if runner else None,
        "margin": (top["prompt_affinity"] - runner["prompt_affinity"])
        if runner else None,
        "amplification": None,
    }


def score_with_reaction(vec, rvec, bank):
    """score_turn with reaction; top-1 ranked by agent_affinity (sort order)."""
    res = pat.score_turn(vec, rvec, bank)
    ranked = rank_by(res, "agent")
    top = ranked[0]
    runner = ranked[1] if len(ranked) > 1 else None
    # amplification of the top-1 entry as computed by score_turn
    amp = top["amplification"]
    return {
        "top1": top["name"],
        "top1_aff": top["agent_affinity"],
        "runner_up": runner["name"] if runner else None,
        "runner_up_aff": runner["agent_affinity"] if runner else None,
        "margin": top["margin"],
        "amplification": amp,
    }


# --------------------------------------------------------------------------
# Probe run
# --------------------------------------------------------------------------


def main() -> None:
    backend = Tier0SketchBackend()
    assert backend.model_version == TIER0_MODEL_VERSION
    bank = pat.build_pattern_bank(backend, backend.model_version)

    def enc(texts: list[str]) -> list[list[float]]:
        return backend.encode(list(texts))

    # ---- IN-DOMAIN: bank paraphrases + fresh ------------------------------
    in_domain: dict[str, list[dict]] = {}
    for dim in DIMENSIONS:
        texts = list(ANCHORS[dim]["positive"]) + list(FRESH_IN_DOMAIN[dim])
        vecs = enc(texts)
        rows = []
        for text, vec in zip(texts, vecs):
            s = score_prompt_only(vec, bank)
            s.update({"text": text, "expected": dim})
            rows.append(s)
        in_domain[dim] = rows

    # ---- NEAR: cross probes ------------------------------------------------
    near: dict[str, list[dict]] = {}
    error_matrix: Counter[tuple[str, str]] = Counter()  # (expected, picked)
    for dim in DIMENSIONS:
        texts = [t for t, _ in NEAR[dim]]
        vecs = enc(texts)
        rows = []
        for (text, pull), vec in zip(NEAR[dim], vecs):
            s = score_prompt_only(vec, bank)
            s.update({"text": text, "expected": dim, "pulls_toward": pull})
            rows.append(s)
            error_matrix[(dim, s["top1"])] += 1
        near[dim] = rows

    # ---- NEAR (L7 curated): separate set, keeps the original 18 pristine ----
    near_curated: dict[str, list[dict]] = {}
    for dim in DIMENSIONS:
        rows = []
        for (text, pull) in NEAR_CURATED.get(dim, []):
            s = score_prompt_only(enc([text])[0], bank)
            s.update({"text": text, "expected": dim, "pulls_toward": pull})
            rows.append(s)
        near_curated[dim] = rows

    # ---- OUT-OF-DOMAIN -----------------------------------------------------
    ood_vecs = enc(OUT_OF_DOMAIN)
    ood_rows = []
    for text, vec in zip(OUT_OF_DOMAIN, ood_vecs):
        s = score_prompt_only(vec, bank)
        s.update({"text": text, "expected": None})
        ood_rows.append(s)

    # ---- REACTION: agent paraphrases vs own in-domain prompt probes -------
    # Every agent paraphrase (individual vector) paired with every
    # in-domain prompt probe (bank + fresh) of its own pattern.
    reaction: dict[str, list[dict]] = {}
    for dim in DIMENSIONS:
        entry = next(e for e in pat.PATTERN_BANK if e["name"] == dim)
        prompt_texts = list(ANCHORS[dim]["positive"]) + list(FRESH_IN_DOMAIN[dim])
        agent_texts = list(entry["agent_paraphrases"])
        prompt_vecs = enc(prompt_texts)
        agent_vecs = enc(agent_texts)
        rows = []
        for ptext, pvec in zip(prompt_texts, prompt_vecs):
            for atext, avec in zip(agent_texts, agent_vecs):
                s = score_with_reaction(pvec, avec, bank)
                # amplification for the intended pattern, not only top-1
                res = pat.score_turn(pvec, avec, bank)
                intended = next(p for p in res["patterns"] if p["name"] == dim)
                s.update({
                    "prompt_text": ptext,
                    "agent_text": atext,
                    "expected": dim,
                    "intended_amplification": intended["amplification"],
                    "intended_agent_aff": intended["agent_affinity"],
                    "intended_prompt_aff": intended["prompt_affinity"],
                })
                rows.append(s)
        reaction[dim] = rows

    # ----------------------------------------------------------------------
    # Aggregate
    # ----------------------------------------------------------------------
    def agg(rows):
        return {
            "n": len(rows),
            "top1_aff": [r["top1_aff"] for r in rows],
            "margin": [r["margin"] for r in rows if r["margin"] is not None],
        }

    def agree(rows, dim):
        return sum(1 for r in rows if r["top1"] == dim)

    # Per-pattern summary tables
    lines: list[str] = []
    lines.append("# L6 pattern-quality probe report")
    lines.append("")
    lines.append(f"- backend: tier-0 ({backend.model_version})")
    lines.append(f"- pattern_bank_version: {pat.PATTERN_BANK_VERSION}")
    lines.append(f"- patterns: {', '.join(DIMENSIONS)}")
    lines.append("- method: real Tier0SketchBackend encode; score_turn "
                 "prompt_affinity ranking for no-reaction probes, "
                 "agent_affinity ranking for reaction probes")
    lines.append("")

    lines.append("## Per-probe detail (prompt-mode probes, no reaction)")
    lines.append("")
    lines.append("### In-domain")
    lines.append("")
    lines.append("| expected | text | top1 | top1_aff | runner_up | margin |")
    lines.append("|---|---|---|---|---|---|")
    for dim in DIMENSIONS:
        for r in in_domain[dim]:
            lines.append(f"| {dim} | {r['text']} | {r['top1']} | "
                         f"{fmt(r['top1_aff'])} | {r['runner_up']} | "
                         f"{fmt(r['margin'])} |")
    lines.append("")
    lines.append("### Near (stress: true intent vs pulled-toward)")
    lines.append("")
    lines.append("| expected | pulls_toward | text | top1 | top1_aff | "
                 "runner_up | margin | correct |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for dim in DIMENSIONS:
        for r in near[dim]:
            lines.append(f"| {dim} | {r['pulls_toward']} | {r['text']} | "
                         f"{r['top1']} | {fmt(r['top1_aff'])} | "
                         f"{r['runner_up']} | {fmt(r['margin'])} | "
                         f"{'yes' if r['top1'] == dim else 'no'} |")
    lines.append("")
    lines.append("### Near probes — L7 curated (separate set)")
    lines.append("")
    lines.append("| expected | pulls_toward | text | top1 | top1_aff | "
                 "runner_up | margin | correct |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for dim in DIMENSIONS:
        for r in near_curated[dim]:
            lines.append(f"| {dim} | {r['pulls_toward']} | "
                         f"{r['text']} | {r['top1']} | "
                         f"{fmt(r['top1_aff'])} | {r['runner_up']} | "
                         f"{fmt(r['margin'])} | "
                         f"{'yes' if r['top1'] == dim else 'no'} |")
    lines.append("")
    lines.append("### Out-of-domain")
    lines.append("")
    lines.append("| text | top1 | top1_aff | runner_up | margin |")
    lines.append("|---|---|---|---|---|")
    for r in ood_rows:
        lines.append(f"| {r['text']} | {r['top1']} | {fmt(r['top1_aff'])} | "
                     f"{r['runner_up']} | {fmt(r['margin'])} |")
    lines.append("")

    lines.append("## In-domain agreement (prompt-mode, top-1 = affinity max)")
    lines.append("")
    lines.append("| pattern | probes | agreed | rate | mean_top1_aff | "
                 "mean_margin |")
    lines.append("|---|---|---|---|---|---|")
    in_rates: dict[str, float] = {}
    for dim in DIMENSIONS:
        rows = in_domain[dim]
        a = agg(rows)
        rate = agree(rows, dim) / len(rows)
        in_rates[dim] = rate
        lines.append(f"| {dim} | {a['n']} | {agree(rows, dim)} | "
                     f"{rate:.3f} | {mean(a['top1_aff']):.6f} | "
                     f"{mean(a['margin']):.6f} |")
    lines.append("")

    lines.append("## Margin comparison: in-domain vs near vs out-of-domain")
    lines.append("")
    lines.append("| pattern | mean_margin_in | mean_margin_near | "
                 "mean_margin_ood |")
    lines.append("|---|---|---|---|")
    for dim in DIMENSIONS:
        m_in = mean(agg(in_domain[dim])["margin"])
        n_rows = near[dim]
        m_near = mean([r["margin"] for r in n_rows if r["margin"] is not None])
        # OOD has no per-pattern split; show the global OOD mean per row
        m_ood = mean(agg(ood_rows)["margin"])
        lines.append(f"| {dim} | {m_in:.6f} | {m_near:.6f} | {m_ood:.6f} |")
    lines.append(f"| (out-of-domain pooled, n={len(ood_rows)}) | "
                 f"{mean(agg(ood_rows)['margin']):.6f} | | |")
    lines.append("")

    lines.append("## Near-probe error matrix (expected -> picked)")
    lines.append("")
    lines.append("| expected | picked | count |")
    lines.append("|---|---|---|")
    wrong = [(k, v) for k, v in sorted(error_matrix.items()) if k[0] != k[1]]
    for (exp, picked), c in sorted(error_matrix.items()):
        if c:
            lines.append(f"| {exp} | {picked} | {c} |")
    if not wrong:
        lines.append("| - | no errors | 0 |")
    lines.append("")
    # curation targets: most-confused pairs
    lines.append("Missed-to (picked instead of expected), most frequent first:")
    miss_counter: Counter[str] = Counter()
    for (exp, picked), c in wrong:
        miss_counter[picked] += c
    for picked, c in miss_counter.most_common():
        lines.append(f"- {picked}: {c} near-probe(s) landed here "
                     "(attractor pattern)")
    lines.append("")

    lines.append("## Out-of-domain affinity distribution vs in-domain")
    lines.append("")
    in_aff = mean([r["top1_aff"] for dim in DIMENSIONS for r in in_domain[dim]])
    ood_aff = mean([r["top1_aff"] for r in ood_rows])
    lines.append(f"- mean in-domain top-1 affinity: {in_aff:.6f}")
    lines.append(f"- mean out-of-domain top-1 affinity (best of six, spurious): "
                 f"{ood_aff:.6f}")
    lines.append(f"- separation (in - out): {in_aff - ood_aff:.6f}")
    lines.append(f"- mean in-domain margin: "
                 f"{mean([r['margin'] for dim in DIMENSIONS for r in in_domain[dim] if r['margin'] is not None]):.6f}")
    lines.append(f"- mean out-of-domain margin: "
                 f"{mean([r['margin'] for r in ood_rows if r['margin'] is not None]):.6f}")
    lines.append("")
    lines.append("| ood text | top1 | top1_aff |")
    lines.append("|---|---|---|")
    for r in ood_rows:
        lines.append(f"| {r['text']} | {r['top1']} | {fmt(r['top1_aff'])} |")
    lines.append("")

    lines.append("## Amplification (reaction probes, agent vs own prompt)")
    lines.append("")
    lines.append("| pattern | reaction probes | mean intended amplification "
                 "| mean intended agent_aff | mean intended prompt_aff | "
                 "agent_top1_rate |")
    lines.append("|---|---|---|---|---|---|")
    amp_by_pattern: dict[str, float] = {}
    agent_top1: dict[str, float] = {}
    for dim in DIMENSIONS:
        rows = reaction[dim]
        amps = [r["intended_amplification"] for r in rows
                if r["intended_amplification"] is not None]
        m_amp = mean(amps)
        amp_by_pattern[dim] = m_amp
        rate = sum(1 for r in rows if r["top1"] == dim) / len(rows)
        agent_top1[dim] = rate
        lines.append(f"| {dim} | {len(rows)} | {m_amp:.6f} | "
                     f"{mean([r['intended_agent_aff'] for r in rows]):.6f} | "
                     f"{mean([r['intended_prompt_aff'] for r in rows]):.6f} | "
                     f"{rate:.3f} |")
    lines.append("")
    ranked_amp = sorted(amp_by_pattern.items(), key=lambda kv: -kv[1])
    lines.append("Strongest agent-vs-prompt gap (mean amplification, desc):")
    for dim, v in ranked_amp:
        lines.append(f"- {dim}: {v:.6f}")
    lines.append("")
    overall_amp = mean([r["intended_amplification"]
                        for dim in DIMENSIONS for r in reaction[dim]
                        if r["intended_amplification"] is not None])
    lines.append(f"Overall mean amplification: {overall_amp:.6f} "
                 f"({'positive' if overall_amp > 0 else 'non-positive'})")
    lines.append("")

    # ----------------------------------------------------------------------
    # VERDICT
    # ----------------------------------------------------------------------
    min_rate = min(in_rates.values())
    all_two_thirds = all(v >= 2 / 3 for v in in_rates.values())
    separation = in_aff - ood_aff
    sep_visible = separation > 0
    # near-probe overall accuracy
    near_total = sum(len(v) for v in near.values())
    near_correct = sum(1 for dim in DIMENSIONS for r in near[dim]
                       if r["top1"] == dim)
    near_rate = near_correct / near_total if near_total else 0.0
    # L7 curated near set (separate; original 18 stay L6-comparable)
    cur_total = sum(len(v) for v in near_curated.values())
    cur_correct = sum(1 for dim in DIMENSIONS
                      for r in near_curated[dim] if r["top1"] == dim)
    cur_rate = cur_correct / cur_total if cur_total else 0.0

    passed = all_two_thirds and sep_visible
    lines.append("## VERDICT")
    lines.append("")
    lines.append(f"- In-domain agreement (min per pattern): {min_rate:.3f} "
                 f"({'PASS' if all_two_thirds else 'FAIL'} vs 0.667 bar)")
    for dim in DIMENSIONS:
        lines.append(f"  - {dim}: {in_rates[dim]:.3f}")
    lines.append(f"- Out-of-domain separation: in {in_aff:.6f} vs out "
                 f"{ood_aff:.6f}, delta {separation:.6f} "
                 f"({'visible' if sep_visible else 'NOT visible'})")
    lines.append(f"- Near-probe accuracy: {near_correct}/{near_total} "
                 f"({near_rate:.3f}) — stress-case discrimination")
    if cur_total:
        lines.append(f"- Near-probe accuracy (L7 curated set): "
                     f"{cur_correct}/{cur_total} ({cur_rate:.3f})")
    lines.append(f"- Amplification overall: {overall_amp:.6f}")
    lines.append("")
    lines.append(f"**Tier-0 sketch discrimination: "
                 f"{'PASS' if passed else 'FAIL'}** "
                 f"(bar: in-domain agreement >= 2/3 per pattern AND "
                 f"out-of-domain separation visible).")
    lines.append("")
    if not passed:
        lines.append("Curation actions (concrete, evidence-first):")
        for picked, c in miss_counter.most_common():
            lines.append(f"- Sharpen paraphrase lists for attractor pattern "
                         f"`{picked}`: {c} near-probe(s) land here; add "
                         f"contrast phrases that exclude the confused "
                         f"neighbour's vocabulary.")
        # name specific confused pairs
        for (exp, picked), c in sorted(wrong, key=lambda kv: -kv[1]):
            if c >= 2:
                lines.append(f"- Pair {exp} vs {picked}: {c} confusion(s); "
                             f"rewrite paraphrases on both sides so "
                             f"{exp} owns its distinctive verbs.")
        lines.append("- If a pattern fails 2/3 in-domain even without "
                     "near wording, its bank paraphrases share too much "
                     "char-3-gram overlap with a neighbour under Tier-0; "
                     "shorten/distinct-ify them before any Tier-1 work.")
    else:
        lines.append("No mandatory curation before Tier-1. Watch the "
                     "attractor patterns above in the near set; Tier-1 "
                     "(semantic) embeddings are expected to widen margins "
                     "on the near pairs listed, since Tier-0 is lexical "
                     "(char-3-gram) and cannot separate paraphrases that "
                     "share vocabulary across patterns.")
    lines.append("")
    lines.append("What Tier-1 is expected to fix regardless of verdict:")
    lines.append("- Near-probe confusions where wording overlap, not meaning, "
                 "drives the pick (Tier-0 has no semantics).")
    lines.append("- Out-of-domain spurious affinity: a semantic encoder "
                 "should push OOD top-1 affinity down and in-domain up, "
                 "widening the separation delta measured above.")
    lines.append("- Amplification noise: with semantic vectors, agent-side "
                 "first-person phrasing should separate from prompt-side "
                 "phrasing more cleanly than char-3-gram overlap allows.")
    lines.append("")

    body = "\n".join(lines)
    report_path = REPO_ROOT / "probe-report.md"
    report_path.write_text(body, encoding="utf-8")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    print(body)
    print(f"\n[probe] report: {report_path}")
    print(f"[probe] sha256(report body): {digest}")
    print(f"[probe] verdict: {'PASS' if passed else 'FAIL'} "
          f"(min in-domain rate {min_rate:.3f}, OOD delta {separation:.6f}, "
          f"near {near_correct}/{near_total}, amplification {overall_amp:.6f})")


if __name__ == "__main__":
    main()
