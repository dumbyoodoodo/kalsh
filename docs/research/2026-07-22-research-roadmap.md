# Research roadmap — 2026-07-22 (supersedes 2026-07-21-research-plan.md)

Planning document only. Nothing here is pre-registered, executed, or
implemented by this document; every listed study still passes through the
full HYPOTHESES.md / pre-registration process when its turn comes.

## 0. PI assessment — where the program actually stands

The label-formation family (H0002 → H0011 → H0013 → H0014 → E0001 →
H0015) is the program's scientific success story, and it is now at
**diminishing returns as a primary direction**. What it established is
essentially complete for practical purposes: settlement labels are
provisional until the post-midnight report; revision risk is
variable-specific (tmin ≈ 2× tmax), station-structural (DEN ~91% vs LAX
~1.2% — an order-of-magnitude spread set by preliminary-cutoff
structure), predictable from occurrence timing, and stable across years
except one flagged, unresolved 2026 CHI shift. Each marginal experiment
in this family now refines a mechanism whose practical payload —
revision-risk features and label-handling rules — is already extracted.

Meanwhile the program's *actual* objective — calibrated pricing of
Kalshi weather contracts — has exactly one confirmed market-side fact
(H0007: no bound-violation mispricing) and one underpowered null
(H0005). The central questions are untouched, for two reasons: the
forecast archive only began accruing 2026-07-20 (the hard clock nothing
can accelerate), and the market-side studies attempted so far ran
before enough settled-market history existed.

**The pivot this roadmap makes:** treat label-formation knowledge as an
*input* (features, risk models, label conventions) and redirect primary
research effort to the market side — first the studies runnable *today*
on the archived price/orderbook/settlement record, then the
forecast-foundation studies as their data matures. Two label-family
retries remain on calendar gates and should run; no new label-family
study should be initiated beyond them without a market-side consumer.

**Infrastructure verdict: stop building.** Collection, reproducibility,
datasets, service management, and the experiment harness are mature and
exceed current research needs. The only justified platform work in the
next two quarters is consumer-driven: folding occurrence-time fields
into the dataset builder when the merged H0014/H0015b retry needs them,
and a backtest cost model *only if* H0006-class results justify Phase 6.
Anything else is procrastination with extra steps.

**Program-level risk to keep honest about:** weather-market liquidity
may be thin enough that even a real edge is untradeable at meaningful
size. E0002 (liquidity characterization) runs first partly because it
is cheap and partly because it bounds the value of everything downstream.
A second honest risk: efficient-market nulls are the *likely* outcome of
the pricing studies (H0007 already returned one); the roadmap treats
well-powered nulls as successful deliverables, per RESEARCH.md.

## 1. Unanswered scientific questions

Q1. Does any Kalshi weather market misprice anything, after costs? (The
    founding thesis; untested. H0005 underpowered, H0007 rejected one
    narrow form.)
Q2. Do near-settlement prices correctly discount *known* revision risk
    — the one information asymmetry the program has actually proven
    exists? (Answerable now from archives.)
Q3. Is NWS forecast error stable and estimable by horizon (H0003)? The
    foundation for every pricing model. (Gated: ~90 accrual days.)
Q4. What are the liquidity/cost realities of these markets? (Answerable
    now; bounds the whole trading application.)
Q5. Is the 2026 CHI tmax shift persistent (H0014-B) and is the
    composition mechanism confirmable at composition extremes
    (H0015-blocked)? (Gated: full-2026.)
Q6. Is the settlement-time label stable at adequate n (H0012 claim B)?
    (Gated: 2026-08-19.)
