# L6 pattern-quality probe report

- backend: tier-0 (tier0-sketch-v1)
- pattern_bank_version: 1
- patterns: directness, context_richness, exploration, analytical_depth, iteration, scope
- method: real Tier0SketchBackend encode; score_turn prompt_affinity ranking for no-reaction probes, agent_affinity ranking for reaction probes

## Per-probe detail (prompt-mode probes, no reaction)

### In-domain

| expected | text | top1 | top1_aff | runner_up | margin |
|---|---|---|---|---|---|
| directness | Only the number, plainly. | directness | 0.677827 | scope | 0.303016 |
| directness | Just the answer, nothing extra. | directness | 0.659365 | scope | 0.000877 |
| directness | Only the verdict, bluntly, no preamble. | directness | 0.534402 | analytical_depth | 0.103529 |
| directness | The bottom line only, in plain words. | directness | 0.624561 | iteration | 0.239811 |
| directness | Short answer only - skip the pleasantries. | directness | 0.646537 | analytical_depth | 0.164577 |
| directness | One plain line, straight to the point. | directness | 0.669239 | scope | 0.178079 |
| directness | Answer briefly, straight up, no fluff. | directness | 0.604321 | scope | 0.285498 |
| directness | Just the answer, nothing extra. | directness | 0.659365 | scope | 0.000877 |
| directness | One line only, please. | directness | 0.451202 | scope | 0.167049 |
| directness | Straight up: what is it? | scope | 0.357087 | directness | 0.011657 |
| context_richness | Attached: the failing config, the stack trace, and the versions, all given as-is. | context_richness | 0.829423 | analytical_depth | 0.166654 |
| context_richness | Pasted below: the full trace and the logs we collected. | context_richness | 0.721264 | analytical_depth | 0.162836 |
| context_richness | Here are the error logs and the version numbers, pasted in full. | context_richness | 0.714338 | analytical_depth | 0.135474 |
| context_richness | Supplied material: docs, repro steps, and what was tried before. | context_richness | 0.556147 | exploration | 0.118070 |
| context_richness | Given up front: the environment, OS, constraints, and what was decided. | context_richness | 0.653315 | analytical_depth | 0.170442 |
| context_richness | Supplied: the dependency tree, the feature flags, and the failing case. | context_richness | 0.726521 | analytical_depth | 0.182692 |
| context_richness | Here is the data sample and the current schema, attached as-is. | context_richness | 0.662911 | analytical_depth | 0.088165 |
| context_richness | Attached: the log, the config, and my repro steps. | context_richness | 0.631786 | analytical_depth | 0.108987 |
| context_richness | Supplied: the versions in play and the exact failing case. | context_richness | 0.662476 | analytical_depth | 0.089677 |
| context_richness | Full error trace pasted below, plus versions. | context_richness | 0.420987 | iteration | 0.126956 |
| exploration | Brainstorm every option you can think of. | exploration | 0.443309 | directness | 0.070913 |
| exploration | What else could we try? List alternatives. | exploration | 0.644726 | scope | 0.322898 |
| exploration | Think sideways — what are we missing? | exploration | 0.610700 | scope | 0.193971 |
| exploration | Explore creative angles before deciding. | exploration | 0.479065 | iteration | 0.172091 |
| exploration | Give me ten ideas, even wild ones. | exploration | 0.485336 | context_richness | 0.128285 |
| exploration | What if we took a completely different approach? | exploration | 0.572641 | analytical_depth | 0.311495 |
| exploration | Cast a wide net and see what turns up. | exploration | 0.644270 | scope | 0.254186 |
| exploration | Give me a few different directions to choose from. | exploration | 0.386681 | context_richness | 0.088650 |
| exploration | What other ways could we approach this? | exploration | 0.447477 | scope | 0.067427 |
| exploration | List some wild ideas before we settle. | exploration | 0.458734 | context_richness | 0.201560 |
| analytical_depth | Why does this happen? Derive the mechanism from first principles. | analytical_depth | 0.663924 | iteration | 0.211080 |
| analytical_depth | Prove which hypothesis fits with evidence and numbers. | analytical_depth | 0.579760 | context_richness | 0.165547 |
| analytical_depth | Analyze the trade-offs numerically and compare pros and cons. | analytical_depth | 0.617082 | context_richness | 0.130226 |
| analytical_depth | Find the root cause: trace the mechanism and stress-test the logic. | analytical_depth | 0.761907 | context_richness | 0.150917 |
| analytical_depth | Break the failure down into first principles and do a post-mortem. | analytical_depth | 0.616050 | context_richness | 0.156651 |
| analytical_depth | Analyze the attached material in depth and explain the root cause. | analytical_depth | 0.675252 | context_richness | 0.124528 |
| analytical_depth | Weigh the evidence, rank the hypotheses by likelihood, and justify the verdict. | analytical_depth | 0.721683 | context_richness | 0.140557 |
| analytical_depth | Rank these by evidence and show your working. | analytical_depth | 0.522092 | context_richness | 0.084164 |
| analytical_depth | What is the root cause? Break it down. | analytical_depth | 0.489778 | context_richness | 0.066184 |
| analytical_depth | Compare costs and risks numerically. | analytical_depth | 0.358720 | context_richness | 0.050480 |
| iteration | Revise that, try again with feedback. | iteration | 0.542463 | directness | 0.206967 |
| iteration | Iterate on the last version, keep the good parts. | iteration | 0.646441 | context_richness | 0.089517 |
| iteration | One more pass — tighten the wording. | iteration | 0.603265 | context_richness | 0.152543 |
| iteration | Refine this draft, then refine again. | iteration | 0.600962 | scope | 0.145697 |
| iteration | Take another swing at it. | iteration | 0.452484 | scope | 0.091286 |
| iteration | Loop back and polish the result. | iteration | 0.493580 | analytical_depth | 0.049485 |
| iteration | Keep going, improve it step by step. | iteration | 0.520254 | scope | 0.103747 |
| iteration | Take another pass and tighten it. | iteration | 0.482807 | context_richness | 0.050861 |
| iteration | Revise from the feedback, keep what works. | iteration | 0.513608 | context_richness | 0.074332 |
| iteration | Polish this draft once more. | iteration | 0.433054 | scope | 0.070980 |
| scope | Just this module, nothing else. | scope | 0.774429 | iteration | 0.360408 |
| scope | Only this part - leave the rest alone. | scope | 0.639067 | iteration | 0.050078 |
| scope | Just this one file; stop when it is fixed. | scope | 0.612757 | iteration | 0.151074 |
| scope | Keep the change inside this area, nothing outside. | scope | 0.668486 | iteration | 0.136481 |
| scope | Out of scope: everything else stays untouched. | scope | 0.482396 | iteration | 0.134957 |
| scope | Only this endpoint, nothing wider. | scope | 0.683428 | directness | 0.272273 |
| scope | Just this function and its test, nothing else. | scope | 0.738724 | exploration | 0.224778 |
| scope | Only the login module, nothing wider. | scope | 0.595275 | directness | 0.142392 |
| scope | One file, one fix, leave the rest. | iteration | 0.358529 | scope | 0.000288 |
| scope | Keep it tiny and local. | context_richness | 0.377676 | iteration | 0.009268 |

