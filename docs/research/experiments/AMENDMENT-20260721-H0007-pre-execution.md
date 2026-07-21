# AMENDMENT-20260721-H0007-pre-execution

**Pre-execution amendment to `PREREG-20260721-H0007-bound-violations.md`,
made 2026-07-21, before H0007 execution.** No H0007 price analysis has been
run. No bounded-strike price results have been inspected. This amendment
resolves the ambiguities identified by the independent institutional
pre-registration audit (recorded below) and does not alter H0007's original
economic hypothesis, decision thresholds, primary cell, `p_bound` value,
bootstrap seed, or confidence-interval rules — those remain frozen exactly
as `PREREG-20260721-H0007-bound-violations.md` and `HYPOTHESES.md` state
them.

The original pre-registration document is **not rewritten**. This amendment
resolves ambiguity by addition, versioned and dated, per the same
never-silently-edit-a-frozen-record discipline `RESEARCH.md` already applies
to research plans and settlement labels.

## Audit record

Institutional review recommendation: **APPROVE WITH REQUIRED CHANGES**.
Six findings (F1-F6), summarized here for traceability; full text in the
review transcript this amendment responds to.

- F1 — inconsistent episode definition (§5 vs. §9 of the pre-registration).
- F2 — ambiguous opportunity-day denominator.
- F3 — persistence filter mitigates the weather bound, not the quote.
- F4 — "consecutive candle" undefined under gapped emission.
- F5 — fee-constant timing permits a post-hoc choice.
- F6 — issuance-availability latency not reflected in permissible conclusion
  wording.

---

## Finding 1 — Canonical episode definition (resolves F1)

**Frozen definition, superseding both prior readings in §5/§9 of the
pre-registration:**

> An H0007 opportunity episode is a maximal run of candles that are both
> (a) entry-eligible per §4 of the pre-registration and (b) satisfy the
> fee-net residual-mass condition (§9 of the pre-registration: `residual_mass_cents
> > fee_cents_for_one_contract_at_this_price`).

Rules:

- Only fee-net residual-mass episodes are counted as opportunities and enter
  the P&L aggregate. A candle that is entry-eligible (the weather bound
  holds) but has **no** fee-net residual mass is not an opportunity episode
  and generates no hypothetical trade — the raw (non-fee-net)
  `has_residual_mass` flag from §5 remains available only for descriptive
  "how often is there any gap at all" reporting, never for the primary
  decision or P&L.
- Entry occurs at the episode's first qualifying candle (first candle
  satisfying both (a) and (b)).
- Exit occurs at the first subsequent candle that no longer satisfies the
  fee-net residual-mass condition, or at the market's `close_time` if no
  correction occurs first.
- An episode may not cross `market_ticker`, `strike_type`/threshold,
  `variable`, `target_date`, or settlement-day boundaries — each is a
  distinct market with its own candle sequence by construction, so this is
  a statement of scope, not a new filter.

This single definition applies uniformly to persistence (Finding 3),
time-to-correction, opportunity counts, and P&L aggregation.

## Finding 2 — Opportunity-day denominator (resolves F2)

**Frozen denominator**, matching `HYPOTHESES.md`'s own "≥5% of settlement
days" wording:

> The primary opportunity-day frequency denominator is all settlement days
> satisfying the pre-registration's valid-coverage requirements for the
> primary `tmax_f` cell (i.e., all days *not* excluded under §7's
> missing-data rule) — **not** conditioned on the day containing any
> entry-eligible candle or any residual-mass episode.

Rules:

- Zero-opportunity days (valid coverage, no qualifying episode) remain in
  the denominator and count as non-opportunity days.
- Days without valid required data (§7's exclusion — e.g.
  `settlement_label_status == "missing_source_data"` for the whole day, or
  no candle archive) are excluded from the denominator and reported
  separately as `days_missing_data`, exactly as the pre-registration already
  specified.