Q7. Are cross-strike/bucket price structures internally coherent?
    (Answerable now; prior lowered by H0007's null.)
Q8. Does conditioning forecast-error models on anything (season,
    persistence, station) beat the unconditional baseline (H0004)?
    (Gated: seasons of data; 2027.)

## 2. Retire / park / keep

**Retired (leave closed, no successor):** H0007 (rejected, adequately
powered for its claim); H0001 as an umbrella (concluded via H0006
when it runs); the physical midnight-boundary mechanism study (the
"H0016 candidate") — its practical payload is zero given what is
already known about label formation timing; park indefinitely unless a
consumer appears.

**Merged:** standalone H0015b is retired as a separate study; its
redesigned criteria (extreme-composition-valid, thin-cell exact
binomials) fold into the H0014 retry's pre-registration — one
full-2026 study answering recency and mechanism together (P7).

**Kept, calendar-gated:** H0012 retry (Aug 19); merged H0014/H0015b
retry (Jan 2027); H0003 (~Nov); H0004 (2027, after contrasting
seasons); H0005 retry (when accrued market history supports the
pre-registered power analysis); H0006 (after H0003).

## 3. The next 10 projects, ranked and sequenced

Scoring: value = expected scientific value to the program's objective;
novelty = new knowledge vs refinement; feasibility = can it run when
scheduled; data = additional accumulation required; effort = person-time
to pre-register, implement, execute, report.

| Seq | ID | Project | Value | Novelty | Feasibility | New data | Effort |
|---|---|---|---|---|---|---|---|
| 1 | E0002 | Liquidity & microstructure characterization | High (gatekeeper) | Med | Now | none | Small |
| 2 | H0012r | Settlement-label stability retry | Med-High | Low | Aug 19 | ~4 wks (accruing) | Trivial |
| 3 | H0017 | Revision-risk pricing / CLI-preliminary event study | **Highest** | High | Now | none | Medium |
| 4 | H0005r | Market calibration null, retry | Med | Low | ~Oct | accruing | Small |
| 5 | H0003 | Forecast-error foundation | High | Med | ~Nov | 90 accrual days | Medium |
| 6 | E0003 | Cross-strike coherence scan | Med | Med | Nov-Dec | none | Small |
| 7 | H0014r (+H0015b) | Full-2026 recency + mechanism | Med | Med | Jan 2027 | full-2026 | Medium |
| 8 | M-01 | Baseline probability model + calibration (Phase 5) | High | Med | Feb 2027 | H0003 out | Medium |
| 9 | H0006 | Threshold-anchoring mispricing (the thesis test) | High (decisive) | High | Mar 2027 | forecast+market months | Large |
| 10 | B-01 | Cost-model backtest + paper-trading go/no-go | Med (decision) | Low | Q2 2027 | E0002 out | Medium |

### Project cards

**1. E0002 — Weather-market liquidity & microstructure (exploratory).**
Objective: characterize spreads, depth, quote presence, trade volumes,
and time-of-day liquidity across the collected weather series.
Why: bounds the value of the entire trading application; supplies the
empirical cost model every backtest needs; informs H0005r power design.
Dependencies: none (orderbook/trade archive). Effort: small. Impact:
high as a gatekeeper — a "these markets are untradeably thin" finding
would legitimately downgrade Phase 6+ and refocus the program on
research outputs.

**2. H0012 retry — settlement-time label stability.**
Objective: re-run claim B (post-settlement correction rate < 2%) at
n ≥ 189 variable-dates.
Why: the canonical-label guarantee every market study scores against.
Dependencies: calendar (≥ 2026-08-19). Effort: trivial (script exists).
Impact: medium-high; converts a favorable-but-underpowered result into
a usable guarantee (or surfaces a real problem early).

**3. H0017 — do near-settlement prices discount known revision risk?**
Objective: pre-register an event study around the scheduled ~16:35
preliminary CLI issuance: (a) how fast/completely prices in
still-trading brackets adjust; (b) whether post-preliminary prices
embed the historically-measured conditional revision probabilities
(upward-only tmax revisions, station/variable-specific rates, late-max
composition), vs treating the preliminary as final.
Why: the sharpest possible crossover of the program's proven knowledge
into a market-efficiency test — the first genuine edge test, runnable
entirely on archived candles × issuance archive × validated settlement
labels. A confirmed inefficiency here is directly actionable; a
well-powered null is the honest headline that near-settlement weather
pricing is efficient.
Dependencies: E0002 (cost context), H0012r (label guarantee) — both
precede it in sequence. Clustered-by-event-day inference per
RESEARCH.md. Effort: medium. Impact: highest of any runnable-now study.

**4. H0005 retry — market calibration null.**
Objective: re-run the calibration comparison with the accumulated
settled-market sample and E0002-informed power analysis.
Why: the program's basic "is the market calibrated at all" null
deserves an adequately powered answer before model-building claims any
edge. Dependencies: accumulation (verify pre-registered power gate
before running). Effort: small. Impact: medium.

**5. H0003 — forecast-error distribution foundation.**
Objective: as pre-registered: horizon-bucketed empirical error CDFs,
walk-forward PIT uniformity, coverage, log score vs climatology, pooled
and per-station.
Why: the foundation of every pricing model (Phase 5 gate). Nothing
downstream is meaningful without it. Dependencies: ~90 accrual days
(≈ late October); multi-station pooling already shortens wall-clock.
Effort: medium. Impact: high — it is the critical path.

**6. E0003 — cross-strike coherence scan (exploratory).**
Objective: scan archived bucket/threshold prices for internal
inconsistencies (monotonicity violations, adjacent-bucket sums, ladder
arbitrage) with cost-awareness from E0002.
Why: cheap; complements H0007's null with the broader coherence
question; any recurring structure becomes a pre-registered H-series.
Dependencies: none. Effort: small. Impact: medium (prior is
efficiency, but the scan is nearly free and its null is citable).

**7. H0014 retry + H0015b (merged) — full-2026 recency and mechanism.**
Objective: one pre-registration, full-year-2026 windows: (a) H0014's
FY contrast at CHI/NYC; (b) composition-mechanism criteria valid at
composition extremes (exact thin-cell bounds; composition-implied
prediction error; cross-station ordering incl. DEN/LAX), with the
blocked run's published gate values disclosed as post-hoc knowledge.
Why: closes the two open label-family questions in one time-disjoint
shot; the occurrence-time feature enters the dataset builder here (its
first consumer). Dependencies: calendar (≥ 2027-01-15). Effort:
medium. Impact: medium — closes the family cleanly.

**8. M-01 — baseline probability model + calibration report (Phase 5).**
Objective: implement the pre-specified simple baselines (empirical CDF;
Gaussian/Student-t if justified) mapping forecast-error distributions
to contract probabilities; evaluate purely on walk-forward calibration.
Why: the platform's stated purpose; strictly downstream of H0003 per
the model-progression rules. Dependencies: H0003 outcome (including
possibly "unstable — don't model yet"). Effort: medium. Impact: high.

**9. H0006 — threshold-anchoring mispricing (the decisive thesis test).**
Objective: pre-registered comparison of market-implied vs
model-implied probabilities near strike boundaries, after costs, with
clustered inference and replication-slice requirements.
Why: this is the question the project exists to answer; everything
above either feeds it (H0003, M-01, E0002 costs, H0012 labels) or
de-risks it (H0005r). Dependencies: M-01 + continued market
accumulation (≈ March 2027 at current accrual). Effort: large. Impact:
decisive in either direction — a confirmed after-cost edge gates
Phase 6; a well-powered null redirects the program to its research
outputs with a clear conscience.

**10. B-01 — cost-model backtest and paper-trading go/no-go.**
Objective: only if H0006-class results warrant it: backtest engine
runs with E0002-derived costs; explicit go/no-go against the CLAUDE.md
Phase 6/7 gates.
Why: the roadmap's endpoint decision, deliberately last and
deliberately conditional. Dependencies: H0006 confirmed. Effort:
medium. Impact: decision-enabling; skipped without regret if H0006
returns a null.

### Calendar (Aug 2026 → Jul 2027)

- **Aug:** E0002; H0012r (from Aug 19).
- **Sep:** H0017 pre-registration + execution.
- **Oct:** H0005r; H0003 pre-registration prep.
- **Nov:** H0003 execution (data permitting); E0003.
- **Dec:** H0003 follow-through; Phase-4 feature work *only* for what
  H0003/M-01 name; merged H0014r/H0015b pre-registration drafting.
- **Jan 2027:** H0014r/H0015b execution.
- **Feb:** M-01 baselines + calibration report.
- **Mar–Apr:** H0006.
- **May–Jun:** B-01 if warranted; else program review + H0004 design
  with two seasons of forecast data in hand.

### Parked (explicitly, with reasons)

- Physical occurrence-mechanism study ("H0016"): no consumer.
- Hurricane/event-market research: data accumulating passively;
  revisit when a season of settled events exists.
- H0004 conditioning studies: need contrasting seasons; 2027.
- Any new collection source, station expansion, or infrastructure
  beyond named consumers: not justified.
