# L8 tier-1 pattern-quality probe report

- backend: tier-1-onnx (0029fce9c82365d8a2bf20e03a476e84785a872d8d423c8bac0fd0f350df88dc, model sha256 0029fce9c82365d8a2bf20e03a476e84785a872d8d423c8bac0fd0f350df88dc, onnxruntime 1.29.0)
- pattern_bank_version: 1
- patterns: directness, context_richness, exploration, analytical_depth, iteration, scope
- method: real Tier0SketchBackend encode; score_turn prompt_affinity ranking for no-reaction probes, agent_affinity ranking for reaction probes

## Per-probe detail (prompt-mode probes, no reaction)

### In-domain

| expected | text | top1 | top1_aff | runner_up | margin |
|---|---|---|---|---|---|
| directness | Give me the short answer, no preamble. | directness | 0.752134 | analytical_depth | 0.418727 |
| directness | Tell me the number, plainly. | directness | 0.612747 | iteration | 0.411083 |
| directness | Say it in a single plain sentence. | directness | 0.724532 | scope | 0.352522 |
| directness | No fluff — what is it? | directness | 0.544531 | scope | 0.150195 |
| directness | TL;DR, straight to the point. | directness | 0.609333 | context_richness | 0.272502 |
| directness | Skip the pleasantries and state the verdict. | directness | 0.657198 | iteration | 0.159503 |
| directness | Skip the intro, give the result. | directness | 0.718417 | scope | 0.195948 |
| directness | Just the answer, nothing extra. | directness | 0.700187 | scope | 0.249151 |
| directness | One line only, please. | directness | 0.565124 | scope | 0.163475 |
| directness | Straight up: what is it? | directness | 0.514539 | exploration | 0.233645 |
| context_richness | Here is the full background and prior attempts. | context_richness | 0.628159 | iteration | 0.165806 |
| context_richness | Context: we tried X, then Y, both failed. | analytical_depth | 0.551340 | context_richness | 0.027947 |
| context_richness | Given that we are offline, here is the config to work from. | context_richness | 0.712948 | scope | 0.373651 |
| context_richness | The docs say this; here is the exact error message. | context_richness | 0.517682 | directness | 0.230606 |
| context_richness | Constraints: offline only, must run on Windows. | context_richness | 0.499815 | scope | 0.281972 |
| context_richness | Before you act, here is the environment: version, OS, and setup. | context_richness | 0.671228 | scope | 0.333287 |
| context_richness | All the setup details are here: how it is wired and what was decided. | context_richness | 0.693513 | scope | 0.365001 |
| context_richness | Attached: the log, the config, and my repro steps. | context_richness | 0.498076 | iteration | 0.079912 |
| context_richness | Background included: what I tried and what failed. | context_richness | 0.568188 | analytical_depth | 0.100487 |
| context_richness | Full error trace pasted below, plus versions. | context_richness | 0.414676 | scope | 0.096419 |
| exploration | Brainstorm every option you can think of. | exploration | 0.700375 | iteration | 0.311352 |
| exploration | What else could we try? List alternatives. | exploration | 0.712117 | analytical_depth | 0.272604 |
| exploration | Think sideways — what are we missing? | exploration | 0.725300 | analytical_depth | 0.274934 |
| exploration | Explore creative angles before deciding. | exploration | 0.735334 | analytical_depth | 0.296639 |
| exploration | Give me ten ideas, even wild ones. | exploration | 0.621341 | directness | 0.315708 |
| exploration | What if we took a completely different approach? | exploration | 0.622462 | analytical_depth | 0.206521 |
| exploration | Cast a wide net and see what turns up. | exploration | 0.519713 | iteration | 0.176736 |
| exploration | Give me a few different directions to choose from. | exploration | 0.587721 | analytical_depth | 0.279878 |
| exploration | What other ways could we approach this? | exploration | 0.642213 | analytical_depth | 0.228887 |
| exploration | List some wild ideas before we settle. | exploration | 0.733502 | directness | 0.390834 |
| analytical_depth | Compare the pros and cons of each approach. | analytical_depth | 0.698310 | exploration | 0.187425 |
| analytical_depth | Analyze the trade-offs with numbers. | analytical_depth | 0.640574 | iteration | 0.256954 |
| analytical_depth | What are the root causes here? Go deeper. | analytical_depth | 0.466289 | scope | 0.191618 |
| analytical_depth | Which hypothesis fits best? Explain the logic, not just the label. | analytical_depth | 0.733673 | exploration | 0.294176 |
| analytical_depth | Break the problem into first principles. | analytical_depth | 0.683488 | exploration | 0.264256 |
| analytical_depth | Stress-test this logic against edge cases. | analytical_depth | 0.733408 | iteration | 0.360262 |
| analytical_depth | Do a proper post-mortem of the failure. | analytical_depth | 0.556845 | iteration | 0.195193 |
| analytical_depth | Rank these by evidence and show your working. | analytical_depth | 0.558341 | exploration | 0.182450 |
| analytical_depth | What is the root cause? Break it down. | analytical_depth | 0.530056 | scope | 0.202511 |
| analytical_depth | Compare costs and risks numerically. | analytical_depth | 0.579976 | exploration | 0.320717 |
| iteration | Revise that, try again with feedback. | iteration | 0.783974 | analytical_depth | 0.345777 |
| iteration | Iterate on the last version, keep the good parts. | iteration | 0.626903 | scope | 0.261463 |
| iteration | One more pass — tighten the wording. | iteration | 0.691288 | directness | 0.252953 |
| iteration | Refine this draft, then refine again. | iteration | 0.713112 | directness | 0.381587 |
| iteration | Take another swing at it. | iteration | 0.630632 | exploration | 0.309234 |
| iteration | Loop back and polish the result. | iteration | 0.643581 | analytical_depth | 0.230735 |
| iteration | Keep going, improve it step by step. | iteration | 0.689373 | exploration | 0.240778 |
| iteration | Take another pass and tighten it. | iteration | 0.660049 | analytical_depth | 0.276936 |
| iteration | Revise from the feedback, keep what works. | iteration | 0.674906 | analytical_depth | 0.252912 |
| iteration | Polish this draft once more. | iteration | 0.577316 | directness | 0.203442 |
| scope | Only the auth module — leave everything else alone. | scope | 0.679711 | directness | 0.235987 |
| scope | Just this one file; stop when it is fixed. | scope | 0.687793 | iteration | 0.334104 |
| scope | Keep the change inside this area, nothing outside. | scope | 0.711727 | directness | 0.374869 |
| scope | Out of scope: the rest of the system stays untouched. | scope | 0.644882 | context_richness | 0.266642 |
| scope | Bound the fix to this endpoint and call it done. | iteration | 0.612474 | scope | 0.035267 |
| scope | One function, one test — that is the boundary. | analytical_depth | 0.520346 | scope | 0.016252 |
| scope | Do not touch the surrounding code at all. | scope | 0.425944 | directness | 0.327802 |
| scope | Only the login module, nothing wider. | scope | 0.378981 | directness | 0.150472 |
| scope | One file, one fix, leave the rest. | scope | 0.617786 | iteration | 0.109032 |
| scope | Keep it tiny and local. | scope | 0.498522 | directness | 0.012465 |