- The numerator is the count of valid-coverage days containing ≥1 qualifying
  opportunity episode (Finding 1's definition, which already requires the
  Finding 3 persistence condition to hold before an episode qualifies).
- The day-clustered bootstrap (pre-registration §6) resamples from this same
  valid-coverage day pool, **including** zero-opportunity days — omitting
  them would upwardly bias the resampled frequency.

## Finding 3 — Persistence applies to the quote-level opportunity (resolves F3)

**Frozen primary day-level opportunity requirement**, superseding
pre-registration §4.5's weather-bound-only reading:

> The fee-net residual-mass condition must hold for at least two consecutive
> qualifying one-minute candles (Finding 4's adjacency rule) within the same
> opportunity episode (Finding 1) before that episode counts toward
> opportunity-day frequency or P&L.

Both candles must independently satisfy, at minimum:

- every entry-eligibility condition in pre-registration §4 (1-8), including
  the logical weather-bound condition and the non-crossed-quote requirement;
- the fee-net residual-mass condition itself.

A weather bound that persists for many candles while the quote corrects
immediately (fee-net residual mass true for only one candle) is **not** a
persistent opportunity and does not qualify — this directly implements
`HYPOTHESES.md` H0007's own pre-registered failure mode #1 ("require the
quote to persist across ≥2 consecutive snapshots"), which the original §4.5
wording did not actually enforce.

## Finding 4 — Definition of consecutive candles (resolves F4)

**Frozen definition:**

> Two candles are consecutive only when their `period_end` timestamps differ
> by exactly one minute (the pre-registered `candle_resolution_minutes`,
> currently 1) for the same `market_ticker` and `period_interval_seconds`.

Rules:

- A gap greater than one minute breaks the episode — the run restarts at
  the next qualifying candle rather than treating the gap as a continuation.
- Adjacent **stored rows** that are more than one minute apart (Phase 7A
  established the candlestick API emits no row for many idle minutes,
  especially on sparse "between"-strike markets) are **not** consecutive
  under this rule, even though they are adjacent in the database.
- Missing candles are never synthesized, forward-filled, or treated as an
  unchanged quote — a gap is a gap, and the episode does not span it.
- This rule is applied identically to every market, liquid or illiquid.

This is deliberately conservative: the data cannot prove a quote persisted
during an interval Kalshi's own API chose not to emit a candle for, so this
protocol does not assume it did. The consequence — sparse markets may fail
the persistence criterion more often than liquid ones even when their next
*emitted* candle still shows residual mass — is a known, accepted property
of this design, not an oversight; it is symmetric with Finding 3's intent
(only count what is actually observed to persist).

## Finding 5 — Fee formula and constant (resolves F5): **BLOCKED**

Kalshi's official fee-schedule sources were sought before any H0007 price
analysis, per the pre-registration's own requirement. Full attempt log:

| Source attempted | Method | Result |
|---|---|---|
| `kalshi.com/docs/kalshi-fee-schedule.pdf` (titled "Fee Schedule for July 2026 — 7.7.26 Update" per search results — the primary, current, first-party document) | `WebFetch` (×2) | **HTTP 429**, both attempts |
| `kalshi.com/docs/kalshi-fee-schedule.pdf` | direct `curl` | Blocked by a Vercel bot-detection checkpoint (returns an HTML challenge page, not the PDF) |
| `kalshi.com/fee-schedule` (first-party, non-PDF) | `WebFetch` | **HTTP 429** |
| `help.kalshi.com/trading/fees` (first-party help center) | `WebFetch` | **Reachable.** Confirms the fee mechanism qualitatively ("a transaction fee on the expected earnings on the contract"; maker fees charged only on execution, never on cancellation) and states explicitly: **"Some markets have fees that are different from those of other markets"** — but does not itself state the formula, deferring to the blocked PDF ("the complete Fee Schedule, and the math behind the fees, are posted at the bottom of our website"). |
| CFTC self-certification filing (`cftc.gov/.../rule091222kexdcm003.pdf`, 2022) | `WebFetch` + local decompression | Reachable as a file, but is a **scanned/non-OCR'd document** — no machine-extractable text; formula not recoverable from this source in this environment. Also would be a **2022** filing, potentially superseded by the July 2026 schedule found above. |
| Third-party secondary summaries (marketmath.io, predictreport.io, deadspin.com, predictionhunt.com, sailgp.com) | `WebSearch` | Multiple independent sources converge on a `ceil(0.07 × P × (1−P) × 100)` per-contract taker formula, ~1/4 that for makers, "applied uniformly across categories including weather" per one source — but these are **not Kalshi's own primary documentation**, and this project's own rules (`CLAUDE.md` API integration rules; the precedent in `docs/API_VERIFICATION.md`) require grounding external assumptions in official documentation or a verified live/official reference, never a secondary paraphrase. |

**Conclusion: the fee constant cannot be verified against Kalshi's own
primary schedule from this environment.** This is the same class of failure
this project already documented for `docs.kalshi.com` in
`docs/API_VERIFICATION.md` §3 ("unreachable from this environment...
consistently... specific to the docs host"). Critically, `help.kalshi.com`'s
own first-party statement that "some markets have fees that are different
from those of other markets" means a uniform constant **cannot safely be
assumed** without the primary schedule confirming weather markets fall under
the general case — exactly the "varies across sampled markets" condition
the pre-registration's Finding 5 requirement says must block execution
rather than guess.

**Per the pre-registration's own instruction: fee configuration is marked
BLOCKED. H0007 must not execute until it is resolved** — by a successful
fetch of the primary schedule from an environment that can reach it, or an
authenticated/live-order-preview verification (mirroring
`docs/API_VERIFICATION.md`'s own precedent of falling back to live behavior
when documentation is unreachable), whichever this project's operator
performs next.

**What was built regardless, so no further code work blocks on this:** a
tested, parameterized fee function, `scripts/h0007_fees.py`
(`contract_fee_cents`), implementing the corroborated formula *shape*
(`ceil(multiplier × P × (1−P) × contracts)` cents, rounded up — every source
consulted agrees fees round up, never down). Its `FeeConfig.verified` flag
defaults to requiring explicit verification; **the function raises rather
than compute** against an unverified config — this is the enforcement
mechanism keeping H0007 blocked on this step in code, not only in
documentation. `tests/unit/test_h0007_fees.py` (10 tests) verifies the
formula's arithmetic (hand-calculated cases at 1¢, 10¢, 50¢, 99¢; ceiling
rounding; p(1−p) symmetry; contract-count scaling; the refusal-to-compute
gate) using an explicitly-labeled illustrative constant — **these tests
assert nothing about Kalshi's actual current fee**, only that the function
is mathematically correct for whatever verified constant is eventually
supplied.

> **Follow-up (2026-07-21, same day, before execution).** A dedicated
> exhaustive-source-search pass,
> `docs/research/experiments/AMENDMENT-20260721-H0007-fee-verification.md`,
> confirms `fee_type="quadratic"`/`fee_multiplier=1` uniformly for both
> weather series (KXHIGHNY, KXLOWTNYC) via live Kalshi API data, and finds
> no series- or event-level fee override exists for either — resolving the
> *applicability* half of this finding. The numeric coefficient itself
> remains unverified for the reasons above; **this finding remains
> BLOCKED.**
>
> **Resolved (2026-07-21, same day, before execution).**
> `docs/research/experiments/AMENDMENT-20260721-H0007-fee-verified.md` — the
> user supplied Kalshi's official fee schedule PDF directly; the coefficient
> (0.07) is verified against 42 of the document's own published worked
> examples with zero discrepancies. **Finding 5 is now fully resolved.**

## Finding 6 — Permissible conclusion scope (resolves F6)

**Frozen interpretation boundary**, to be applied verbatim when H0007's
Results/Conclusion are eventually written:

> A confirmed H0007 result may support only: evidence of persistent
> quote-level mispricing relative to mechanically implied settlement
> bounds, net of the (verified, per Finding 5) pre-registered fee
> assumptions.

It may **not** claim: realized trading profit; guaranteed executable alpha;
fill probability; latency-adjusted capturability; production-trading
viability; or realized capacity.

**Latency caveat (must appear alongside any confirmed result):** an
observation's `issuance_time` is the CLI product's own publication
timestamp; real-world public availability (a human or system actually
reading and acting on the report) may lag it by an unmeasured, unquantified
delay. This experiment therefore evaluates quote behavior against the
platform's timestamped information reconstruction, not against a
demonstrated human- or system-level execution access at that instant. Any
future claim about *capturable* alpha (as opposed to *observed*
mispricing) requires a separate, explicitly pre-registered latency and
execution study — out of scope for H0007 as currently registered.

## Finding 7 — Versioning and audit trail (this section)

- Amendment version: `AMENDMENT-20260721-H0007-pre-execution`, v1.
- Audit recommendation responded to: APPROVE WITH REQUIRED CHANGES.
- Findings resolved: F1, F2, F3, F4, F6 (fully resolved, frozen above).
  F5 (fee schedule) **remains BLOCKED** — not resolved, by design; the
  amendment documents the block precisely rather than forcing a resolution
  with an unverified number.
- Git commit hash: recorded at commit time by the commit that includes this
  file (see the repository's own commit log — this document does not
  self-reference a hash it cannot yet know).
- Confirmation: **H0007 remained unexecuted at freeze time.** No price
  analysis was run; no bounded-strike quote behavior was inspected as part
  of producing this amendment. The only data-retrieval performed was (a)
  fetching Kalshi's public fee-schedule *documentation* (Finding 5 — an
  execution-assumption artifact, not H0007 outcome data) and (b) running the
  new fee-function's own unit tests against synthetic, hand-calculated
  inputs (never real price data).

