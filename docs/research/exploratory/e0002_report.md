# E0002 — Weather-market liquidity & microstructure (exploratory)

> **EXPLORATORY ONLY — NOT PREREGISTERED — NOT CONFIRMATORY.**
> Descriptive statistics only. Results may generate future candidate
> ideas; they cannot update H0019/H0020, cannot justify registering
> HX-A, and support no trading claim. Effect sizes are descriptive;
> no inference is controlled for multiple comparisons; nothing here is
> "supported", "validated", or "profitable".

Run 2026-07-27 (as-of `2026-07-28T00:00:00` UTC) at commit of this file's
introduction. Source spec: `docs/research/2026-07-22-research-roadmap.md`
project card 1 ("characterize spreads, depth, quote presence, trade
volumes, and time-of-day liquidity across the collected weather series"),
plus the H0017 closeout's stale-quote measurement note.

Reproduce:

```
uv run kalshi-weather research exploratory e0002 \
  --as-of 2026-07-28T00:00:00 --json
```

Machine-readable headline summary: `e0002_summary.json` (compact,
aggregates only). The command is read-only, refuses any row inside the
reserved confirmatory windows (2026-08-12 → 2026-10-06), and enforces
the injected as-of cap.

## Frame

- Sources: `orderbook_snapshots` (62,694 rows ≤ as-of), `trades`
  (306,263 rows), production environment as the primary frame; demo and
  legacy (NULL-environment) rows appear only in the all-environments
  robustness variant.
- 2,248 tickers with books, 2,026 with trades, 82 series families —
  all weather (temperature thresholds, multi-strike temperature scanners,
  rain, tornado, hurricane families).
- Availability contract: ingestion (`captured_at`/`executed_at` ≤ as-of).
  No labels, no outcomes, no forecasts are read — publication vs
  ingestion contrast is not scientifically meaningful here.
- Close-time guard: not required — no decision boundary depends on
  `close_time`; time-of-day grouping uses row timestamps only.
- Leakage audit at run time: PASS (0 findings, 5 production invariants
  clean).

## Findings (descriptive patterns, production frame)

1. **Quote presence.** 67.2% of stored books are two-sided, 25.9%
   ask-only, 6.3% bid-only, 0.7% empty. Temperature-threshold families
   are slightly less two-sided (62.5%) than the pooled frame.
2. **Spreads are tight at the median, heavy-tailed at the top.**
   Two-sided median spread 2¢ (p25 1¢), but p90 25¢ and mean 10.5¢.
   The tail is family-structured: single-threshold temperature families
   quote 1–2¢ medians in every city, while multi-strike scanner families
   (KXTEMP\*H) and severe-weather families run 4–51¢ medians. City is
   *not* a driver: leave-one-city-out median spread is 2¢ for all 21
   exclusions (two-sided rate 0.669–0.679).
3. **Displayed size at the touch is small.** Depth at best bid: median
   17 contracts (p90 286); at best ask: median 9 (p90 200). Whole-book
   displayed depth is much larger (median ~4,130 contracts) but sits
   away from the touch. Descriptively: one-to-few-contract executions
   look routinely absorbable; size beyond tens of contracts would
   walk the book.
4. **Trading is active but concentrated.** 306k trades / 7.07M
   contracts in the archive. Median traded ticker-day: 119 contracts
   (p10 4, p90 2,230). 26.2% of quoted ticker-days print zero trades
   overall; 10.5% within temperature families.
5. **Time-of-day (trades; well-covered).** Trade counts peak 19–23 UTC
   (US afternoon/evening) at roughly 3× the 05–09 UTC trough — activity
   follows the US day, with a secondary overnight tail.
6. **Time-of-day (books; NOT yet reliable).** Production book coverage
   is only ~1.2 days old (cutover 2026-07-26 19:52 UTC) and the
   2026-07-27 upstream outage removed ~13:00–19:00 UTC entirely, so
   book-side hourly patterns (e.g. the apparent 08–09 UTC two-sided
   peak) are coverage artifacts until more production days accrue.
   The trade archive does not share this hole (backfilled history).
7. **Stale-quote note (H0017 closeout follow-up).** Gaps between
   consecutive *stored* books: median ~10.5 min ≈ the poll cadence,
   p90 ~28 min. Caveat: stored books are content-hash deduplicated, so
   a gap conflates "unchanged across polls" with "not polled"; with the
   current cadence this bounds quote-refresh resolution at ~10 min.

## Robustness

All predetermined; all reported; none iterated:

| Variant | Headline effect |
|---|---|
| All environments (adds demo + legacy) | median spread 2¢ → 3¢; conclusions unchanged |
| Temperature families only | median 2¢; zero-trade share drops to 10.5% |
| Leave-one-city-out (21 cities) | median spread 2¢ in every case |
| Missingness treatment | quoted-but-untraded ticker-days reported as a rate (26.2%), never dropped |

No variant contradicts the pooled description. The one unstable block is
book-side time-of-day (finding 6) — insufficient production coverage,
flagged rather than smoothed.

## Multiple-comparison inventory

Planned by the E0002 card: presence/spread/depth/volume/time-of-day,
each overall + by family + by city + by hour, plus the four robustness
variants and the stale-quote note. Nothing was added mid-analysis; no
thresholds were selected; no correction is applied because no inferential
claim is made.

## Limitations

- Production book history is ~1.2 days; every book-side statistic will
  sharpen substantially within weeks.
- Trade archive depth varies by family (older backfill for some series).
- City codes are reported verbatim from tickers (legacy "NY" vs "NYC"
  are not unified).
- Book-change resolution is bounded by the ~10-min poll cadence.
- Depth is *displayed* depth only; hidden liquidity is unobservable.

## Gatekeeper reading (descriptive, not a decision)

The "untradeably thin" downgrade scenario from the roadmap card is
**not descriptively supported at small size**: single-threshold
temperature markets show 1–2¢ median spreads with routine two-sided
presence and daily prints. The binding constraints observed are touch
depth (median 9–17 contracts) and participation gaps (10–26% zero-trade
ticker-days). These are inputs for the eventual cost model (B-01), not
evidence of edge, profitability, or tradability of any strategy.

## Future measurement input: book-continuity classifier

The stored-book gap measurement above (finding 7) conflates "unchanged
across polls" with "not polled". A read-only classifier now exists to
resolve that at the interval level for the next remeasurement:
`research/book_continuity.py` + `kalshi-weather research book-continuity`.
It classifies each interval between consecutive stored books for an exact
ticker, using the per-ticker poll ledger (`market_poll_attempts`,
prospective from 2026-07-27T02:52:20Z, ADR 0020) and collector-run
lineage, into: `CHANGED_AFTER_SUCCESSFUL_POLL`,
`UNCHANGED_CONFIRMED_BY_SUCCESSFUL_POLL` (proof: a successful same-ticker
order-book poll with outcome `succeeded_unchanged`/`deduplicated=True`
and matching environment completed inside the interval),
`FAILED_POLL_INTERVAL`, `NO_DIRECT_POLL_EVIDENCE`, `COLLECTION_GAP`,
`AMBIGUOUS_PROVENANCE`, `LEGACY_PRE_POLL_LEDGER`, and
`OPEN_INTERVAL_NOT_YET_CLASSIFIABLE`, each with a confidence level
(HIGH/MEDIUM/LOW/UNKNOWN) and a stable reason code. It fails closed:
intervals without proof are never called unchanged, and pre-ledger
history keeps the original conflation caveat permanently (no synthetic
backfill).

**Integration contract for the 14-day remeasurement:** replace the
single "stored-book change gap" distribution with duration decomposed
into unchanged-confirmed, changed-book, failed-poll, no-evidence,
collection-gap, and legacy seconds per ticker and in aggregate. The
numerical findings in this report remain the checked-in as-of
2026-07-28 baseline and are deliberately NOT restated using the
classifier.

## Candidate generation

Outcome: **COLLECT_MORE_DATA — no new hypothesis draft.** The measured
quantities (spread/depth/volume) are cost-model inputs, and every
apparent conditional pattern (hourly book liquidity) is currently
confounded by coverage. One measurement follow-up, not a hypothesis:
re-run this command after ≥14 production days (≥ 2026-08-09, still
before the reserved windows) to obtain a stable time-of-day book
profile. H0009 (liquidity-conditioned deviations) remains Tier 2 in
`docs/research/hypothesis_backlog.md`, gated on the frozen program's
outcomes — nothing here changes that.