### Near (stress: true intent vs pulled-toward)

| expected | pulls_toward | text | top1 | top1_aff | runner_up | margin | correct |
|---|---|---|---|---|---|---|---|
| directness | scope | Only the number, and keep it to this one file too. | scope | 0.456478 | directness | 0.086473 | no |
| directness | scope | Answer briefly and do not touch anything else. | directness | 0.551901 | scope | 0.114254 | yes |
| directness | scope | Short reply only, no big redesign talk. | directness | 0.540924 | exploration | 0.152947 | yes |
| context_richness | analytical_depth | Here are the logs; weigh what they imply step by step. | analytical_depth | 0.431022 | iteration | 0.005203 | no |
| context_richness | analytical_depth | Pasted the full trace and config for your analysis. | analytical_depth | 0.497558 | context_richness | 0.041510 | no |
| context_richness | analytical_depth | All the background, ranked by likelihood. | exploration | 0.375859 | analytical_depth | 0.005549 | no |
| exploration | analytical_depth | Brainstorm options and compare their trade-offs numerically. | exploration | 0.602109 | analytical_depth | 0.009637 | yes |
| exploration | analytical_depth | List every angle we have not weighed yet. | exploration | 0.404088 | analytical_depth | 0.097135 | yes |
| exploration | analytical_depth | Wild ideas first, then rank them by evidence. | exploration | 0.574779 | analytical_depth | 0.055202 | yes |
| analytical_depth | context_richness | Break down the logs we pasted into root causes. | analytical_depth | 0.472522 | iteration | 0.139353 | yes |
| analytical_depth | context_richness | Analyze the attached config and error output in depth. | context_richness | 0.426434 | analytical_depth | 0.033853 | no |
| analytical_depth | exploration | Decompose the background and compare hypotheses by numbers. | analytical_depth | 0.595130 | exploration | 0.203766 | yes |
| iteration | directness | Revise it once more, just the short version please. | iteration | 0.501018 | directness | 0.021629 | yes |
| iteration | directness | One more pass: make it brief and final. | iteration | 0.649734 | directness | 0.100565 | yes |
| iteration | context_richness | Polish step by step, keep every earlier good part. | iteration | 0.575030 | exploration | 0.216056 | yes |
| scope | directness | Just this module, and answer in one sentence. | directness | 0.575034 | scope | 0.027643 | no |
| scope | directness | Tiny fix only, bottom line, no extra areas touched. | scope | 0.554577 | directness | 0.100343 | yes |
| scope | iteration | Keep it narrow and iterate only on this file. | scope | 0.458458 | directness | 0.186965 | yes |