**Unchanged, per the pre-registration audit's own constraint** (verified by
diffing this amendment against the original document): the 5% day-frequency
threshold; the two-consecutive-candle persistence *principle* (only its
scope — weather-bound vs. quote — was clarified, per Finding 3); the primary
`tmax_f` cell; the confidence-interval decision rules; the bootstrap seed
(20260721); the economic rationale (§2 of the pre-registration); `p_bound`
(1/1278, per H0002); and the confirmed/rejected/inconclusive thresholds
(§9 of the pre-registration).

## Finding 8 — Validation

Mechanical checks performed before declaring this amendment complete:

- [x] Only one episode definition remains operative (Finding 1 supersedes
      both prior readings; the pre-registration document itself is
      unedited, so a reader must consult this amendment — a visible pointer
      is added there, see below).
- [x] The opportunity-day denominator is unambiguous (Finding 2).
- [x] Zero-opportunity valid days remain included in the denominator and
      bootstrap pool (Finding 2).
- [x] Persistence evaluates fee-net quote residual mass, not merely the
      weather bound (Finding 3).
- [x] Exactly one-minute timestamp adjacency is enforced; gaps break
      episodes (Finding 4).
- [x] The fee configuration is versioned (`scripts/h0007_fees.py`'s
      `FeeConfig` dataclass) and its *unverified* status is committed
      explicitly — not silently deferred.
- [x] The fee function (and, by extension, any manifest built from it)
      cannot compute a result without an explicitly verified config —
      enforced by `contract_fee_cents`'s `ValueError` gate, tested.
- [x] Conclusion wording is constrained to quote-level mispricing net of
      fees (Finding 6) — not realized profit or capturable alpha.
- [x] H0007 has not been executed.
- [x] No result artifacts exist (`docs/research/experiments/EXP-*-H0007-*`
      does not exist; only `PREREG-`/`AMENDMENT-`/`TEMPLATE-` files do).

Full test/lint/type-check results are reported in the deliverables summary
accompanying this amendment.

---

**Pointer added to the original pre-registration and to `HYPOTHESES.md`,
without altering their existing text — see those files' own top-level
notes.**
