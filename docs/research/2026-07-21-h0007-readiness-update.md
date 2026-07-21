# H0007 readiness verification — update after Phase 7B

**Status: report only. H0007 has not been run.** This updates
`docs/research/2026-07-21-h0007-readiness.md` (the Phase 7A assessment) now
that Phase 7B (`docs/adr/0008-point-in-time-alignment.md`) has closed the one
blocker that report identified — the missing point-in-time join between
`market_prices` and the weather-observation timeline. Every number below is
measured against the real production database (761,475 candles, 804
markets), not estimated.

## Is H0007 now fully executable?

**Data-ready to pre-register and run a first pass.** The point-in-time
alignment blocker is closed and verified leak-free at full scale. Two small,
non-blocking implementation items remain — neither is a data-collection gap,
and neither blocks a first execution of H0007's core mechanism:

1. **F8 (revision-delta feature)** is still unbuilt. It refines H0007's
   statistical adjustment ("explicit adjustment of the hard bound by H0002's
   measured revision asymmetry") but is not required for the core mechanism
   (flagging bounded strikes and measuring residual mass) — H0002's
   asymmetry estimate can be supplied as a fixed input constant without it.
   Small (a `diff()` over `running_extreme_f`'s already-materialized
   sequence); out of scope for this phase, which was scoped narrowly to
   price/observation alignment, not "every feature H0007 could use."
2. **A fee-schedule constant** for net-of-fees P&L is still not implemented
   anywhere in the codebase (`strategy/` correctly doesn't exist yet, per
   `ROADMAP.md`'s phase gating). Kalshi's public fee formula is small and
   well-documented; this is an execution-time addition, not a platform gap.

Neither item requires new data collection, new ingestion, or further
architecture — both are small, well-understood, and can be added at the
point H0007 is actually pre-registered and executed.

## Updated per-dependency table

| Dependency | Phase 7A verdict | Phase 7B verdict | What changed |
|---|---|---|---|
| Intraday market price/quote history | Ready | **Ready** | Unchanged |
| Preliminary CLI issuance times/values | Ready | **Ready** | Unchanged |
| ≥60 settlement days | Ready, at the margin | **Ready, at the margin** | Unchanged (66-67 days; Phase 7B doesn't add days, only alignment) |
| **Point-in-time price/observation join** | *(the blocker)* | **Ready** | Closed this phase — `market_price_weather`, 0 leakage violations over 761,475 rows |
| F6 (market bid/ask spread, as-of) | Blocked as literally defined (`market_weather`, ~zero historical depth) | **Ready** | `market_price_weather` carries `yes_bid_close_cents`/`yes_ask_close_cents` at every candle, full historical depth |
| F7 (preliminary obs as-of, `obs_value_known`) | Blocked as literally defined, same reason | **Ready** | `market_price_weather`'s `obs_value_known`/`obs_issuance_time_known`, same semantics as `market_weather`, now with real historical depth |
| F8 (revision delta) | Not required for 7A; small, unbuilt | **Still unbuilt, still small** | Out of scope for this phase too; refines but does not block the core mechanism |
| Quoted vs. traded price | Ready — both available | **Ready** | Unchanged |
| Fee/executable-cost representability | Partially ready | **Partially ready** | Unchanged — quotes ready, fee formula not implemented |
| Quote-staleness filter (failure mode 1) | Ready | **Ready** | Unchanged, now backed by explicit `obs_age_seconds`/lock tracking |
| ASOS/METAR sub-daily readings | Not required | **Not required** | Unchanged |

## What changed, measured

- New frame `market_price_weather`: 761,475 rows (matches `market_prices`
  exactly — every currently-known settled market already resolves through
  the settlement mapping, so nothing was excluded as an orphan in this
  build).
- `observation_quality_status`: 89,364 rows (11.7%) `"known"`, 672,111
  (88.3%) `"unknown"` — expected, not a gap: each market's candle window
  spans 5 days before close, but same-day observations only start existing
  within roughly the final day.
- `tmax_locked`/`tmin_locked` true for 8,713 rows (1.1%) — the
  settlement-window-elapsed subset, exactly the population H0007's
  end-game mechanism concerns.
- Of the 3,891 tmax-market rows where the window has elapsed, 3,848 (98.9%)
  already have a known observation value; 43 (1.1%) are in the reporting-lag
  gap between window-close and the confirming issuance arriving — itself a
  real, measured quantity H0007's failure-mode analysis can use directly.
- **Leakage: 0 violations** of `*_issuance_time_known <= period_end` across
  all 761,475 rows (the full archive, not a sample).
- A live, concrete instance of H0007's own mechanism, found directly in the
  aligned data (illustrative only — not an analysis, no claim of edge):
  `KXHIGHNY-26JUL14-B90.5`, 2026-07-14 — the same-day preliminary report held
  at 89°F from the evening of 7-14 through the observation window's close
  (past local midnight into 7-15), while the eventual settlement value was
  90°F (the confirming report hadn't arrived within the captured candle
  window). 9 distinct (station, target_date) pairs show this
  known-so-far-below-eventual-settlement pattern in the current archive —
  a raw count, not a claim about mispricing, sample size, or profitability.

## Recommended next milestone

H0007 can now be pre-registered for execution per `RESEARCH.md`'s standard
experiment protocol. Before executing:

1. Add F8 (revision delta) if the full pre-registered statistical adjustment
   is wanted for the first run — small, or explicitly defer it and use
   H0002's asymmetry estimate as a fixed constant for a first pass.
2. Add a small, explicit Kalshi fee-schedule constant/formula (not a
   `strategy/` module — a fee calculation, needed only to compute
   net-of-fees P&L for the decision rule).
3. Re-confirm the ≥60-day sample size is still adequate at execution time
   (it currently is, narrowly); do not lower the pre-registered threshold.

Do not run H0007 as part of closing out this report — that is a separate,
explicit next step requiring the user's sign-off per `HYPOTHESES.md`'s
process.
