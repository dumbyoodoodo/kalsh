# Prospective hypothesis backlog — audited 2026-07-27

> **NOT A REGISTRATION.** Nothing in this document is preregistered,
> frozen, hashed, or ledger-recorded. Every candidate must pass the full
> RESEARCH.md pre-freeze gate (leakage audit, availability-contract
> declaration, close-time guard, partition isolation, provenance) before
> any registration. **Next review: after the 2026-08-04 pilot review and
> the H0019 (post 2026-08-25) / H0020 (post 2026-09-22) evaluations.**
> No candidate may be tuned on, or tested inside, H0019's test window
> (08-12→08-25) or H0020's validation/test windows (08-26→10-06).

Supersedes the backlog portion of `2026-07-22-research-roadmap.md` for
*new* H-series candidates; that roadmap's calendar-gated retries and
E-series remain authoritative and are inventoried below, updated for what
has happened since (H0017 ran → EFFICIENT-WITHIN-MARGIN; H0018 closed
NOT SUPPORTED; H0019/H0020 registered; workbench + leakage tooling built;
SEA/PHX/MIA pilot live).

## Inventory (existing program, statuses only — no outcomes inspected)

| Item | Status | Disposition |
|---|---|---|
| H0002/H0011/H0013 label-formation family | Confirmed | Complete; payload extracted as features/label rules |
| H0007, H0017 near-settlement pricing | Rejected (efficiency nulls) | **Family adjudicated** — high bar for re-entry |
| H0014 (+merged H0015b) | Retry ≥ 2027-01-15 | Calendar-gated, already designed |
| H0012 retry (label stability, claim B) | Retry ≥ 2026-08-19 | Calendar-gated, trivial, already designed |
| H0003 forecast-error foundation | Blocked/retry ≥ ~Oct–Nov | Critical path for model work (M-01, H0006) |
| H0005 retry (calibration null) | ~Oct, power-gated | Scheduled |
| H0006 threshold-anchoring | After H0003 | The decisive thesis test; Tier 2 by calendar |
| H0004 conditioned error models | 2027 (seasons) | Scheduled |
| E0002 liquidity characterization | Runnable now | Exploratory (not an H-series); feeds cost models |
| E0003 cross-strike coherence scan | Nov–Dec | Exploratory |
| H0018 → H0019 (levels vs market) | Closed → frozen NOT_READY | Reserved |
| H0020 (revision underreaction) | Frozen COLLECTING_TRAIN | Reserved; secondaries cover asymmetry/timing |

## Excluded / merged new-candidate families (with reasons)

- **Warming-vs-cooling asymmetry; late-revision timing; horizon-dependent
  response; per-station heterogeneity of revisions** — SAME_FAMILY as
  H0020 (registered secondaries or moderators); standalone versions wait
  for H0020's outcome.
- **Cross-city replication of H0020 at pilot stations** — the *designated
  successor if H0020 is supported*; contingent, not draftable before the
  outcome exists (SAME_FAMILY_REPLICATION).
- **Market response to first-observation / preliminary-CLI evidence;
  near-settlement revision-risk discounting (incl. tmin-noise pricing)**
  — adjudicated by H0007 + H0017 efficiency nulls at the sharpest
  information point; re-entry requires a materially different mechanism.
- **Partial-day temperature trajectories** — no intraday observation
  stream is collected (daily CLI aggregates only): point-in-time data
  does not exist. Rejected.
- **Forecast-source disagreement** — single point-in-time source (NWS);
  no second archived source. Rejected until one is collected.
- **Underreaction conditional on prior forecast error; station
  reliability priors** — foundation is H0003 (blocked, integrity gate).
  Tier 2 behind H0003.
- **Threshold-distance / extremity recalibration (H0006 flavor)** —
  kept as the scheduled H0006, gated on H0003; not duplicated here.
- **Information arrival during collection gaps** — an operations
  question, not a scientific hypothesis. Excluded.
- **Book-depth/spread conditioning (H0009)** — needs a demonstrated
  efficiency deviation to condition on; wait for H0020/E0002. Tier 2.

## Tiered ranking (new candidates surviving exclusion)

**Tier 1 — draft-worthy now:** HX-A forecast-instability underpricing
(drafted below; the only new candidate with a distinct mechanism, a
valid temporal contract, and current+prospective data).

