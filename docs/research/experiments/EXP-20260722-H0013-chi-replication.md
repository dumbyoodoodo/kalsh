# EXP-20260722-H0013-chi-replication

Executed exactly as pre-registered in
`PREREG-20260722-H0013-chi-replication.md` (H0011's frozen protocol
re-instantiated for CHI, with the H0011 amendment's F1-F9 resolutions
inherited from the freeze).

## Decision

**CONFIRMED** (L(D_chi) > 0 and L(p_pm_chi) > 1/2).

## Part 1 (rate difference, CHI)

- tmax: 201/1282 revised (p = 0.156786271451)
- tmin: 330/1282 revised (p = 0.257410296412)
- D_chi = 0.100624024961, Newcombe 95% CI ['0.069393717609', '0.131656614311']
- criterion 1 (L > 0): pass

## Replication assessment vs H0011 (NYC, frozen published values)

- NYC: D = 0.109984399376, CI ['0.079970196795', '0.139841273431'] (N = 1282/variable)
- direction_replicated: True
- magnitude_consistent (CI overlap, descriptive): True
- D_chi - D_nyc (descriptive): -0.009360374415
- part2_consistent (descriptive): True

## Part 2 (adjacent required reporting -- inherited AMENDMENT Finding 1)

This result is evidence about settlement-label formation timing only -- that revised daily-minimum labels typically reach their final value in a post-midnight issuance. It does not identify the timing of the underlying physical temperature event (when the true daily minimum occurred), because this dataset carries issuance timestamps only, not occurrence timestamps. A study identifying the physical mechanism would require new occurrence-time data (e.g. parsed from raw CLI product text) and would constitute a separate, future hypothesis, not a re-interpretation of this one.

- tmax attribution rate: {'r_tmax': 201, 'x_pm_tmax': 199, 'rate': '0.990049751244'}
- Stratified gap decomposition: {'post_midnight_stratum_gap': '0.102184087363', 'pre_midnight_stratum_gap': '-0.001560062402'}
- Part 2 result: {'r_tmin': 330, 'x_pm': 330, 'n_unresolved': 0, 'p_pm': '1.000000000000', 'ci95_wilson': ['0.988493164287', '1.000000000000'], 'criterion_2_state': 'pass', 'required_adjacent_fields': ['descriptive_results.tmax_attribution_rate', 'descriptive_results.stratified_gap_decomposition']}

## NYC same-frame consistency cross-check (descriptive)

{'recomputed': {'n_tmax': 1283, 'r_tmax': 170, 'n_tmin': 1283, 'r_tmin': 311}, 'published_h0011': {'n_tmax': 1282, 'r_tmax': 170, 'n_tmin': 1282, 'r_tmin': 311}, 'note': 'expected +1 day per variable vs published (archive advanced 2026-07-20 -> 2026-07-21 between pins); larger divergence is a data-integrity flag -- PREREG Sec 9'}

Full cell-level results: see the accompanying `-results.json`. Figures,
diagnostics, comparison narrative, limitations, and the generalization
recommendation: `docs/research/postmortems/2026-07-22-h0013-closeout.md`.
