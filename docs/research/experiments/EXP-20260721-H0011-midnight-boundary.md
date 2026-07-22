# EXP-20260721-H0011-midnight-boundary

Executed exactly as pre-registered in
`PREREG-20260721-H0011-midnight-boundary.md` and
`AMENDMENT-20260721-H0011-audit-resolution.md`.

## Decision

**CONFIRMED** (L(D) > 0 and L(p_pm) > 1/2).

## Part 2 (adjacent required reporting -- AMENDMENT Finding 1)

This result is evidence about settlement-label formation timing only -- that revised daily-minimum labels typically reach their final value in a post-midnight issuance. It does not identify the timing of the underlying physical temperature event (when the true daily minimum occurred), because this dataset carries issuance timestamps only, not occurrence timestamps. A study identifying the physical mechanism would require new occurrence-time data (e.g. parsed from raw CLI product text) and would constitute a separate, future hypothesis, not a re-interpretation of this one.

- tmax attribution rate: {'r_tmax': 170, 'x_pm_tmax': 170, 'rate': '1.000000000000'}
- Stratified gap decomposition: {'post_midnight_stratum_gap': '0.109984399376', 'pre_midnight_stratum_gap': '0.000000000000'}
- Part 2 result: {'r_tmin': 311, 'x_pm': 311, 'n_unresolved': 0, 'p_pm': '1.000000000000', 'ci95_wilson': ['0.987798751679', '1.000000000000'], 'criterion_2_state': 'pass', 'required_adjacent_fields': ['descriptive_results.tmax_attribution_rate', 'descriptive_results.stratified_gap_decomposition']}

Full cell-level results: see the accompanying `-results.json`.
