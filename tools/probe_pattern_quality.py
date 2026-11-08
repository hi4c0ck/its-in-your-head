#!/usr/bin/env python3
"""L6 pattern-quality probe: does the shipped preset bank + Tier-0 sketch
backend actually discriminate the six patterns?

Deterministic, stdlib + repo imports only. Zero network, no LLM, no
randomness, no timestamps. Re-runnable: same input -> identical report
body (sha256 printed at the end).

Run from the repo root:  python tools/probe_pattern_quality.py [--tier tier0|tier1]
Default (tier0) writes probe-report.md at the repo root of this clone;
--tier tier1 writes probe-report-tier1.md (same layout plus a Tier-0 vs
Tier-1 comparison table and a Tier-1 verdict) using the repo's ONNX
Tier-1 backend (worker subprocess, model dir from --model-dir or the
sibling .modeldir dir), with model_version pinned to the int8 graph's
sha256 so the bank cache key separates from Tier-0 by design.
"""
from __future__ import annotations

import argparse
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
from perception.encoder import OnnxMiniLMBackend  # noqa: E402

# Tier-1 pinned artifact (onnx/manifest.json int8 graph sha256). The probe's
# Tier-1 backend uses this as model_version so the bank cache key separates
# from Tier-0 by design (Tier-0 key is TIER0_MODEL_VERSION).
ONNX_MODEL_SHA256 = "0029fce9c82365d8a2bf20e03a476e84785a872d8d423c8bac0fd0f350df88dc"

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
        "Supplied: the versions in play and the exact failing case.",
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

