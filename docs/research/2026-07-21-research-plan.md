# Research Plan — 2026-07-21

The research program for Phases 3-5: what experiments to run, in what order,
and how we will know if they succeed. Hypotheses referenced here are fully
specified in `HYPOTHESES.md`; the methodology every experiment must follow is
in `RESEARCH.md`. This document is a point-in-time plan — revise by writing a
new dated plan, not by rewriting this one after results are known.

Nothing here implements features, models, backtests, or trading logic.

---

## 1. Data platform review (as of 2026-07-21)

Verified directly against the live database and dataset builds, not from
memory.

### What exists

| Source | Coverage | Notes |
|---|---|---|
| `weather_observations` | NYC only; 2026-06-20 → 2026-07-20 (30 days, 116 rows) | Includes **issuance history**: 56 of 60 station/variable-days have ≥2 CLI issuances; first issuance differs from final on **11/60 (18%)** — immediately studyable |
| `weather_forecasts` | NYC; **1 issuance** (2026-07-20), 14 periods | Effectively zero history; accrues only going forward |
| `market_snapshots` / `orderbook_snapshots` / `trades` | **0 rows** | Collector has only been smoke-tested; live accumulation not started |
| `settlement_specs` | 0 rows persisted (parser validated live: 12/12 registry-station markets resolved, ADR 0005) | Resolution is automatic once markets are collected |
| `weather_stations` | 1 station (NYC / Central Park, OKX) | 19 more cities validated as data-only registry additions (ADR 0005) |
| Datasets | `weather_panel` builds (37 rows); `market_weather` builds but is empty | Point-in-time correctness tested; versioned manifests working |

### Variables available

- Settled daily `tmax_f` / `tmin_f` per station/day, **with full CLI issuance
  (revision) history** — not just final values.
- NWS forecast periods (point estimate, issue time, valid window) — as-of
  joinable.
- Once collection starts: market bid/ask/last, order-book depth at best,
  trades, close times, resolved settlement targets per market.

### Limitations that shape the plan

1. **No historical forecast archive** (ADR 0003: NDFD backfill deferred).
   Forecast history exists only from the day collection starts. Every
   forecast-dependent hypothesis is gated on wall-clock accumulation.
2. **No historical market data.** Kalshi exposes no retroactive order-book
   history; market snapshots accrue only while the collector runs. Every day
   not collected is unrecoverable.
3. **Single station.** NYC-only halves nothing but multiplies nothing;
   per-station samples accrue at ~1 settled day/station/day. Registry
   expansion multiplies accumulation ~20× at near-zero cost.
4. **Observation history is deep on demand.** IEM backfill supports years of
   CLI history via a config change (`WEATHER_BACKFILL_DAYS`) — observations
   (and their revision structure) are the one thing we can have a large
   sample of *today*.
5. **Sample-rate reality.** Daily-settlement research accrues ~30
   observations/station/month. Statistical power for market-edge claims will
   take months even multi-station — the plan sequences cheap, data-available
   studies first.

### Accumulation actions recommended now (operational, not research)

| Action | Cost | Unlocks |
|---|---|---|
| Run `collector run` continuously (production public data) | A supervised process | All market hypotheses (H0005-H0010); irreplaceable history |
| Run `weather collect` on schedule | Already operational | Forecast issuance history (H0003, H0004, H0006, H0008) |
| Deepen observation backfill (`WEATHER_BACKFILL_DAYS≈1095+`) | Config change + one slow cycle | H0002 at full sample size immediately |
| Expand station registry (19 validated cities) | Small data-only change, live verification per ADR 0005 procedure | ~20× accumulation rate on every weather hypothesis |

---

## 2. Candidate research questions considered

Generated broadly, then filtered by what this platform can actually measure:

1. Settlement-revision risk: how often and by how much do preliminary CLI
   values differ from final? → **H0002**
2. Forecast quality: what is the NWS forecast-error distribution by horizon,
   and is it stable enough to model? → **H0003**