### Near (stress: true intent vs pulled-toward)

| expected | pulls_toward | text | top1 | top1_aff | runner_up | margin | correct |
|---|---|---|---|---|---|---|---|
| directness | scope | Only the number, and keep it to this one file too. | directness | 0.577285 | analytical_depth | 0.037537 | yes |
| directness | scope | Answer briefly and do not touch anything else. | directness | 0.515223 | scope | 0.023758 | yes |
| directness | scope | Short reply only, no big redesign talk. | directness | 0.389747 | context_richness | 0.102453 | yes |
| context_richness | analytical_depth | Here are the logs; weigh what they imply step by step. | iteration | 0.534638 | context_richness | 0.063932 | no |
| context_richness | analytical_depth | Pasted the full trace and config for your analysis. | context_richness | 0.590714 | analytical_depth | 0.076279 | yes |
| context_richness | analytical_depth | All the background, ranked by likelihood. | analytical_depth | 0.477796 | context_richness | 0.070776 | no |
| exploration | analytical_depth | Brainstorm options and compare their trade-offs numerically. | analytical_depth | 0.479060 | context_richness | 0.020134 | no |
| exploration | analytical_depth | List every angle we have not weighed yet. | exploration | 0.357421 | directness | 0.015333 | yes |
| exploration | analytical_depth | Wild ideas first, then rank them by evidence. | analytical_depth | 0.468262 | context_richness | 0.090879 | no |
| analytical_depth | context_richness | Break down the logs we pasted into root causes. | analytical_depth | 0.480164 | context_richness | 0.070430 | yes |
| analytical_depth | context_richness | Analyze the attached config and error output in depth. | analytical_depth | 0.624997 | context_richness | 0.027551 | yes |
| analytical_depth | exploration | Decompose the background and compare hypotheses by numbers. | analytical_depth | 0.611706 | context_richness | 0.121146 | yes |
| iteration | directness | Revise it once more, just the short version please. | context_richness | 0.424754 | analytical_depth | 0.029574 | no |
| iteration | directness | One more pass: make it brief and final. | iteration | 0.382188 | analytical_depth | 0.018485 | yes |
| iteration | context_richness | Polish step by step, keep every earlier good part. | iteration | 0.495524 | scope | 0.151268 | yes |
| scope | directness | Just this module, and answer in one sentence. | scope | 0.490938 | analytical_depth | 0.046840 | yes |
| scope | directness | Tiny fix only, bottom line, no extra areas touched. | directness | 0.419170 | scope | 0.076770 | no |
| scope | iteration | Keep it narrow and iterate only on this file. | scope | 0.530432 | context_richness | 0.016872 | yes |