# L9 curation round (tier-1-aware): fresh near probes for the two remaining
# confusion pairs. Scored/reported SEPARATELY (original 18 + L7 5 pristine).
# CUR3: extended with CR-vs-AD and CR-vs-SCO discrimination probes shaped
# from the live margin distribution (median ~0.023 = noise; strong-runner
# cases where supply-vocabulary wording pulls toward the neighbour but the
# true intent stays with the key pattern).
NEAR_CURATED2: dict[str, list[tuple[str, str]]] = {
    "context_richness": [
        ("Supplied up front: background and setup; take it as given.",
         "analytical_depth"),
        ("Pasted: the full error trace, the config, and what we already tried.",
         "analytical_depth"),
        ("Here is the failing module's log; stay inside it.",
         "scope"),
    ],
    "analytical_depth": [
        ("Given the supplied logs, derive the mechanism and prove the cause.",
         "context_richness"),
        ("From the pasted stack trace, find the root cause.",
         "context_richness"),
    ],
    "scope": [
        ("Just this module only, bluntly: what is the verdict?",
         "directness"),
        ("Just this one file — the pasted config below; leave the rest.",
         "context_richness"),
    ],
    "directness": [
        ("No preamble: only the number, and leave the rest of the system alone.",
         "scope"),
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
# Tier-1 backend (worker subprocess; guarded for test-interpreter runtime)
# --------------------------------------------------------------------------


class _Tier1CheckedBackend:
    """Wraps OnnxMiniLMBackend: refuses any silent Tier-0 fallback.

    The repo backend degrades to Tier-0 on any worker failure (by design in
    production). For a quality probe that would fabricate Tier-1 numbers —
    so this wrapper raises instead, and pins model_version to the ONNX
    sha256. A direct-construction backend with a client never returns Tier-0
    text through this wrapper: last_failure is always checked.
    """

    name = "tier-1-onnx"

    def __init__(self, backend, client):
        self._backend = backend
        self.client = client

    @property
    def model_version(self) -> str:
        return ONNX_MODEL_SHA256

    @property
    def dim(self) -> int:
        return 384

    def encode(self, texts: list[str]) -> list[list[float]]:
        out = self._backend.encode(list(texts))
        failure = self._backend.last_failure
        if failure is not None:
            raise RuntimeError(f"tier-1 encode fell back: {failure}")
        if out and len(out[0]) != 384:
            raise RuntimeError(
                f"tier-1 encode dim != 384: {len(out[0])}")
        return out


def default_model_dir() -> Path:
    return REPO_ROOT.parent / (REPO_ROOT.name + ".modeldir")


def make_tier1_backend(model_dir: Path | None = None) -> tuple:
    """Construct the checked Tier-1 backend via the repo worker client.

    Returns (backend, ort_version). Raises on ANY failure (no fabrication).
    """
    import onnxruntime as ort  # optional dependency; tier-1 only

    from perception.worker_client import OnnxWorkerClient

    mdir = Path(model_dir) if model_dir else default_model_dir()
    client = OnnxWorkerClient(
        model_dir=mdir, interpreter=sys.executable, timeout_s=120.0)
    backend = OnnxMiniLMBackend(client=client, model_version=ONNX_MODEL_SHA256)
    ping = client.ping()
    if not ping.ok:
        raise RuntimeError(f"tier-1 worker ping failed: {ping.failure}")
    return _Tier1CheckedBackend(backend, client), str(ort.__version__)


def quick_stats(backend) -> dict:
    """Compact per-backend stats for the Tier-0 vs Tier-1 comparison.

    Reuses the identical probe sets and scoring helpers as the main run.
    """
    bank = pat.build_pattern_bank(backend, backend.model_version)
    enc = lambda texts: backend.encode(list(texts))  # noqa: E731
    in_rates, in_margins = {}, {}
    for dim in DIMENSIONS:
        texts = list(ANCHORS[dim]["positive"]) + list(FRESH_IN_DOMAIN[dim])
        rows = [score_prompt_only(v, bank) for v in enc(texts)]
        in_rates[dim] = (
            sum(1 for r in rows if r["top1"] == dim) / len(rows))
        in_margins[dim] = mean(
            [r["margin"] for r in rows if r["margin"] is not None])
    near_total = near_correct = 0
    pair_err: Counter[tuple[str, str]] = Counter()
    for dim in DIMENSIONS:
        texts = [t for t, _ in NEAR[dim]]
        for (_text, _pull), vec, in zip(NEAR[dim], enc(texts)):
            s = score_prompt_only(vec, bank)
            near_total += 1
            near_correct += 1 if s["top1"] == dim else 0
            if s["top1"] != dim:
                pair_err[(dim, s["top1"])] += 1
    ood_rows = [score_prompt_only(v, bank) for v in enc(OUT_OF_DOMAIN)]
    in_aff = mean([score_prompt_only(v, bank)["top1_aff"]
                   for dim in DIMENSIONS
                   for v in enc(list(ANCHORS[dim]["positive"])
                                + list(FRESH_IN_DOMAIN[dim]))])
    ood_aff = mean([r["top1_aff"] for r in ood_rows])
    amps = []
    for dim in DIMENSIONS:
        entry = next(e for e in pat.PATTERN_BANK if e["name"] == dim)
        prompt_texts = list(ANCHORS[dim]["positive"]) + list(
            FRESH_IN_DOMAIN[dim])
        agent_texts = list(entry["agent_paraphrases"])
        for pvec in enc(prompt_texts):
            for avec in enc(agent_texts):
                res = pat.score_turn(pvec, avec, bank)
                intended = next(p for p in res["patterns"]
                                if p["name"] == dim)
                if intended["amplification"] is not None:
                    amps.append(intended["amplification"])
    return {
        "in_rates": in_rates,
        "in_margins": in_margins,
        "near_correct": near_correct,
        "near_total": near_total,
        "near_rate": near_correct / near_total if near_total else 0.0,
        "in_aff": in_aff,
        "ood_aff": ood_aff,
        "ood_delta": in_aff - ood_aff,
        "amplification": mean(amps),
        "pair_err": pair_err,
    }


# --------------------------------------------------------------------------
# Probe run
# --------------------------------------------------------------------------


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tier", choices=["tier0", "tier1"], default="tier0")
    ap.add_argument("--model-dir", default=None)
    args = ap.parse_args(argv)
    tier = args.tier
    tier1_client = None
    ort_version: str | None = None
    if tier == "tier0":
        backend = Tier0SketchBackend()
        assert backend.model_version == TIER0_MODEL_VERSION
    else:
        try:
            backend, ort_version = make_tier1_backend(args.model_dir)
            tier1_client = backend.client
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            lines = ["# L8 tier-1 pattern-quality probe report", "",
                     f"- backend: tier-1-onnx (FAILED TO LOAD)",
                     f"- model sha256 (int8 graph): {ONNX_MODEL_SHA256}",
                     "", "## BLOCKER", "",
                     "Tier-1 model could not be loaded in any available "
                     "runtime; no tier-1 numbers were produced (no "
                     "fabricated vectors).",
                     "", f"Error: `{err}`", ""]
            body = "\n".join(lines)
            report_path = REPO_ROOT / "probe-report-tier1.md"
            report_path.write_text(body, encoding="utf-8")
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            print(body)
            print(f"\n[probe] report: {report_path}")
            print(f"[probe] sha256(report body): {digest}")
            print(f"[probe] BLOCKED: {err}")
            return
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

    # ---- NEAR (L9 curated-2): separate set, keeps original 18 + L7 5 pristine ----
    near_curated2: dict[str, list[dict]] = {}
    for dim in DIMENSIONS:
        rows = []
        for (text, pull) in NEAR_CURATED2.get(dim, []):
            s = score_prompt_only(enc([text])[0], bank)
            s.update({"text": text, "expected": dim, "pulls_toward": pull})
            rows.append(s)
        near_curated2[dim] = rows

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
    lines.append("# L6 pattern-quality probe report" if tier == "tier0"
                   else "# L8 tier-1 pattern-quality probe report")
    lines.append("")
    if tier == "tier0":
        lines.append(f"- backend: tier-0 ({backend.model_version})")
    else:
        lines.append(f"- backend: tier-1-onnx ({backend.model_version}, "
                     f"model sha256 {ONNX_MODEL_SHA256}, "
                     f"onnxruntime {ort_version})")
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
    lines.append("### Near probes — L9 curation-2 (separate set)")
    lines.append("")
    lines.append("| expected | pulls_toward | text | top1 | top1_aff | "
                 "runner_up | margin | correct |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for dim in DIMENSIONS:
        for r in near_curated2[dim]:
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
    # L9 curated-2 near set (separate; original 18 + L7 5 stay comparable)
    cur2_total = sum(len(v) for v in near_curated2.values())
    cur2_correct = sum(1 for dim in DIMENSIONS
                       for r in near_curated2[dim] if r["top1"] == dim)
    cur2_rate = cur2_correct / cur2_total if cur2_total else 0.0

    # L-QA: the OOD-Δ separation floor is RETIRED as a gate — the rolling
    # reference's drift-recall monitor (tools/probe_dynamic_monitors.py) is the
    # new out-of-territory signal. The OOD separation below is kept
    # informational only; in-domain agreement is the discrimination gate.
    passed = all_two_thirds
    lines.append("## VERDICT")
    lines.append("")
    lines.append(f"- In-domain agreement (min per pattern): {min_rate:.3f} "
                 f"({'PASS' if all_two_thirds else 'FAIL'} vs 0.667 bar)")
    for dim in DIMENSIONS:
        lines.append(f"  - {dim}: {in_rates[dim]:.3f}")
    lines.append(f"- Out-of-domain separation (INFORMATIONAL — OOD-Δ gate "
                 f"retired, replaced by the drift-recall monitor): in "
                 f"{in_aff:.6f} vs out {ood_aff:.6f}, delta "
                 f"{separation:.6f} ({'visible' if sep_visible else 'NOT visible'})")
    lines.append(f"- Near-probe accuracy: {near_correct}/{near_total} "
                 f"({near_rate:.3f}) — stress-case discrimination")
    if cur_total:
        lines.append(f"- Near-probe accuracy (L7 curated set): "
                     f"{cur_correct}/{cur_total} ({cur_rate:.3f})")
    if cur2_total:
        lines.append(f"- Near-probe accuracy (L9 curation-2 set): "
                     f"{cur2_correct}/{cur2_total} ({cur2_rate:.3f})")
    lines.append(f"- Amplification overall: {overall_amp:.6f}")
    lines.append("")
    verdict_name = ("Tier-0 sketch discrimination" if tier == "tier0"
                    else "Tier-1 ONNX discrimination")
    lines.append(f"**{verdict_name}: "
                 f"{'PASS' if passed else 'FAIL'}** "
                 f"(bar: in-domain agreement >= 2/3 per pattern; OOD-Δ "
                 f"separation is informational — the gate was retired in "
                 f"favour of the drift-recall monitor).")
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
        if tier == "tier0":
            lines.append("- If a pattern fails 2/3 in-domain even without "
                         "near wording, its bank paraphrases share too much "
                         "char-3-gram overlap with a neighbour under Tier-0; "
                         "shorten/distinct-ify them before any Tier-1 work.")
        else:
            lines.append("- If a pattern fails 2/3 in-domain even without "
                         "near wording, its bank paraphrases share too much "
                         "semantic overlap with a neighbour; sharpen them.")
    elif tier == "tier0":
        lines.append("No mandatory curation before Tier-1. Watch the "
                     "attractor patterns above in the near set; Tier-1 "
                     "(semantic) embeddings are expected to widen margins "
                     "on the near pairs listed, since Tier-0 is lexical "
                     "(char-3-gram) and cannot separate paraphrases that "
                     "share vocabulary across patterns.")
    else:
        lines.append("No mandatory curation before Tier-1 sign-off. Watch "
                     "the attractor patterns above in the near set and the "
                     "Tier-0 vs Tier-1 comparison below: semantic "
                     "embeddings should widen margins on the near pairs "
                     "where Tier-0 (lexical, char-3-gram) could not "
                     "separate paraphrases sharing vocabulary across "
                     "patterns.")
    lines.append("")
    if tier == "tier0":
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

    if tier == "tier1":
        # ---- Tier-0 vs Tier-1 comparison (identical probe sets) --------
        t0 = quick_stats(Tier0SketchBackend())
        # recompute tier-1 stats through the checked backend for the table
        # (in-process re-encode; identical inputs, deterministic results)
        t1 = quick_stats(backend)
        lines.append("## Tier-0 vs Tier-1 comparison")
        lines.append("")
        lines.append("| metric | tier-0 | tier-1 |")
        lines.append("|---|---|---|")
        lines.append(f"| in-domain agreement (min per pattern) | "
                     f"{min(t0['in_rates'].values()):.3f} | "
                     f"{min(t1['in_rates'].values()):.3f} |")
        for dim in DIMENSIONS:
            lines.append(f"| in-domain rate: {dim} | {t0['in_rates'][dim]:.3f} | "
                         f"{t1['in_rates'][dim]:.3f} |")
        for dim in DIMENSIONS:
            lines.append(f"| mean in-domain margin: {dim} | "
                         f"{t0['in_margins'][dim]:.6f} | "
                         f"{t1['in_margins'][dim]:.6f} |")
        lines.append(f"| near-probe accuracy (18 probes) | "
                     f"{t0['near_correct']}/{t0['near_total']} "
                     f"({t0['near_rate']:.3f}) | "
                     f"{t1['near_correct']}/{t1['near_total']} "
                     f"({t1['near_rate']:.3f}) |")
        lines.append(f"| OOD mean in-affinity | {t0['in_aff']:.6f} | "
                     f"{t1['in_aff']:.6f} |")
        lines.append(f"| OOD mean out-affinity (spurious) | "
                     f"{t0['ood_aff']:.6f} | {t1['ood_aff']:.6f} |")
        lines.append(f"| OOD separation delta (in - out) | "
                     f"{t0['ood_delta']:.6f} | {t1['ood_delta']:.6f} | "
                     f"({'better' if t1['ood_delta'] > t0['ood_delta'] else 'worse'}) |")
        lines.append(f"| mean amplification (overall) | "
                     f"{t0['amplification']:.6f} "
                     f"({'positive' if t0['amplification'] > 0 else 'non-positive'}) | "
                     f"{t1['amplification']:.6f} "
                     f"({'positive' if t1['amplification'] > 0 else 'non-positive'}) |")
        lines.append("")

        # ---- Tier-1 verdict: confusion pairs + curation advice -------
        def pair_count(stats, a, b):
            return (stats["pair_err"].get((a, b), 0)
                    + stats["pair_err"].get((b, a), 0))

        t0_cr_ad = pair_count(t0, "context_richness", "analytical_depth")
        t1_cr_ad = pair_count(t1, "context_richness", "analytical_depth")
        t0_sc_di = pair_count(t0, "scope", "directness")
        t1_sc_di = pair_count(t1, "scope", "directness")
        lines.append("## VERDICT (Tier-1)")
        lines.append("")
        lines.append(f"- Tier-1 discrimination (same bar as Tier-0): "
                     f"{'PASS' if passed else 'FAIL'} "
                     f"(min in-domain rate {min_rate:.3f}, OOD delta "
                     f"{separation:.6f}, near {near_correct}/{near_total}, "
                     f"amplification {overall_amp:.6f}).")
        lines.append(f"- Confusion context_richness vs analytical_depth "
                     f"(near probes both directions): Tier-0 {t0_cr_ad} "
                     f"confusion(s), Tier-1 {t1_cr_ad} confusion(s) "
                     f"({'FIXED' if t1_cr_ad == 0 and t0_cr_ad > 0 else 'not fixed (still needs curation)' if t1_cr_ad > 0 else 'clear on both'}).")
        lines.append(f"- Confusion scope vs directness (near probes both "
                     f"directions): Tier-0 {t0_sc_di} confusion(s), Tier-1 "
                     f"{t1_sc_di} confusion(s) "
                     f"({'FIXED' if t1_sc_di == 0 and t0_sc_di > 0 else 'not fixed (still needs curation)' if t1_sc_di > 0 else 'clear on both'}).")
        lines.append(f"- Other near-probe confusions still present in "
                     f"Tier-1 (expected -> picked): "
                     f"{[(f'{a}->{b}', c) for (a, b), c in sorted(t1['pair_err'].items()) if c] or 'none (18/18 correct)'}.")
        # curation list: undirected pairs with >=1 remaining Tier-1 confusion
        undirected: Counter[tuple[str, str]] = Counter()
        for (a, b), c in t1["pair_err"].items():
            undirected[tuple(sorted((a, b)))] += c
        remaining = sorted(undirected.items(), key=lambda kv: -kv[1])
        if remaining:
            lines.append("- Pairs still needing curation under Tier-1 "
                         "(no curation was applied; bank is the shipped preset):")
            for (a, b), c in remaining:
                lines.append(f"  - {a} vs {b}: {c} near-probe confusion(s) "
                             f"(both directions); rewrite paraphrases on both "
                             f"sides so each owns its distinctive verbs.")
        else:
            lines.append("- No pair needs curation on the near set under Tier-1 "
                         "(all 18 near probes correct).")
        lines.append(f"- OOD separation: Tier-0 delta {t0['ood_delta']:.6f} vs "
                     f"Tier-1 delta {t1['ood_delta']:.6f} "
                     f"({'better' if t1['ood_delta'] > t0['ood_delta'] else 'worse'}).")
        lines.append("")

    body = "\n".join(lines)
    report_path = (REPO_ROOT / "probe-report.md" if tier == "tier0"
                   else REPO_ROOT / "probe-report-tier1.md")
    report_path.write_text(body, encoding="utf-8")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if tier == "tier0":
        body_final = body
        report_path.write_text(body_final, encoding="utf-8")
        print(body_final)
        print(f"\n[probe] report: {report_path}")
        print(f"[probe] sha256(report body): {digest}")
    else:
        # Footer is appended AFTER the digest pass so the digest stays meaningful
        # and deterministic; the footer records the digest of the body above it.
        footer = ["", "## Footer (reproducibility)", "",
                  f"- onnxruntime version used: {ort_version}",
                  f"- model sha256 (int8 graph): {ONNX_MODEL_SHA256}",
                  f"- determinism sha256 of report body above: {digest}"]
        body_final = body + "\n" + "\n".join(footer)
        report_path.write_text(body_final, encoding="utf-8")
        digest_final = hashlib.sha256(body_final.encode("utf-8")).hexdigest()
        print(body_final)
        print(f"\n[probe] report: {report_path}")
        print(f"[probe] sha256(report body): {digest}")
        print(f"[probe] sha256(full report with footer): {digest_final}")
    if tier1_client is not None:
        try:
            tier1_client.shutdown()
        except Exception:
            pass
    print(f"[probe] verdict: {'PASS' if passed else 'FAIL'} "
          f"(min in-domain rate {min_rate:.3f}, OOD delta {separation:.6f}, "
          f"near {near_correct}/{near_total}, amplification {overall_amp:.6f})")


if __name__ == "__main__":
    main()