### Near probes — L7 curated (separate set)

| expected | pulls_toward | text | top1 | top1_aff | runner_up | margin | correct |
|---|---|---|---|---|---|---|---|
| directness | scope | No preamble: just the verdict for this module only. | directness | 0.591891 | scope | 0.156702 | yes |
| context_richness | analytical_depth | Here is the context, now tell me the underlying reason. | analytical_depth | 0.499582 | directness | 0.059347 | no |
| context_richness | analytical_depth | Setup, constraints, and history attached; derive what comes next. | analytical_depth | 0.469497 | context_richness | 0.005856 | no |
| analytical_depth | context_richness | Trace the mechanism from the pasted logs, step by step. | analytical_depth | 0.317048 | iteration | 0.039683 | yes |
| scope | directness | Only this part, then state the blunt result. | directness | 0.649554 | analytical_depth | 0.147629 | no |

### Out-of-domain

| text | top1 | top1_aff | runner_up | margin |
|---|---|---|---|---|
| What time is it in Berlin right now? | directness | 0.168481 | context_richness | 0.106160 |
| Remind me to buy milk on Friday. | directness | 0.263719 | iteration | 0.037456 |
| Can you review this pull request for style issues? | iteration | 0.322498 | context_richness | 0.090739 |
| The meeting moved to 3pm, see you there. | iteration | 0.200142 | context_richness | 0.003666 |
| What is the capital of France? | directness | 0.130835 | scope | 0.051542 |
| My train leaves at six, check the platform board. | scope | 0.260105 | context_richness | 0.005850 |
| Spellcheck this paragraph for me. | directness | 0.523090 | iteration | 0.120232 |
| Ping me when the build finishes. | iteration | 0.468888 | directness | 0.118908 |

## In-domain agreement (prompt-mode, top-1 = affinity max)

