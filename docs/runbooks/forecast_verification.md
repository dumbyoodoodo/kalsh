# Runbook: Forecast verification framework

Reusable, deterministic infrastructure for joining point-in-time forecasts
to finalized observations without leakage. Implemented in
`kalshi_weather/verification/` (five modules: `eligibility.py`,
`forecast_matching.py`, `observation_matching.py`, `metrics.py`,
`reporting.py`).

**This package computes no scientific conclusion, fits no model, and owns
no hypothesis's decision thresholds.** It exists so H0003 retries, M-01,
and H0006 share one correct, tested implementation rather than each
reimplementing eligibility/matching/error arithmetic inline — which is
exactly how the gap this package closes was originally introduced (see
"Origin" below).

## Origin

H0003's first execution (2026-07-22,
`docs/research/postmortems/2026-07-22-h0003-closeout.md`) blocked at its
own point-in-time integrity gate: the platform's pre-existing "settled =
latest observation issuance" convention
(`dataset.builder._settled_observations`) is sound only once a target date
has actually *concluded*. For a day still in progress, "latest issuance so
far" is a same-day provisional reading, not a final truth — using it to
score a forecast is a leakage risk wearing a join's clothing. The
closeout's own recommendation was to add this as an explicit, reusable
eligibility criterion for the next execution rather than patch it
in-place. This package is that fix, generalized.

The already-executed H0003 experiment script
(`scripts/exp_h0003_forecast_error_foundation.py`) and its frozen
pre-registration are **unmodified** — each is a closed artifact of an
already-executed study. A future H0003 retry, M-01, or H0006 should import
this package rather than reimplement the logic H0003's script wrote inline.

## Eligibility rules

An observation for `(station_id, variable, observation_date)` is
**eligible** for verification use if and only if its **latest stored
issuance** was recorded at or after **local midnight of the day following
`observation_date`**, in the station's own IANA timezone.

This is the same boundary convention H0011, H0013, and H0015 each
independently froze for their own (closed) hypotheses about settlement-
label revision timing; `eligibility.local_midnight_boundary` is the
canonical, reusable form of it. Rationale: the daily CLI report cycle
issues a same-day preliminary in the afternoon and a genuinely final
report after local midnight (confirmed across H0002/H0011/H0013's
archives) — anything before that final report is provisional and subject
to revision.

If not eligible, `ObservationEligibility.reason` names why:

