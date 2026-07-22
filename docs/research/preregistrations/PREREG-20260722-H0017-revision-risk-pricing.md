# PREREG-20260722-H0017-revision-risk-pricing

**Pre-registration package for H0017 — frozen 2026-07-22, before any
price-vs-outcome quantity, calibration residual, event-window price, or
at-risk cohort join has been computed.** H0017 is a market-efficiency
event study: do prices of revision-dependent contracts, immediately
after the preliminary CLI issues, match the realized frequency of
qualifying revisions? It is not a forecasting study and not a trading
strategy.

**This document computes and reports no decisional statistic.** Facts
cited are published H-series results and structural/coverage facts from
the pinned inputs' manifests.

## 0. Provenance — what has and has not been seen

**Seen pre-freeze (structural/coverage only, disclosed):** input
manifests and hashes; label coverage (NYC 69 target-dates per variable,
2026-05-15→07-22; 792 resolved of 828 NYC label rows at the time of the
E-A pin; the fresh pin has 2,862 label rows of which only NYC has candle
coverage); candle-window coverage (65% of market-days have a quoted
candle in 20:30–23:00Z; every in-window candle carries both quotes);
strike-type inventory (between/greater/less); candle schema
(bid/ask OHLC present; carried-forward flag present); series with candle
history (KXHIGHNY, KXLOWTNYC only). **Never computed:** any event-window
price, any implied probability, any price-vs-outcome pairing, any
at-risk contract's identity or outcome, any calibration quantity.

**Leakage rule adopted at freeze:** occurrence-time data (E0001/H0015
extract) derives from the *final* CLI product and is not knowable at
event time — it is excluded from every analysis in this study,
including descriptives. The only weather inputs are the preliminary
product's value and its archived publication timestamp — both
point-in-time by construction.

## 1. Hypotheses and predictions

- **H (miscalibration / anchoring direction):** post-event prices of
  at-risk contracts are too low relative to realized qualifying-revision
  frequency (traders anchor on the preliminary as final) — mean
  calibration residual D < 0 beyond the margin.
- **Alternative (overreaction):** D > 0 beyond the margin.
- **Efficient null:** |D| within the frozen ±0.075 margin (economically
  material threshold ≈ half the known revision base rate and ≈ 3–10×
  the per-contract fee scale at these prices).

## 2. Population, cohorts, inclusion/exclusion, datasets

| Item | Frozen value |
|---|---|
| Station cohort | **NYC only** (KXHIGHNY = tmax_f, KXLOWTNYC = tmin_f). CHI/DEN/LAX are excluded for zero candle coverage (their collection began 2026-07-21); this is a coverage exclusion, named here, not a choice made after seeing results. |
| Variable cohorts | tmax_f and tmin_f, pooled in the primary; split as secondary. |
| Unit of analysis | The **variable-day**: (variable, target_date) with one *adjacent at-risk* contract and (for the control) one *adjacent locked-NO* contract. |
| Time span | Every target_date present in both pinned inputs with a resolved label and an archived preliminary (structurally 2026-05-15 → 2026-07-21). |
| Input A (markets+labels) | dataset `exp-20260722-h0017-market`: `market_prices.parquet` hash `f364132289e7f453ddd0e6c969b0905809bc5f03ac000feab989e42e4083eb53` (761,475 rows; 1-min candles, bid/ask OHLC, strike structure, joined payout-validated labels), `settlement_labels.parquet` hash `49bc34dfc4d78da5494a23192780790f068dbb74f0f85a9c671b78258e9d7bf7` (2,862 rows). |
| Input B (preliminaries) | issuance frame `exp-20260722-h0013-replication`, hash `5ccf5b7a3ec11f1ec10fd8a3eac244640406130ec75934e58ead3b2f44e28775`. Preliminary value M(v,d) = first-issued value for (NYC, v, d); event time E(d) = the archived `issuance_time` (naive-UTC, publication timestamp) of that first product. |
| Inclusion (primary cohort) | variable-days with: resolved label (`settlement_label_status == "resolved"`); archived preliminary value and event time; an adjacent at-risk contract existing in that day's ladder; a fully-quoted candle in the POST window for that contract. |
| Exclusion / missing data | Days failing any inclusion item are excluded **and counted by reason** (no preliminary; no at-risk contract; no post-window quote; unresolved label). No other exclusions; no outlier removal. |

## 3. Event definition and windows (timing assumptions documented)

- **Event time** E(d): the archived publication timestamp of the day's
  first CLI product (the ~16:35 ET preliminary), per date, from Input B.
  Both variables share E(d) (one product carries both). Candle
  `period_start` and issuance timestamps are both naive-UTC (platform
  convention, verified in prior experiments); no clock reconstruction
  is performed anywhere.
