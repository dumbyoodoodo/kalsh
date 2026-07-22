# EXP-20260722-H0014-recency-stability

Executed exactly as pre-registered in
`PREREG-20260722-H0014-recency-stability.md`.

## Decision

**B** (step_4: shift event(s) exist but are not cross-station corroborated in the same direction and no operational indicator fired).

At least one station shows a decision-level shift without cross-station corroboration or operational co-occurrence. The asymmetry's current magnitude is unreliable at the shifted station(s); downstream work must not assume it there. Retry with full-2026 windows (FY(2026) vs FY(2023-2025), a new pre-registered entry) on or after 2027-01-15.

## The declared family of 4 contrasts (2026 vs pooled 2023-2025, season-matched)

- CHI tmax_f: Δ = 0.097006857876 (95% ['0.033398055726', '0.167173674604']; decision 98.75% ['0.017190351188', '0.187293328836']) → shift_event = True (up)
- CHI tmin_f: Δ = -0.025066720719 (95% ['-0.091820871338', '0.048319121581']; decision 98.75% ['-0.108831219866', '0.069301158845']) → shift_event = False (down)
- NYC tmax_f: Δ = 0.023450586265 (95% ['-0.031794660130', '0.087453858173']; decision 98.75% ['-0.045604408169', '0.106495878247']) → shift_event = False (up)
- NYC tmin_f: Δ = 0.020100502513 (95% ['-0.046512237728', '0.092948895928']; decision 98.75% ['-0.063544842403', '0.113710727748']) → shift_event = False (up)

## Season-matched yearly cells (Jan 1 - Jul 21; decisional windows)

| station | variable | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|
| CHI | tmax_f | 35/201 (17.4%) | 29/198 (14.6%) | 29/199 (14.6%) | 50/198 (25.3%) |
| CHI | tmin_f | 56/201 (27.9%) | 52/198 (26.3%) | 58/199 (29.1%) | 50/198 (25.3%) |
| NYC | tmax_f | 29/197 (14.7%) | 22/202 (10.9%) | 37/198 (18.7%) | 34/199 (17.1%) |
| NYC | tmin_f | 38/197 (19.3%) | 53/202 (26.2%) | 53/198 (26.8%) | 52/199 (26.1%) |

## Full-year cells (tables only, never decisional)

| station | variable | 2023 | 2024 | 2025 |
|---|---|---|---|---|
| CHI | tmax_f | 50/362 (13.8%) | 50/359 (13.9%) | 51/362 (14.1%) |
| CHI | tmin_f | 96/362 (26.5%) | 92/359 (25.6%) | 92/362 (25.4%) |
| NYC | tmax_f | 46/360 (12.8%) | 36/363 (9.9%) | 54/360 (15.0%) |
| NYC | tmin_f | 77/360 (21.4%) | 98/363 (27.0%) | 84/360 (23.3%) |

## Season-matched gap D = p_tmin - p_tmax per year

- CHI: 2023: 0.104477611940, 2024: 0.116161616162, 2025: 0.145728643216, 2026: 0.000000000000; hist: 0.122073578595
- NYC: 2023: 0.045685279188, 2024: 0.153465346535, 2025: 0.080808080808, 2026: 0.090452261307; hist: 0.093802345059

Full cell-level results, diagnostics, and operational indicators: see the
accompanying `-results.json`. Figures, interpretation narrative, and
limitations: `docs/research/postmortems/2026-07-22-h0014-closeout.md`.