3. Forecast-error structure: serial correlation, seasonal bias, cross-station
   differences? → **H0004**
4. Market calibration: are Kalshi daily-temp prices calibrated probabilities?
   → **H0005**
5. Threshold anchoring: is miscalibration concentrated near the point
   forecast, at long horizons? → **H0006** (the sharpened, testable core of
   the original H0001 thesis)
6. Hard-information incorporation: do prices respect the monotonic bound a
   preliminary intraday high imposes? → **H0007**
7. Reaction speed: how fast do prices adjust after a forecast revision?
   → **H0008**
8. Liquidity vs efficiency: are thin markets less efficient than costs can
   explain? → **H0009**
9. Lifecycle structure: where in time-to-settlement is inefficiency (if any)
   concentrated? → **H0010**
10. Order-book imbalance as short-horizon signal — **deliberately deferred**:
    pure microstructure, weakest link to the settlement thesis, needs the
    most data, highest overfitting risk. Reconsider only after H0005-H0007.
11. Forecast disagreement across providers — **deferred**: requires a second
    forecast provider; the `WeatherProvider` protocol supports adding one,
    but no current hypothesis justifies the integration cost yet.
12. Forecast issuance timing census — folded into H0008's design as a
    descriptive prerequisite, not a standalone hypothesis.

---

## 3. Prioritization

Scored 1-5 (5 = favorable). *Value* = expected information value toward the
program goal (a validated, calibrated edge — or a well-evidenced null);
*Effort* = 5 is easiest; *Robustness* = resistance to
overfitting/leakage/small-sample noise; *Data now* = fraction of required
history already in hand or obtainable by config alone.

| ID | Question | Value | Effort | Robustness | Data now | Sample size path | Actionable? |
|---|---|---|---|---|---|---|---|
| H0002 | CLI revision risk | 4 | **5** | 5 | **5** | Years, via backfill | Settlement-risk model input |
| H0003 | Forecast-error distribution | **5** | 4 | 4 | 1 (accrues) | ~90d × stations | Foundation of all pricing |
| H0005 | Market calibration null | **5** | 3 | 4 | 0 (accrues) | ~60-90d markets | The "is there any edge" gate |
| H0007 | Preliminary-obs bound | 4 | 4 | **5** | 0 (accrues) | Event-driven, small-N OK | Model-free mispricing detector |
| H0006 | Threshold anchoring | **5** | 2 | 3 | 0 | Months, multi-station | The core edge thesis |
| H0008 | Revision reaction latency | 3 | 3 | 3 | 0 | Event study, ~100 events | Execution-timing input |
| H0004 | Error persistence/season | 3 | 3 | 3 | 0 | ≥2 seasons or pooled | Model conditioning |
| H0009 | Liquidity vs efficiency | 2 | 3 | 3 | 0 | With H0005 data | Cost/sizing input |
| H0010 | Time-to-settlement effects | 3 | 3 | 3 | 0 | With H0005 data | Entry-timing input |

**Robustness note:** H0002 and H0007 are the most robust designs available —
H0002 is a measurement (no model to overfit), H0007 tests a hard logical
bound (no distributional assumption).

---

## 4. Recommended research sequence

```
Now                 → H0002 (runnable today; deep backfill is config-only)
                    + start all accumulation actions (§1) in parallel
~90 days of weather → H0003 (forecast-error distribution; pooled across
                      however many stations were added)
~60-90 days of mkt  → H0005 (market calibration null)
                    → H0007 (bound violations; same collected data)
After H0003 + H0005 → H0006 (threshold anchoring — needs both the residual
                      model foundation and the market baseline)
Opportunistic       → H0008 (once ~100 revision events collected)
Season permitting   → H0004 (needs winter-vs-summer contrast or wide pooling)
With H0005 data     → H0009, H0010 (descriptive add-ons to the same dataset)
```

Dependencies (prerequisite → dependent):