**Tier 2 — collect more data / calendar-gated:** H0006 (after H0003) ·
H0009 liquidity conditioning (after H0020/E0002) · prior-error
conditioning (after H0003) · H0020 pilot-city replication (after H0020)
· H0005r/H0012r/H0014r as already scheduled.

**Tier 3 — reject/archive:** trajectory studies (no data),
source-disagreement (no second source), near-settlement pricing
variants (adjudicated), gap-arrival (not science), cosmetic H0020
variants.

## Draft candidate (NOT registered, no number assigned, no hash)

### HX-A — Markets underprice forecast instability

- **Mechanism:** an unstable forecast (large intraday revision
  dispersion for the target day) implies a wider predictive temperature
  distribution; threshold prices should be pulled toward 50¢ when
  instability is high. If participants set extremity from the point
  forecast alone, high-instability days are systematically over-extreme.
- **Confirmatory claim:** conditioning market extremity on point-in-time
  revision dispersion improves probability scoring over the market
  alone. **Distinct from H0020** (unsigned dispersion vs signed
  direction; uncertainty-width vs directional underreaction) and from
  H0017 (forecast-side pre-close uncertainty, not settlement-label
  revision risk near close).
- **Primary estimand:** test-window Brier(M_disp) − Brier(M1_market),
  named only — never computed before the registered final run.
- **Unit/grouping:** event_group = (station, variable, target_date).
- **Eligibility:** single-sided KXHIGHT*/KXLOWT* threshold markets, all
  seven collected stations, production provenance, settled binary
  labels, candle at decision, **≥3 distinct ingested issuances for the
  target day at decision** (else excluded with reason
  `insufficient_issuances_for_dispersion` — never imputed).
- **Feature:** dispersion = std-dev of the per-issuance high/low point
  estimates available at decision (ingestion contract: `observed_at ≤
  decision`, issue ordering for sequence only); single interaction
  feature `disp × (logit(market_prob) − 0)` shrinking extremity, plus
  logit(market_prob). No other features; no feature search.
- **Availability contract:** **ingestion** (`observed_at`), R003-declared.
- **Decision horizon:** close − 24 h (close-time guard preflight
  REQUIRED; run `experiment preflight`-style stage check before freeze).
- **Windows (concept, to be fixed at registration):** train from
  registration date → +3 wks; validation +2 wks; test = 2 untouched
  future weeks **starting no earlier than 2026-10-07** (after H0020's
  absolute extension end); H0019's 08-12→08-25 and H0020's 08-26→10-06
  excluded from every split.
- **Baseline:** raw market probability (clip [0.02, 0.98]).
- **Missingness:** exclusion with reason codes (frozen at registration).
- **Robustness/ablations (exploratory only):** per-station, tmax vs
  tmin, dispersion-tercile strata, clip/L2/min-issuance sensitivity via
  the workbench sensitivity module.
- **Multiple testing:** one primary comparison; ledger family count +1.
- **Gates (concept):** ≥25 test event-groups, ≥3 stations,
  concentration ≤0.40, ≥5 pos/neg labels, bootstrap units ≥25 — the
  H0019/H0020 gate family.
- **Stopping/invalidation:** deterministic extension rule as in H0020;
  leakage/hash/issue-time-availability/split failure → INVALID; a
  negative result is final.
- **Pre-freeze requirements:** full RESEARCH.md gate (leakage audit,
  contract declaration, close-time guard, partition isolation,
  provenance) — all tooling exists; **no new capability needed**.
- **Earliest plausible readiness:** registration after the H0020
  test window closes (~2026-09-23+); test complete ~late October.
- **Early inspection prohibited:** counts-only readiness until the
  single registered final run.

Counts-only feasibility (reproduce, do not trust stale numbers):
`SELECT count(*) FROM (SELECT station_id, date(valid_start), count(DISTINCT issue_time) n FROM weather_forecasts GROUP BY 1,2) s WHERE n>=3;`
(2026-07-27: 50 of 81 station-target-days dispersion-computable; ~32
station-target-days/day accruing at 7 stations; 2,216 settled
temperature-family markets to date.)

## Station-expansion interaction
HX-A works at the frozen 4 stations but 7 materially improve station
gates and regime diversity (marine/desert/tropical added); DFW/MSP/ATL/
MSY would add external validity, not rescue feasibility. **Waiting for
the 2026-08-04 pilot review costs nothing** (HX-A cannot register before
late September anyway) — no station action is requested by this backlog.