| pattern | probes | agreed | rate | mean_top1_aff | mean_margin |
|---|---|---|---|---|---|
| directness | 10 | 10 | 1.000 | 0.639874 | 0.260675 |
| context_richness | 10 | 9 | 0.900 | 0.575562 | 0.205509 |
| exploration | 10 | 10 | 1.000 | 0.660008 | 0.275409 |
| analytical_depth | 10 | 10 | 1.000 | 0.618096 | 0.245556 |
| iteration | 10 | 10 | 1.000 | 0.669113 | 0.275582 |
| scope | 10 | 8 | 0.800 | 0.577817 | 0.186289 |

## Margin comparison: in-domain vs near vs out-of-domain

| pattern | mean_margin_in | mean_margin_near | mean_margin_ood |
|---|---|---|---|
| directness | 0.260675 | 0.117892 | 0.066819 |
| context_richness | 0.205509 | 0.017421 | 0.066819 |
| exploration | 0.275409 | 0.053991 | 0.066819 |
| analytical_depth | 0.245556 | 0.125657 | 0.066819 |
| iteration | 0.275582 | 0.112750 | 0.066819 |
| scope | 0.186289 | 0.104983 | 0.066819 |
| (out-of-domain pooled, n=8) | 0.066819 | | |

## Near-probe error matrix (expected -> picked)

| expected | picked | count |
|---|---|---|
| analytical_depth | analytical_depth | 2 |
| analytical_depth | context_richness | 1 |
| context_richness | analytical_depth | 2 |
| context_richness | exploration | 1 |
| directness | directness | 2 |
| directness | scope | 1 |
| exploration | exploration | 3 |
| iteration | iteration | 3 |
| scope | directness | 1 |
| scope | scope | 2 |

Missed-to (picked instead of expected), most frequent first:
- analytical_depth: 2 near-probe(s) landed here (attractor pattern)
- context_richness: 1 near-probe(s) landed here (attractor pattern)
- exploration: 1 near-probe(s) landed here (attractor pattern)
- scope: 1 near-probe(s) landed here (attractor pattern)
- directness: 1 near-probe(s) landed here (attractor pattern)

## Out-of-domain affinity distribution vs in-domain

- mean in-domain top-1 affinity: 0.623412
- mean out-of-domain top-1 affinity (best of six, spurious): 0.292220
- separation (in - out): 0.331192
- mean in-domain margin: 0.241503
- mean out-of-domain margin: 0.066819

| ood text | top1 | top1_aff |
|---|---|---|
| What time is it in Berlin right now? | directness | 0.168481 |
| Remind me to buy milk on Friday. | directness | 0.263719 |
| Can you review this pull request for style issues? | iteration | 0.322498 |
| The meeting moved to 3pm, see you there. | iteration | 0.200142 |
| What is the capital of France? | directness | 0.130835 |
| My train leaves at six, check the platform board. | scope | 0.260105 |
| Spellcheck this paragraph for me. | directness | 0.523090 |
| Ping me when the build finishes. | iteration | 0.468888 |

## Amplification (reaction probes, agent vs own prompt)

| pattern | reaction probes | mean intended amplification | mean intended agent_aff | mean intended prompt_aff | agent_top1_rate |
|---|---|---|---|---|---|
| directness | 40 | 0.083404 | 0.723278 | 0.639874 | 1.000 |
| context_richness | 40 | 0.110027 | 0.682794 | 0.572768 | 1.000 |
| exploration | 40 | 0.007679 | 0.667687 | 0.660008 | 1.000 |
| analytical_depth | 40 | 0.149743 | 0.767839 | 0.618096 | 1.000 |
| iteration | 40 | 0.026727 | 0.695841 | 0.669113 | 1.000 |
| scope | 40 | 0.142266 | 0.714931 | 0.572665 | 1.000 |

Strongest agent-vs-prompt gap (mean amplification, desc):
- analytical_depth: 0.149743
- scope: 0.142266
- context_richness: 0.110027
- directness: 0.083404
- iteration: 0.026727
- exploration: 0.007679

Overall mean amplification: 0.086641 (positive)

## VERDICT

- In-domain agreement (min per pattern): 0.800 (PASS vs 0.667 bar)
  - directness: 1.000
  - context_richness: 0.900
  - exploration: 1.000
  - analytical_depth: 1.000
  - iteration: 1.000
  - scope: 0.800