- **Windows (frozen):** PRE = [E−90min, E−10min]; POST = [E+15min,
  E+120min]; PLACEBO event P = E−240min with windows shaped
  identically. The −10/+15 buffers absorb publication/propagation lag;
  POST ends hours before market close (~00:59 ET next day).
- **Price measurement:** the implied probability at a window is the
  bid/ask midpoint `(yes_bid_close + yes_ask_close)/2` (cents → /100,
  Decimal) of the **last** candle in-window with both quotes non-null.
  No trade requirement (quote-based); trade-price is a sensitivity.

## 4. Contract selection (deterministic, no discretion)

Strike semantics (frozen; validated by gate G3): `between` → YES iff
floor_strike ≤ value_at_settlement ≤ cap_strike; `greater` → YES iff
value > floor_strike; `less` → YES iff value < cap_strike.

For variable-day (v, d) with preliminary M:

- **tmax (final ≥ M, upward-only):** *at-risk* contracts are those
  whose YES region lies entirely above M (between/greater with
  floor_strike > M). **Adjacent at-risk** = the one with the smallest
  floor_strike. *Locked-NO* contracts are those whose YES region lies
  entirely below M (between/less with cap_strike < M); **adjacent
  locked-NO** = largest cap_strike.
- **tmin (final ≤ m, downward-only):** mirrored: at-risk = region
  entirely below m (between/less with cap_strike < m), adjacent =
  largest cap_strike; locked-NO = region entirely above m
  (between/greater with floor_strike > m), adjacent = smallest
  floor_strike.
- Distance stratum (descriptive): the gap between the preliminary and
  the at-risk region edge, in °F.
- **Outcome** y = 1 iff the selected contract's `kalshi_result` is
  "yes" (payout-validated labels; E-A 791/791).

## 5. Primary outcome and statistical procedure

- **Primary estimand:** D = (1/N) Σ (p_POST,i − y_i) over the primary
  cohort — the mean post-event calibration residual of adjacent
  at-risk contracts. Sign: D < 0 = revision risk underpriced; D > 0 =
  overpriced.
