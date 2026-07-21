# AMENDMENT-20260721-H0007-fee-verification

**Second pre-execution amendment to `PREREG-20260721-H0007-bound-violations.md`,
made 2026-07-21, before H0007 execution.** No H0007 price analysis has been
run. No bounded-strike price results have been inspected. This amendment
documents an exhaustive attempt to verify Kalshi's official fee schedule for
weather markets, per `AMENDMENT-20260721-H0007-pre-execution.md`'s Finding 5.
It resolves several sub-questions with authoritative, first-party evidence,
and **leaves the experiment blocked** on the one question it could not
resolve: the exact numeric fee coefficient.

This document is additive, per this project's established discipline
(`RESEARCH.md`, and the prior amendment's own precedent): it does not rewrite
`AMENDMENT-20260721-H0007-pre-execution.md` or the original pre-registration.
No H0007 hypothesis, threshold, primary cell, `p_bound` value, or bootstrap
seed is altered.

## 1. Exhaustive source search

Every source below was checked on **2026-07-21** (timestamps recorded to the
minute where retrieval succeeded). "Reachable" means the request returned
content from the target host; several official Kalshi hosts returned
`HTTP 429` (rate-limited / bot-checkpointed), which counts as unreachable for
this exercise even though the host exists.

| # | Source | URL | Method | Time (UTC) | Reachable? | HTTP | Specifies weather-market fees? | Conflicts with another source? |
|---|---|---|---|---|---|---|---|---|
| 1 | Kalshi official fee schedule PDF | `kalshi.com/docs/kalshi-fee-schedule.pdf` | WebFetch | ~15:50, ~16:05 (2 attempts this session; 2 more in the prior amendment) | **No** | 429 (both attempts) | Would be authoritative for the exact coefficient; content never retrieved | N/A — no content obtained |
| 2 | Same PDF | same URL | direct `curl` | ~15:52 | **No** | Vercel bot-detection HTML challenge page returned instead of the PDF (soft-429) | — | — |
| 3 | Kalshi official fee schedule page | `kalshi.com/fee-schedule` | WebFetch | ~15:53 | **No** | 429 | — | — |
| 4 | Kalshi Help Center — Fees | `help.kalshi.com/trading/fees` | WebFetch | ~15:55 | **Yes** | 200 | Qualitatively yes ("a transaction fee on the expected earnings on the contract"); defers to the PDF for the formula. States generally: **"Some markets have fees that are different from those of other markets."** | Apparent tension with #7/#8/#9 below, **resolved** — see §2 |
| 5 | CFTC self-certification filing (2022) | `cftc.gov/sites/default/files/filings/orgrules/22/09/rule091222kexdcm003.pdf` | WebFetch + local zlib decompression of PDF streams | ~15:58 | File retrieved but **not usable** — scanned/non-OCR'd document, no extractable text; also a 2022 filing, potentially superseded | 200 (file), content unreadable | Unknown — could not read | — |
| 6 | Kalshi official OpenAPI specification | `docs.kalshi.com/openapi.yaml` | direct `curl` | 16:09:45 | **Yes** | 200 | Defines the `FeeType` enum (`quadratic`, `quadratic_with_maker_fees`, `flat`) and the `fee_multiplier` field on `Series`/`Event`; explicitly states: *"Fee structures can be found at https://kalshi.com/docs/kalshi-fee-schedule.pdf. 'quadratic' is described by the General Trading Fees Table..."* — does **not** embed the numeric coefficient itself. | No conflict — consistent with #7-9 |
| 7 | This project's own previously-captured, verbatim raw Kalshi API response | `GET /series?category=Climate+and+Weather` (production, unauthenticated) | Already stored in `raw_api_payloads` (this project's own append-only capture); re-read, not re-fetched | received_at `2026-07-21T13:45:49Z` (original collection) | **Yes** | 200 (as originally recorded) | **Directly yes** — every one of 289 "Climate and Weather" series objects, including `KXHIGHNY` and `KXLOWTNYC` specifically, carries `fee_type: "quadratic"`, `fee_multiplier: 1` | No conflict — resolves #4's general statement (see §2) |
| 8 | Live Kalshi API | `GET /series/fee_changes?series_ticker=KXHIGHNY&show_historical=true` | direct `curl` (production, unauthenticated, per this project's existing `KalshiClient` convention for public data) | ~16:02 | **Yes** | 200 | `series_fee_change_arr: []` — no historical or scheduled fee change for KXHIGHNY | No conflict |
| 9 | Live Kalshi API | `GET /series/fee_changes?series_ticker=KXLOWTNYC&show_historical=true` | direct `curl` | ~16:02 | **Yes** | 200 | `series_fee_change_arr: []` — same, for KXLOWTNYC | No conflict |
| 10 | Live Kalshi API | `GET /events/fee_changes?show_historical=true` (unfiltered) | direct `curl` | ~16:04 | **Yes** | 200 | `event_fee_changes: []` — **no event-level fee override exists anywhere on the exchange**, so no KXHIGHNY-*/KXLOWTNYC-* event has one | No conflict |
| 11 | Kalshi official API docs — Get Series Fee Changes reference page | `docs.kalshi.com/api-reference/exchange/get-series-fee-changes` | WebFetch | ~16:06 | **Yes** | 200 | Confirms the endpoint shape; does not disclose the formula (consistent with #6) | No conflict |
| 12 | Kalshi official API docs — Fee Rounding | `docs.kalshi.com/getting_started/fee_rounding` | WebFetch | ~16:07 | **Yes** | 200 | States the **rounding mechanics** precisely (see §2.4) — does not state the base coefficient | No conflict — adds detail, doesn't contradict anything |
| 13 | Kalshi official API docs — general fees overview (guessed path) | `docs.kalshi.com/getting_started/fees` | WebFetch | ~16:07 | **No** | 404 — page does not exist at this path | — | — |
| 14 | Kalshi official API docs sitemap | `docs.kalshi.com/sitemap.xml` | WebFetch | ~16:08 | **Yes** | 200 | Confirms the **exhaustive** list of fee-related pages under `docs.kalshi.com`: the two `fee_changes` API references, `fee_rounding`, and `margin-rest/fees/get-fee-tiers` — no hidden formula page was missed | No conflict |
| 15 | Kalshi official API docs — margin/perps fee tiers | `docs.kalshi.com/margin-rest/fees/get-fee-tiers` | WebFetch | ~16:08 | **Yes** | 200 | **Not applicable** — explicitly scoped to perpetual futures/margin products, a different product line from standard event-contract weather markets; ruled out, not silently ignored | No conflict (out of scope) |
| 16 | Wayback Machine archive of the PDF | `web.archive.org/web/20260218003606/https://kalshi.com/docs/kalshi-fee-schedule.pdf` | Availability API + CDX index query | ~16:00-16:03 | Archive pointer reachable; **content deliberately not used** | 200 (pointer), 200 (last successful archived fetch) | Would specify weather-market fees, but is a **stale February 2026 snapshot** — see §2.5 | Not used as authoritative; would risk citing a superseded schedule |
| 17 | Third-party secondary summaries (marketmath.io, predictreport.io, deadspin.com, predictionhunt.com, sailgp.com, pm.wiki, botforkalshi.com, predscope.com, tech-insider.org, revenuememo.com, betherosports.com) | various | `WebSearch` | ~15:49, ~16:05 | Reachable, but **not used as authoritative** per this task's explicit instruction | — | Multiple sources converge on `ceil(0.07 × contracts × P × (1-P))` (taker) / `~0.0175` (maker) — one source explicitly labels this **"the February 2026 schedule"**, i.e. not the confirmed-current July 2026 version | Not treated as a source of truth; recorded for transparency only |

## 2. Applicability findings

### 2.1 Standard vs. special schedule

**Verified, uniform, standard schedule.** Every one of the 289 series in the
"Climate and Weather" category — including both `KXHIGHNY` and `KXLOWTNYC`,
the two series underlying every H0007 market — carries `fee_type: "quadratic"`
and `fee_multiplier: 1` (source #7). `fee_multiplier = 1` is the Kalshi
OpenAPI spec's own default/baseline multiplier value (source #6); no weather
series shows any other value. **No** weather series uses `fee_type:
"quadratic_with_maker_fees"` or `"flat"`.

### 2.2 Maker/taker distinction

`fee_type: "quadratic"` (not `"quadratic_with_maker_fees"`) is documented
(source #6) as governed entirely by the "General Trading Fees Table" — the
maker-fee section of Kalshi's schedule applies only to series whose
`fee_type` is `quadratic_with_maker_fees`. Since no weather series uses that
type, **there is no documented separate maker discount for these markets**
— every fill (resting or crossing) is governed by the same table. This
resolves the amendment's earlier open question about maker/taker
applicability for the weather category specifically, though the table's
actual numbers remain unread (§3).

### 2.3 Fees differing across series

**Resolved, not merely assumed.** `help.kalshi.com/trading/fees` (source #4)
states generally that "some markets have fees that are different from those
of other markets" — a platform-wide statement, presumably referring to
special-event categories mentioned in the same article (elections, awards
ceremonies, major sporting championships). Direct, first-party API data
(source #7) confirms this does **not** apply within the weather category:
all 289 series are uniform. No conflict exists between these two sources —
the general statement is true of the platform, and is simply inapplicable to
the specific series sampled here.

### 2.4 Rounding rules

**Partially verified.** `docs.kalshi.com/getting_started/fee_rounding`
(source #12), read directly, states:

> "Round trade fee up to the nearest $0.0001" ... the exchange then "floors
> `balance_change` toward negative infinity to the user's target balance
> precision" ($0.01 for non-direct members, $0.0001 for direct members),
> charging the difference as a rounding fee. Net fee = trade fee + rounding
> fee − rebate (always ≥ $0.00). A fee accumulator tracks rounding
> overpayment across all fills of one order; once it exceeds $0.01, a $0.01
> whole-cent rebate is issued automatically.

This is a real, materially more granular mechanism than the simple
"round up to the nearest cent" approximation `scripts/h0007_fees.py`'s
illustrative test fixture uses (that fixture was always explicitly labeled
non-authoritative — see its own docstring). It is also a **multi-fill,
per-order accumulator**, which does not map cleanly onto H0007's unit-stake,
single-fill design; for a standard ("non-direct member") account making one
fill, the net effect settles to whole-cent precision, consistent with (but
now more precisely justified than) the fee function's existing rounding
behavior. Whether this research's fee assumption should model a "direct" or
"non-direct" member account is **not independently confirmed** and is
recorded as an open, minor sub-question — not a blocker on its own, since
either tier's fee ultimately nets to a whole-cent-equivalent result for a
single unit-stake fill, but flagged for completeness.

### 2.5 Minimums, maximums, settlement-time fees, exercise/expiration fees

**Not found in any reachable source.** No page checked (sources #4, #6, #11,
#12, #14, #15) states a fee floor/cap distinct from the rounding mechanism
in §2.4, nor any settlement-time, exercise, or expiration fee for event
contracts (the CFTC filing, source #5, might address this but was
unreadable). This project's own already-collected data corroborates the
absence of a *settlement* fee specifically at the platform level, but not
authoritatively for this exact question — recorded as unconfirmed, not
assumed absent.

## 3. Consistency check

**No disagreement exists among the authoritative sources actually read.**
Sources #6, #7, #8, #9, #10, #11, #12, #14, #15 are mutually consistent —
each covers a distinct facet (type/multiplier fields, override history,
rounding mechanics, page inventory) with no contradiction. The one apparent
tension (source #4's general "fees differ by market" statement) is resolved,
not left unresolved, by direct evidence (§2.3).

**The one substantive fact that could not be established from any
authoritative source in this environment is the numeric coefficient itself**
in the "General Trading Fees Table" (the quantity third-party sources call
"0.07," explicitly for an outdated "February 2026" version per one such
source — source #17). The sole document that states it,
`kalshi.com/docs/kalshi-fee-schedule.pdf`, returned `HTTP 429` on every
attempt (sources #1-3), and its most recent successfully-crawled archive
(source #16) predates the confirmed "July 2026 (7.7.26 Update)" revision —
using it would mean citing a schedule Kalshi itself has since superseded.
Per this task's explicit instruction, **this is reported as an
unresolvable-from-here fact, not filled in with a guess.**

## 4. Configuration

**Not fully populated — the coefficient is not uniquely determined.** Per
this amendment's own governing instruction ("if and only if every required
parameter is uniquely determined, populate the fee configuration"), the
`TEMPLATE-H0007-manifest.json` `config.fee_schedule` block has been updated
to record every parameter that **is** now verified (`fee_type`,
`fee_multiplier`, the absence of series/event overrides, and the partial
rounding-rule finding), each with its exact source, retrieval timestamp, and
(where applicable) content hash — while `formula`, `constant`,
`schedule_effective_date`, `min_max_fee_behavior`, and
`source_content_hash_or_archive` remain explicit placeholders. `status`
reads `"STILL BLOCKED"`. No field was filled with an inferred, estimated, or
third-party-sourced value.

`scripts/h0007_fees.py` is **unchanged** — `contract_fee_cents` still
refuses to compute against an unverified `FeeConfig`. This amendment adds no
new code; it only adds verified provenance to the manifest template.

## 5. Validation

- [x] All existing fee tests still pass — `PYTHONPATH=src pytest -q
      tests/unit/test_h0007_fees.py` → **10 passed** (unchanged from the
      prior amendment; no code was modified).
- [x] Hand calculations match — unchanged, since the illustrative test
      fixture and function were not modified.
- [x] Configuration hash recorded — the OpenAPI spec's content hash
      (`f26ddccd92e5805f935ed3de1effd9f9a99b4a3bba6e8cb563cd2258722826ce`)
      and the raw `/series` payload's existing content hash
      (`533079a18e233d72efe71bdd2f115dc25e1d22dca546c2405e1f9513e5714a6e`)
      are both recorded in the manifest template as the provenance for
      every *verified* field. No hash is recorded for the coefficient,
      because no document containing it was retrieved.
- [x] Manifest updated — `TEMPLATE-H0007-manifest.json`'s `fee_schedule`
      block and `preregistration.fee_verification_amendment` pointer, as
      described in §4.
- [x] No H0007 price data was inspected; no bounded-strike quote behavior
      was examined. The only data touched: Kalshi's own already-collected
      series metadata (`raw_api_payloads`, source #7) and fresh, read-only
      public API calls to fee-related endpoints (sources #8-10) — none of
      which return market price, quote, or trade data.
- [x] H0007 has not been executed. No `EXP-*-H0007-*` result artifacts
      exist.

## 6. Final decision

**STILL BLOCKED.**

**Precisely what is missing**: the numeric coefficient(s) in the "General
Trading Fees Table" of Kalshi's official fee schedule
(`kalshi.com/docs/kalshi-fee-schedule.pdf`, currently titled "Fee Schedule
for July 2026 — 7.7.26 Update"), and confirmation of the same for the
rounding-precision member-tier question in §2.4. This single document was
unreachable from every method attempted (direct fetch, `WebFetch`, and — for
completeness — checking whether a usable cached copy existed anywhere) in
this environment, on **2026-07-21**. Everything else needed to configure
H0007's fee treatment — fee type, fee multiplier, absence of series/event
overrides, and the shape of the rounding mechanism — **is** now verified
against Kalshi's own authoritative sources and recorded in the manifest.

**What would unblock this**: a successful fetch of
`kalshi.com/docs/kalshi-fee-schedule.pdf` (or an authoritative equivalent —
e.g. a live order-preview response that discloses the actual fee charged,
which this project has not attempted here since it was out of this task's
scope) from an environment or method that is not rate-limited by Kalshi's
bot protection on that specific host.

> **Resolved (2026-07-21, same day, before execution).** The user supplied
> that exact document directly —
> `docs/research/experiments/AMENDMENT-20260721-H0007-fee-verified.md`
> verifies it against its own 42 published worked examples (zero
> discrepancies) and freezes the coefficient. **No pre-registration blocker
> remains.**
