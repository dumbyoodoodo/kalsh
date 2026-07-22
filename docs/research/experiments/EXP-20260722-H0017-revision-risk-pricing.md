# EXP-20260722-H0017-revision-risk-pricing

Executed exactly as pre-registered in
`PREREG-20260722-H0017-revision-risk-pricing.md`.

## Decision

**EFFICIENT-WITHIN-MARGIN** (step_3: CI within (-0.075, +0.075): equivalence established at the frozen margin).

The 95% CI on the mean calibration residual lies within +/-7.5pp -- a genuine equivalence claim at the stated margin: near-settlement NYC weather prices incorporated known revision risk to within economic materiality over this window. Not merely absence of evidence.

## Primary result

- cohort: N = 122 adjacent at-risk variable-days; qualifying revisions = 9 (realized rate 7.4%)
- mean post-event implied probability: 9.7%
- **D = +2.33pp**, 95% CI [-1.85, +6.50]pp (sd 0.235)

## Controls (non-decisional)

- locked-NO control: {'candidates': 77, 'priced': 76, 'mean_post_price': '0.005000000000'}
- event vs placebo reaction: {'event': {'n_with_both_windows': 113, 'mean_abs_change': '0.059778761062'}, 'placebo': {'n_with_both_windows': 110, 'mean_abs_change': '0.093090909091'}}
- pre-window calibration: {'n': 113, 'qualifying_events': 9, 'mean_price': '0.123982300885', 'realized_rate': '0.079646017699', 'd_hat': '0.044336283186', 'sd': '0.250421523586', 'ci95': ['-0.001835911987', '0.090508478359']}

Full cell-level results, exclusion accounting, sensitivities, and
liquidity descriptives: see the accompanying `-results.json`. Figures,
narrative, and limitations:
`docs/research/postmortems/2026-07-22-h0017-closeout.md`.