- H0002 → H0005/H0006/H0007 settlement-risk handling (how much noise the
  "label" itself carries).
- H0003 → H0006 (a calibrated residual baseline is the comparison object);
  → Phase 5 baseline models (its conclusion *is* the model choice evidence).
- H0005 → H0006/H0009/H0010 (calibration infrastructure and the null to beat).
- Registry expansion → every weather hypothesis's sample-size path.

### First experiment: H0002 — CLI settlement-revision risk

Justification:

1. **It is the only hypothesis fully runnable today** — and with a *large*
   sample (years of NYC CLI history via config-only backfill), not the 30
   days currently loaded.
2. **Zero model risk.** It is a measurement with a pre-registered materiality
   threshold, not a fitted model — the right first exercise of the
   experiment protocol end to end (hypothesis → dataset version → experiment
   record → conclusion) with no confounding.
3. **Its answer is load-bearing for everything downstream.** Every market
   hypothesis treats the settled CLI value as the label; H0002 quantifies
   exactly how noisy that label is between first issuance and final (already
   visibly ≈18% of days in the small loaded sample), and Kalshi's own rules
   warn about preliminary values. Both the eventual probability model and
   any near-settlement trading logic need this number.
4. **It exercises the platform's rarest asset** — the append-only issuance
   history that most datasets discard.

---

## 5. Feature planning (documentation only — nothing implemented)

Minimal feature sets per hypothesis. "Exists" = already a column in the
Milestone 4 datasets; "FE" = needs Phase 4 feature engineering.

| Feature | Definition | Status | Used by |
|---|---|---|---|
| F1 latest forecast as-of | `forecast_high_f`/`forecast_low_f` at decision time | Exists (`market_weather`) | H0003, H0005, H0006, H0008 |
| F2 forecast age | `forecast_age_seconds` | Exists | H0006, H0008 |
| F3 time to settlement | `close_time - observed_at` | Trivial derivation | H0005, H0006, H0010 |
| F4 season/month | calendar month of `target_date` | Trivial derivation | H0003, H0004 |
| F5 forecast horizon | `target_date` midpoint − forecast `issue_time` | Trivial derivation | H0003, H0004, H0006 |
| F6 market mid / spread | from `best_yes_bid/ask_cents` | Exists | H0005-H0010 |
| F7 preliminary obs as-of | `obs_value_known`, `obs_issuance_time_known` | Exists | H0002, H0007 |
| F8 revision delta | value change between consecutive issuances (obs or forecast) | FE (small) | H0002, H0008 |
| F9 issuance count as-of | `n_forecast_issuances_known` | Exists | H0008 |
| F10 forecast residuals | forecast − settled, by horizon (walk-forward safe) | FE (the first real Phase 4 item) | H0003, H0004, H0006 |
| F11 market-implied probability | mid price / 100 with cost bands | FE (small) | H0005, H0006, H0009, H0010 |

Reusable core: F1-F7 cover five hypotheses with zero feature engineering —
the Milestone 4 dataset columns were chosen well for this program. The first
genuine Phase 4 work item is **F10 (walk-forward-safe residual features)**,
needed by H0003 and everything downstream of it; F8 and F11 are small
adjuncts. No hypothesis in the first two waves needs more than these.

---

## 6. What "success" looks like for the program

- Either a **confirmed, calibrated, cost-surviving edge** (H0006 confirmed
  with H0003/H0005 foundations), or
- A **well-evidenced null** (H0005 holds; H0006 rejected) — which per
  `RESEARCH.md` is a successful outcome that stops the project from funding
  a losing strategy, with the platform still valuable for future market
  types.

Both outcomes require the same next steps; only the accumulation clock and
the experiment discipline differ. The failure mode to avoid is neither of
these: an under-powered, retrofitted "edge" that survives no out-of-sample
window — the methodology in `RESEARCH.md` §"Standard experiment protocol"
exists to make that outcome structurally difficult.