- Out-of-domain separation: in 0.623412 vs out 0.292220, delta 0.331192 (visible)
- Near-probe accuracy: 12/18 (0.667) — stress-case discrimination
- Near-probe accuracy (L7 curated set): 2/5 (0.400)
- Amplification overall: 0.086641

**Tier-1 ONNX discrimination: PASS** (bar: in-domain agreement >= 2/3 per pattern AND out-of-domain separation visible).

No mandatory curation before Tier-1 sign-off. Watch the attractor patterns above in the near set and the Tier-0 vs Tier-1 comparison below: semantic embeddings should widen margins on the near pairs where Tier-0 (lexical, char-3-gram) could not separate paraphrases sharing vocabulary across patterns.

## Tier-0 vs Tier-1 comparison

| metric | tier-0 | tier-1 |
|---|---|---|
| in-domain agreement (min per pattern) | 0.700 | 0.800 |
| in-domain rate: directness | 0.700 | 1.000 |
| in-domain rate: context_richness | 0.800 | 0.900 |
| in-domain rate: exploration | 1.000 | 1.000 |
| in-domain rate: analytical_depth | 0.700 | 1.000 |
| in-domain rate: iteration | 1.000 | 1.000 |
| in-domain rate: scope | 0.900 | 0.800 |
| mean in-domain margin: directness | 0.119612 | 0.260675 |
| mean in-domain margin: context_richness | 0.136984 | 0.205509 |
| mean in-domain margin: exploration | 0.175631 | 0.275409 |
| mean in-domain margin: analytical_depth | 0.101962 | 0.245556 |
| mean in-domain margin: iteration | 0.104700 | 0.275582 |
| mean in-domain margin: scope | 0.123677 | 0.186289 |
| near-probe accuracy (18 probes) | 9/18 (0.500) | 12/18 (0.667) |
| OOD mean in-affinity | 0.545002 | 0.623412 |
| OOD mean out-affinity (spurious) | 0.403052 | 0.292220 |
| OOD separation delta (in - out) | 0.141950 | 0.331192 | (better) |
| mean amplification (overall) | 0.189690 (positive) | 0.086641 (positive) |

## VERDICT (Tier-1)

- Tier-1 discrimination (same bar as Tier-0): PASS (min in-domain rate 0.800, OOD delta 0.331192, near 12/18, amplification 0.086641).
- Confusion context_richness vs analytical_depth (near probes both directions): Tier-0 1 confusion(s), Tier-1 3 confusion(s) (not fixed (still needs curation)).
- Confusion scope vs directness (near probes both directions): Tier-0 1 confusion(s), Tier-1 2 confusion(s) (not fixed (still needs curation)).
- Other near-probe confusions still present in Tier-1 (expected -> picked): [('analytical_depth->context_richness', 1), ('context_richness->analytical_depth', 2), ('context_richness->exploration', 1), ('directness->scope', 1), ('scope->directness', 1)].
- Pairs still needing curation under Tier-1 (no curation was applied; bank is the shipped preset):
  - analytical_depth vs context_richness: 3 near-probe confusion(s) (both directions); rewrite paraphrases on both sides so each owns its distinctive verbs.
  - directness vs scope: 2 near-probe confusion(s) (both directions); rewrite paraphrases on both sides so each owns its distinctive verbs.
  - context_richness vs exploration: 1 near-probe confusion(s) (both directions); rewrite paraphrases on both sides so each owns its distinctive verbs.
- OOD separation: Tier-0 delta 0.141950 vs Tier-1 delta 0.331192 (better).


## Footer (reproducibility)

- onnxruntime version used: 1.29.0
- model sha256 (int8 graph): 0029fce9c82365d8a2bf20e03a476e84785a872d8d423c8bac0fd0f350df88dc
- determinism sha256 of report body above: 1faee379380a93ada5364b6632e7773037b942bf09a572f8ca2fa03a83d1e546