### Near probes — L7 curated (separate set)

| expected | pulls_toward | text | top1 | top1_aff | runner_up | margin | correct |
|---|---|---|---|---|---|---|---|
| directness | scope | No preamble: just the verdict for this module only. | analytical_depth | 0.533043 | context_richness | 0.045257 | no |
| context_richness | analytical_depth | Here is the context, now tell me the underlying reason. | iteration | 0.536340 | analytical_depth | 0.058334 | no |
| context_richness | analytical_depth | Setup, constraints, and history attached; derive what comes next. | exploration | 0.425154 | analytical_depth | 0.005902 | no |
| analytical_depth | context_richness | Trace the mechanism from the pasted logs, step by step. | iteration | 0.568184 | context_richness | 0.032536 | no |
| scope | directness | Only this part, then state the blunt result. | iteration | 0.563417 | analytical_depth | 0.020229 | no |

### Near probes — L9 curation-2 (separate set)

| expected | pulls_toward | text | top1 | top1_aff | runner_up | margin | correct |
|---|---|---|---|---|---|---|---|
| directness | scope | No preamble: only the number, and leave the rest of the system alone. | analytical_depth | 0.605920 | directness | 0.007844 | no |
| context_richness | analytical_depth | Supplied up front: background and setup; take it as given. | context_richness | 0.514824 | analytical_depth | 0.078942 | yes |
| context_richness | analytical_depth | Pasted: the full error trace, the config, and what we already tried. | context_richness | 0.704620 | analytical_depth | 0.160111 | yes |
| context_richness | scope | Here is the failing module's log; stay inside it. | context_richness | 0.521229 | iteration | 0.082534 | yes |
| analytical_depth | context_richness | Given the supplied logs, derive the mechanism and prove the cause. | analytical_depth | 0.697462 | context_richness | 0.049179 | yes |
| analytical_depth | context_richness | From the pasted stack trace, find the root cause. | analytical_depth | 0.669402 | context_richness | 0.080861 | yes |
| scope | directness | Just this module only, bluntly: what is the verdict? | context_richness | 0.548637 | scope | 0.007640 | no |
| scope | context_richness | Just this one file — the pasted config below; leave the rest. | iteration | 0.558217 | context_richness | 0.004767 | no |

### Out-of-domain

| text | top1 | top1_aff | runner_up | margin |
|---|---|---|---|---|
| What time is it in Berlin right now? | scope | 0.402604 | directness | 0.023635 |
| Remind me to buy milk on Friday. | context_richness | 0.295349 | exploration | 0.035470 |
| Can you review this pull request for style issues? | iteration | 0.428322 | scope | 0.034531 |
| The meeting moved to 3pm, see you there. | scope | 0.422747 | iteration | 0.015670 |
| What is the capital of France? | context_richness | 0.455854 | analytical_depth | 0.004228 |
| My train leaves at six, check the platform board. | directness | 0.483472 | context_richness | 0.110951 |
| Spellcheck this paragraph for me. | scope | 0.366193 | iteration | 0.077687 |
| Ping me when the build finishes. | analytical_depth | 0.377435 | context_richness | 0.016312 |

## In-domain agreement (prompt-mode, top-1 = affinity max)

| pattern | probes | agreed | rate | mean_top1_aff | mean_margin |
|---|---|---|---|---|---|
| directness | 10 | 9 | 0.900 | 0.588391 | 0.145497 |
| context_richness | 10 | 10 | 1.000 | 0.657917 | 0.134995 |
| exploration | 10 | 10 | 1.000 | 0.517294 | 0.181148 |
| analytical_depth | 10 | 10 | 1.000 | 0.600625 | 0.128033 |
| iteration | 10 | 10 | 1.000 | 0.528892 | 0.103541 |
| scope | 10 | 8 | 0.800 | 0.593077 | 0.148200 |

## Margin comparison: in-domain vs near vs out-of-domain

| pattern | mean_margin_in | mean_margin_near | mean_margin_ood |
|---|---|---|---|
| directness | 0.145497 | 0.054583 | 0.039810 |
| context_richness | 0.134995 | 0.070329 | 0.039810 |
| exploration | 0.181148 | 0.042115 | 0.039810 |
| analytical_depth | 0.128033 | 0.073042 | 0.039810 |
| iteration | 0.103541 | 0.066442 | 0.039810 |
| scope | 0.148200 | 0.046828 | 0.039810 |
| (out-of-domain pooled, n=8) | 0.039810 | | |