- **CI (deterministic):** D ± z·s/√N, z = `Decimal("1.959963984540054")`,
  s = sample standard deviation of residuals (Decimal precision-50;
  ROUND_HALF_EVEN 1e-12 quantized decisions; fixed-point 12-digit
  serialization — the program's frozen arithmetic, unchanged). The
  normal approximation on a mean of bounded residuals at the gated
  N ≥ 80 is the deterministic substitute for a bootstrap; declared.
- **Dependence, declared:** tmax and tmin of one calendar date are
  treated as independent units (their revision events are physically
  distinct; within-date revision concordance in the published record is
  low). Residual same-date dependence would widen true intervals
  slightly; disclosed, untreated.
- **Multiplicity:** exactly **one** primary cell (pooled). Everything
  in §7 is secondary/descriptive and decides nothing. No correction is
  therefore applied; declared.

## 6. Decision table (exhaustive; ordered; terminal rows)

| Step | Condition (quantized) | Outcome |
|---|---|---|
| 0 | any §8 gate fails | **BLOCKED** |
| 1 | L(D) > 0 | **OVERPRICED** — markets overreact to revision risk |
| 2 | U(D) < 0 | **UNDERPRICED** — markets underreact (anchoring direction confirmed) |
| 3 | L(D) > −0.075 and U(D) < +0.075 | **EFFICIENT-WITHIN-MARGIN** (equivalence established at ±7.5pp) |
| 4 | otherwise | **INCONCLUSIVE** (CI too wide to classify; the honest underpowered branch) |

Interpretation criteria (frozen): OVERPRICED/UNDERPRICED claims are
NYC-scoped, dated (2026-05→07), and quantified by the CI — step 2 in
particular is the first actionable-inefficiency finding of the program
and must be reported with the locked-control and placebo diagnostics
adjacent; EFFICIENT-WITHIN-MARGIN is a genuine equivalence claim at the
stated margin, not "absence of evidence"; INCONCLUSIVE names the
achieved half-width and the accumulation needed to shrink it. No
outcome licenses a trading decision; a step-2 result specifically
requires an independent replication on post-freeze data before any
Phase-6 use.

## 7. Secondary outcomes and descriptives (non-decisional)

Registered: per-variable D; per-distance-stratum D; pre-window D (was
risk priced before the preliminary?); **negative controls** — (a)
adjacent locked-NO control D_locked = mean(p_POST) vs certain 0, (b)
placebo-time "reaction" |p_PLACEBO-POST − p_PLACEBO-PRE| vs event
reaction |p_POST − p_PRE|; reaction magnitudes; monthly split;
liquidity descriptives (in-window spread, volume, quote presence);
exclusion-reason counts. Sensitivities: POST extended to [E+15m,
close]; trade-price (`price_close_cents`, non-carried-forward candles
only) instead of midpoint; window shift ±15 minutes. Figures (post-
verdict): event-study price paths (mean across cohort, minute-binned,
PRE→POST), calibration bar (p̄ vs ŷ with CIs), residual distribution,
locked/placebo control panel. Nothing beyond this list without an
amendment.

## 8. Power and eligibility gates

| Gate | Frozen requirement |
|---|---|
| G1a | market_prices + settlement_labels recomputed hashes == §2 pins |
| G1b | issuance frame recomputed hash == §2 pin |
| G2 | market frame unique on (market_ticker, period_start); labels unique on market_ticker; issuance-frame invariants as in prior studies; station set of Input B == {CHI,DEN,LAX,NYC} |
| G3 | **Strike-semantics agreement:** over all resolved labels with a numeric value_at_settlement, the §4 region rule reproduces `kalshi_result` on ≥ 99% of rows. **Event-time presence:** archived preliminary exists for ≥ 90% of label dates. |
| G4 | Primary cohort N ≥ 80; qualifying events Σy ≥ 5; post-window quote coverage ≥ 50% of at-risk days (missing counted by reason) |

Power, from structural facts only: ≤ 138 variable-days exist; with
N ≈ 80–130 and residual SD plausibly 0.2–0.35, the 95% CI half-width is
≈ 4–8pp. Only gross miscalibration is confidently detectable, and
equivalence at ±7.5pp is establishable only if residuals are tight;
step 4 exists precisely for the middle case. Every month of continued
collection adds ~60 variable-days; a successor at 3× the sample is
feasible by late 2026 if this run lands INCONCLUSIVE.

## 9. Reproducibility

As in all prior studies: two raw executions byte-identical before
reporting; no randomness anywhere; config-vs-prereg drift check against
independently re-typed literals; output schema frozen in
`TEMPLATE-H0017-manifest.json` (no key changes after results);
evaluation order G1→G2→G3→G4 → primary → decision → §7 descriptives;
iteration orders fixed (variables tmax_f then tmin_f, dates ascending).
**Point-in-time audit:** every price is an archived quote/trade
timestamped before or within its window; the event time is the archived
publication timestamp; preliminary values are first-issuance archive
rows; outcomes enter only as outcomes; occurrence-time data excluded
(§0); nothing is reconstructed.

## 10. Implementation-integrity checklist

- [ ] Strike-region semantics unit tests (all three types, boundary
      values, .5-strike vs integer-temperature edges) + the G3
      agreement gate on synthetic labels.
- [ ] Adjacent at-risk / locked-NO selection tests (ties, no-candidate
      days, both variables' directions).
- [ ] Window/price-measurement tests: last-quoted-candle selection,
      missing-quote handling, buffer boundaries, placebo windows.
- [ ] Residual/CI tests: hand-checked mean/SD/CI on synthetic
      residuals; decision-table branches incl. CI touching 0 and
      touching ±0.075 (resolve to the less-decisive branch).
- [ ] Gate tests: hash tampering, semantics-agreement failure,
      thin-cohort blocking with sentinels.
- [ ] Determinism: run-twice byte-identity on synthetic inputs.
- [ ] Code review against §2–§6 before first execution.

## 11. Known risks

- **R1 — Thin markets:** quote-based measurement mitigates missing
  trades but wide spreads make the midpoint a coarse probability proxy;
  spread descriptives quantify this; no correction is attempted.
- **R2 — Small event count:** Σy may be ≈ 5–15; the CI machinery is
  valid but the margin-vs-power tension is real and encoded in step 4.
- **R3 — Two-month window:** market-behavior conclusions decay
  (RESEARCH.md #9); any outcome carries a re-run date (+2 accumulation
  months).
- **R4 — Fee/discount microstructure:** even efficient prices sit
  slightly off pure probability (fees, tick size, time value ≈ hours);
  the ±7.5pp margin dwarfs these, and the locked-NO control measures
  the baseline discount directly.
- **R5 — Single station:** NYC-only by coverage; no cross-station
  generality is claimed.

---

**No event-window price, implied probability, at-risk identity, or
price-vs-outcome pairing has been computed. H0017 has not been
executed.**