| Reason | Meaning |
|---|---|
| `no_observation` | zero issuances stored for this key |
| `not_yet_final` | issuances exist, but the latest predates local midnight of the next day (the day is still in progress, or its final report hasn't been collected yet) |
| `unknown_station` | the station isn't in the registry (no timezone available to evaluate the boundary) |

`ObservationEligibility.final_value` / `final_issuance_time` are populated
**only** when `eligible` is `True` — a caller cannot obtain a value from
this type without it having passed the finality check. When issuances
exist but the day isn't yet eligible, `latest_seen_value` /
`latest_seen_issuance_time` expose the same-day provisional reading for
diagnostic/reporting use only. **Never use `latest_seen_*` as ground
truth** — that is exactly the mistake this package exists to prevent.

## Temporal guarantees

- **Horizon (lead time)** is computed as `valid_start - issue_time` —
  both are metadata carried on the forecast product itself, known at
  issuance. This is the standard NWS-verification lead-time convention. A
  naive "target-day-midnight" reference was considered and rejected at
  design time (during H0003's own pre-registration): it produces negative
  horizons for ordinary same-day afternoon/evening forecasts.
- A horizon within `(-slack_hours, 0)` (default slack: 1 hour) is
  classified into the lowest bucket rather than excluded — this tolerates
  ordinary collection-latency artifacts, not genuine anomalies.
- **Selection within a cell**: when multiple issuances fall in the same
  `(station, variable, target_date, bucket)` cell, `select_forecasts`
  picks one deterministically (default: the latest issuance — the
  freshest information available at that horizon depth). Exact-duplicate
  natural keys (a data-quality anomaly `find_duplicate_keys` also flags
  separately) are still resolved deterministically, by `(issue_time,
  value)` ordering, never by input order.
- **The integrity check**: `match_forecasts_to_observations` flags (as
  `integrity_violation_issue_after_final`, never silently drops) any
  forecast whose `issue_time` does not strictly precede the observation's
  finalizing issuance time. This is the exact scenario H0003's G2c gate
  caught, generalized as a per-row primitive. **This package does not
  decide whether a violation blocks an entire run** — that policy belongs
  to each hypothesis's own frozen pre-registration and gate, not to this
  library.

## Leakage prevention

- Truth is used **only to score** an already-made, independent forecast
  — never to construct, adjust, or select the forecast itself. This is
  the standard, non-leaking "verify against eventual truth" pattern
  (identical in kind to scoring a backtest against a settlement outcome).
- No revised/superseding forecast is substituted for an earlier one: each
  `(issue_time)` row is scored as it was issued.
- Horizon is computed from `issue_time` and `valid_start` only — never
  from the observation's occurrence time or issuance time (which would
  leak observation-side information into a forecast-side quantity).
- Eligibility is a pure function of the *stored issuance history* for a
  target date — it never reads a wall-clock "now"; the same input always
  produces the same eligibility decision, indefinitely (a requirement for
  reproducible re-execution of a closed experiment's inputs).

## Metrics (generic, no inference)

`metrics.py` provides `signed_error`, `absolute_error`, `bias`, `mae`,
`rmse`, `error_sd` — pure point-estimate arithmetic over `(forecast,
observed)` Decimal pairs. **No confidence intervals, no significance
tests, no thresholds live here.** A hypothesis's own pre-registration
decides what to do with these numbers (which CI construction, which
decision margin, which multiplicity handling) — this module only computes
the numbers themselves, deterministically, using `Decimal(str(v))`
construction throughout (never `Decimal(v)` directly, which would import a
float's binary-exact expansion).

## Reporting

`reporting.py` produces plain, JSON-serializable, deterministically sorted
structures: `eligible_forecasts_table`, `excluded_forecasts_table`,
`exclusion_reason_summary`, `forecast_coverage_report`,
`horizon_coverage_report`. Drop these directly into an experiment's own
results manifest (`json.dumps(..., sort_keys=True)` is byte-identical
across reruns of the same inputs).

## Usage by a future H0003 retry

```python
from kalshi_weather.verification import (
    eligibility_table_from_frame, build_observation_lookup,
    forecasts_from_frame, select_forecasts,
    match_forecasts_to_observations, metrics, reporting,
)

eligibility_table = eligibility_table_from_frame(observations_frame)
lookup = build_observation_lookup(eligibility_table)

forecasts = forecasts_from_frame(extract_frame)
selected, forecast_excluded = select_forecasts(forecasts)  # pass the
# retry's own pre-registered buckets/slack/max-horizon here, not the
# library defaults, if they differ from H0003's original scheme

matched, unmatched = match_forecasts_to_observations(selected, lookup)

# The retry's OWN frozen gate decides pass/fail from here -- e.g. requiring
# zero integrity violations, and >=20 matched rows per (station, variable,
# bucket) cell, exactly as H0003's pre-registration specified:
pairs = [(m.forecast.value, m.observed_value) for m in matched]
bias_estimate = metrics.bias(pairs)  # the retry computes its OWN CI on this

coverage = reporting.forecast_coverage_report(matched, forecast_excluded, unmatched)
```

The retry's pre-registration should explicitly add the eligibility fix
this package encodes as a **named inclusion criterion** (it already is,
by construction, once the retry uses `eligibility_table_from_frame` /
`check_eligibility` instead of the old `_settled_observations` "latest
issuance so far" convention).

## Usage by M-01 (Phase 5 baseline models)

M-01 fits an empirical residual distribution per horizon bucket. This
package supplies exactly the leak-free `(forecast, observed)` pairs, per
bucket, that a walk-forward fitting procedure needs as its raw material —
`match_forecasts_to_observations`'s output, grouped by `.bucket`. M-01
still owns its own walk-forward window construction, its own baseline
comparison (climatology), and its own calibration evaluation (PIT,
coverage) — none of that lives here, matching the "no predictive models,
no scientific interpretation" scope of this package.

## Validation

`tests/unit/test_verification_*.py` (64 tests): eligibility across every
DST/boundary/unknown-station case; horizon bucket boundaries and the
collection-latency slack; forecast selection determinism (including exact
key collisions); the integrity-violation check at its exact boundary
(`issue_time == final_issuance_time`); every metric hand-checked at the
library's own 50-digit Decimal precision; deterministic, byte-identical
reporting output across reordered input. Every module also passes mypy
`--strict`.