## Near-probe error matrix (expected -> picked)

| expected | picked | count |
|---|---|---|
| analytical_depth | analytical_depth | 3 |
| context_richness | analytical_depth | 1 |
| context_richness | context_richness | 1 |
| context_richness | iteration | 1 |
| directness | directness | 3 |
| exploration | analytical_depth | 2 |
| exploration | exploration | 1 |
| iteration | context_richness | 1 |
| iteration | iteration | 2 |
| scope | directness | 1 |
| scope | scope | 2 |

Missed-to (picked instead of expected), most frequent first:
- analytical_depth: 3 near-probe(s) landed here (attractor pattern)
- iteration: 1 near-probe(s) landed here (attractor pattern)
- context_richness: 1 near-probe(s) landed here (attractor pattern)
- directness: 1 near-probe(s) landed here (attractor pattern)

## Out-of-domain affinity distribution vs in-domain

- mean in-domain top-1 affinity: 0.581032
- mean out-of-domain top-1 affinity (best of six, spurious): 0.403997
- separation (in - out): 0.177035
- mean in-domain margin: 0.140236
- mean out-of-domain margin: 0.039810

| ood text | top1 | top1_aff |
|---|---|---|
| What time is it in Berlin right now? | scope | 0.402604 |
| Remind me to buy milk on Friday. | context_richness | 0.295349 |
| Can you review this pull request for style issues? | iteration | 0.428322 |
| The meeting moved to 3pm, see you there. | scope | 0.422747 |
| What is the capital of France? | context_richness | 0.455854 |
| My train leaves at six, check the platform board. | directness | 0.483472 |
| Spellcheck this paragraph for me. | scope | 0.366193 |
| Ping me when the build finishes. | analytical_depth | 0.377435 |

## Amplification (reaction probes, agent vs own prompt)

| pattern | reaction probes | mean intended amplification | mean intended agent_aff | mean intended prompt_aff | agent_top1_rate |
|---|---|---|---|---|---|
| directness | 40 | 0.123572 | 0.710797 | 0.587225 | 1.000 |
| context_richness | 40 | 0.084194 | 0.742111 | 0.657917 | 1.000 |
| exploration | 40 | 0.192942 | 0.710236 | 0.517294 | 1.000 |
| analytical_depth | 40 | 0.135699 | 0.736323 | 0.600625 | 1.000 |
| iteration | 40 | 0.187135 | 0.716027 | 0.528892 | 1.000 |
| scope | 40 | 0.171022 | 0.752322 | 0.581300 | 1.000 |

Strongest agent-vs-prompt gap (mean amplification, desc):
- exploration: 0.192942
- iteration: 0.187135
- scope: 0.171022
- analytical_depth: 0.135699
- directness: 0.123572
- context_richness: 0.084194

Overall mean amplification: 0.149094 (positive)

## VERDICT

- In-domain agreement (min per pattern): 0.800 (PASS vs 0.667 bar)
  - directness: 0.900
  - context_richness: 1.000
  - exploration: 1.000
  - analytical_depth: 1.000
  - iteration: 1.000
  - scope: 0.800
- Out-of-domain separation (INFORMATIONAL — OOD-Δ gate retired, replaced by the drift-recall monitor): in 0.581032 vs out 0.403997, delta 0.177035 (visible)
- Near-probe accuracy: 12/18 (0.667) — stress-case discrimination
- Near-probe accuracy (L7 curated set): 0/5 (0.000)
- Near-probe accuracy (L9 curation-2 set): 5/8 (0.625)
- Amplification overall: 0.149094

**Tier-0 sketch discrimination: PASS** (bar: in-domain agreement >= 2/3 per pattern; OOD-Δ separation is informational — the gate was retired in favour of the drift-recall monitor).

No mandatory curation before Tier-1. Watch the attractor patterns above in the near set; Tier-1 (semantic) embeddings are expected to widen margins on the near pairs listed, since Tier-0 is lexical (char-3-gram) and cannot separate paraphrases that share vocabulary across patterns.

What Tier-1 is expected to fix regardless of verdict:
- Near-probe confusions where wording overlap, not meaning, drives the pick (Tier-0 has no semantics).
- Out-of-domain spurious affinity: a semantic encoder should push OOD top-1 affinity down and in-domain up, widening the separation delta measured above.
- Amplification noise: with semantic vectors, agent-side first-person phrasing should separate from prompt-side phrasing more cleanly than char-3-gram overlap allows.
