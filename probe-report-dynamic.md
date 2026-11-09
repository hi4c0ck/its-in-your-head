# L-QA dynamic-territory monitor report

- backend: tier-0 (tier0-sketch-v1)
- reference: seeded from bank (directness, context_richness, exploration, analytical_depth, iteration, scope)
- monitors: drift-recall, territory-stability, reference-freshness (all informational — no hard gate)

## Drift recall (replaces the retired OOD-Δ floor)

- out-of-territory flagged: 8/8 (1.000) at OOD threshold 0.5
- in-domain false flags: 10/18 (0.556)

## Territory stability (per-dim centroid movement)

| dim | centroid shift |
|---|---|
| directness | 0.199099 |
| context_richness | 0.180263 |
| exploration | 0.233338 |
| analytical_depth | 0.255948 |
| iteration | 0.227469 |
| scope | 0.236479 |

- max shift: 0.255948 (watch: possible runaway reassembly)

## Reference freshness (injected now, age per dim)

| dim | seeded | live_count | age |
|---|---|---|---|
| directness | False | 10 | 999990.0 |
| context_richness | False | 10 | 999990.0 |
| exploration | False | 10 | 999990.0 |
| analytical_depth | False | 10 | 999990.0 |
| iteration | False | 10 | 999990.0 |
| scope | False | 10 | 999990.0 |
