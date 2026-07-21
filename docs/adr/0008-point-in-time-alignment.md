# ADR 0008: Point-in-time price/observation alignment

## Status

Accepted.

## Context

The Phase 7A H0007 readiness assessment (`docs/research/2026-07-21-h0007-readiness.md`)
identified one concrete remaining blocker before H0007 could be pre-registered
and run: `market_prices` (the candlestick archive) had no as-of join against
the weather-observation timeline, unlike `market_weather` (which already has
`obs_value_known`/`obs_issuance_time_known`, but only at the live snapshot
collector's ~zero-depth history). This phase (7B) removes that specific
blocker: a deterministic, leakage-free point-in-time join between every
stored candle and the observation state knowable at that instant. It does
**not** run H0007, analyze profitability, or add any forecasting/model logic
— purely a dataset-integration/feature-engineering milestone, same category
as `market_weather` itself.

## Decisions

1. **Extract the as-of join primitive into a reusable, independently-tested
   engine** (`dataset/asof.py`: `asof_latest`, `asof_count`), moved verbatim
   out of `builder.py` where it previously lived as private helpers used only
   by `build_market_weather`. No behavior change to existing frames — the
   full pre-existing test suite passed unchanged after the extraction. The
   engine is Polars `join_asof(strategy="backward")` under the hood, which by
   construction cannot select a future row: leakage-freedom is a property of
   the join itself, not of caller discipline. It is deterministic (a fixed
   input always sorts and joins to the same output, tested against
   reordered input), vectorized (no per-row Python loop), and resolution-
   /grouping-agnostic (`by` accepts any number of key columns, so "multiple
   cities" is just a longer `by` list — tested directly with two stations in
   the engine's own test suite).
2. **Observation timeline reconstruction is two independent, deterministic,
   model-free computations** (`dataset/observation_timeline.py`), not one:
   - `compute_running_extremes`: the cumulative max (`tmax_f`) / min
     (`tmin_f`) of the *reported* value across successive same-day
     issuances, in issuance order. This is exactly H0007's own rationale's
     "hard bound known so far" — not a prediction, a fact already published
     by NWS. Using `cum_max`/`cum_min` (rather than "the latest issuance's
     own value") is deliberately conservative: even in the rare case of a
     downward same-day correction (H0002 measured an asymmetry, not a
     guarantee), an earlier higher/lower reading remains a valid bound.
   - `attach_calendar_lock`: whether a target date's observation window (a
     station-local *calendar day*, matching every resolved
     `settlement_specs.observation_window` in this project) has fully
     elapsed as of a given instant. **Pure calendar arithmetic** — no
     observation data, no model, no guess about NWS reporting timing.
     Deliberately *not* defined as "has the final issuance arrived", which
     would require deciding which issuance is "final" without seeing the
     future — a genuine leakage risk this design avoids entirely.
3. **"Locked" and "the value is known" are independent facts, tested as
   such.** A market's observation window can close (`tmax_locked=True`)
   before NWS has actually published the confirming report — the running
   value stays at whatever the last legitimate issuance said until a real,
   later issuance supersedes it. `theoretical_remaining_range_{high,low}_f`
   is `0.0` once locked (no further data can move the bound) and `null`
   (deliberately "unbounded", not a numeric estimate) otherwise — this is
   the one place a weaker design could have leaked a forecast-like estimate,
   and it doesn't: there is no code path that infers a number instead of
   `null`.
4. **`build_market_price_weather` is built from `build_market_prices` plus a
   market-map inner join**, not a parallel reimplementation of the
   candle/metadata logic — DRY, and it means every `market_prices` guarantee
   (zero-volume flagging, strike/event metadata, dedup) is inherited for
   free. Grain matches `market_prices` (one row per stored candle);
   restricted to mapped markets only, same convention as `market_weather`
   (an unmapped market has no station/variable/target_date to align
   against — an orphan, not a null-weather row).
5. **Both `running_tmax_f_known` and `running_tmin_f_known` are attached
   to every mapped market, regardless of which single variable the market
   itself settles on.** A `tmax`-family market only *settles* on tmax, but
   both extremes are informative context, and the milestone's own
   requirements list both explicitly. `tmax_locked`/`tmin_locked` are two
   separate columns for the same reason, even though they are computed
   identically today (both variables share the same observation window
   under every settlement spec resolved so far) — a future spec divergence
   would only need to change `observation_timeline.py`, not every consumer.
6. **Settlement labels are joined exactly like `market_weather`'s block**
   (`pipeline.py`), not `market_prices`' wider block — `market_price_weather`
   already carries `station_id`/`variable`/`target_date` from the market map,
   so only the stage-value columns (`value_at_close`, `value_at_settlement`,
   `latest_final_value`, `settlement_label_status`, `settlement_label_version`)
   are pulled from `labels_frame`. `settlement/labels.py` itself is
   untouched — this phase only reuses its existing output, per the
   milestone's explicit "do not modify settlement labels" constraint.

## Leakage validation (requirement 4)

Three layers, all passing:

- **Engine-level** (`tests/unit/test_dataset_asof.py`): a future `other` row
  is never selected, proven directly against the join primitive, independent
  of any dataset frame.
- **Frame-level** (`tests/unit/test_dataset_leakage.py`): a synthetic
  same-day-preliminary-then-next-morning-revision scenario (79°F then 81°F)
  proves (a) a future observation never appears in `obs_value_known`, (b) the
  81°F revision is invisible to every candle strictly before its own
  06:00 issuance — including candles *after* the window has already locked,
  explicitly proving lock status is never used as a shortcut to reveal the
  final value early, and (c) a blanket structural check (`*_issuance_time_known
  <= period_end`, non-null) holds over every row of a built frame, not just
  the hand-picked example.
- **Pipeline-level, through the real settlement-label join**
  (`test_settlement_outcome_never_leaks_into_pre_settlement_weather_progression`):
  builds through the actual DB → `pipeline.build()` path with a real
  settlement label attached (`value_at_settlement=81`), and confirms the
  point-in-time progression columns for a pre-revision candle remain `79`,
  never silently copy the outcome label forward.
- **Full-scale, on the real production database** (761,475 rows, the entire
  Phase 7A archive): the same structural invariant (`*_issuance_time_known
  <= period_end`) checked over every stored row —
  **0 violations**. `dataset validate` reports 0 errors on the same build.

## Consequences

- The blocker identified in the Phase 7A readiness artifact is closed. See
  the follow-up readiness verification
  (`docs/research/2026-07-21-h0007-readiness-update.md`) for the updated,
  per-dependency verdict — pre-registration and execution of H0007 itself
  remain a separate, not-yet-taken step.
- `dataset/asof.py` is now the one place any future point-in-time join
  (a new city, a new resolution, a new source table) should be built from,
  rather than each frame reimplementing `join_asof` calls independently.
- No trading, alpha, forecasting-feature, or statistical-model code was
  introduced; `strategy/`, `features/`, and `models/` remain absent.
