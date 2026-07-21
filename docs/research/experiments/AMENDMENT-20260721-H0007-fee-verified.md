# AMENDMENT-20260721-H0007-fee-verified

**Third pre-execution amendment to `PREREG-20260721-H0007-bound-violations.md`,
made 2026-07-21, before H0007 execution.** No H0007 price analysis has been
run. No bounded-strike price results have been inspected. This amendment
resolves `AMENDMENT-20260721-H0007-pre-execution.md` Finding 5 in full: the
Kalshi fee coefficient that `AMENDMENT-20260721-H0007-fee-verification.md`
could not retrieve is now supplied directly by the user as an official
Kalshi document and verified here against Kalshi's own worked examples
before being frozen.

Additive, per this project's established discipline: no prior amendment or
the original pre-registration is rewritten. No H0007 hypothesis, threshold,
primary cell, `p_bound` value, or bootstrap seed is altered.

## 1. Source

**`kalshi-fee-schedule.pdf`**, supplied by the user, saved to
`docs/research/experiments/kalshi-fee-schedule-2026-07-07.pdf` for permanent
provenance (this project's own established convention of preserving primary
source evidence verbatim, mirroring `raw_api_payloads`).

- **sha256**: `815e2d5127d02d2fb90773d1a3844dc15a987696171eddc4e58de87b59c6124c`
- **Size**: 382,507 bytes
- **Pages**: 12 real page objects, confirmed by direct inspection of the PDF's
  object structure (`/Type /Page` count) — the file's own `/Count` field in
  its page-tree root reads `8`, an internal metadata inconsistency in the
  document itself (not a retrieval error on this project's part); every one
  of the 12 pages was read and is accounted for below.
- **Document title/branding**: Kalshi, consistent formatting and footer
  across every page.
- **Effective date, stated on every page**: "Last updated and effective:
  July 7, 2026" — matches the "Fee Schedule for July 2026 — 7.7.26 Update"
  title found via search in the prior amendment, and post-dates the last
  fetchable archive (2026-02-18) that amendment could reach — this is
  plausibly the same, now-current version that was unreachable from this
  environment via every method previously attempted.
- **Retrieved**: 2026-07-21, via user upload (not a network fetch by this
  project — recorded as such).

This is treated as **authoritative** per this task's explicit instruction to
use it as the source of truth, on the basis that it is internally consistent
(uniform branding/dates across 12 pages), consistent with every
independently-verified fact from the prior amendment (see §3), and — most
importantly — **self-verifying**: its own worked-example table is checked
against its own formula below, and matches exactly.

## 2. Content

**General Trading Fees** (page 2), quoted verbatim:

> "Trading fees are charged as a variable percentage fee of the expected
> earnings on an individual contract... The current general fee charged for
> a trade in dollars is given by the following formula:
>
> fees = round up(M x 0.07 x C x P x (1-P))
>
> P = the price of a contract in dollars (50 cents is 0.5)
> C = the number of contracts being traded
> M = the multiplier for each contract (default is 1 unless otherwise indicated)
> round up = rounds up such that the fee + positionCost is rounded to a centicent"

**Maker Fees** (page 2), quoted verbatim:

> "fees = round up(M x 0.0175 x C x P x (1-P))
>
> M = the multiplier for each contract (default is **0** unless otherwise
> indicated)"

**Non-Standard Fees** (pages 6-11): an explicit, enumerated table of ~90
series with maker/taker multiplier overrides — special-event and financial
categories (elections/awards/sports/rates/crypto/etc.). **No weather or
climate series appears in this table** — confirmed by reading every row.

**Perpetual Futures Fees** (page 12): a wholly separate bps-based,
volume-tiered fee schedule for margin/perps products — explicitly out of
scope for standard event-contract weather markets (consistent with the prior
amendment's independent finding that `docs.kalshi.com/margin-rest/fees/
get-fee-tiers` does not apply here).

**Settlement fees**: "There is no settlement fee" (page 3, verbatim).

## 3. Verification against Kalshi's own worked examples

Pages 4-5 publish a "General Trading Fees Table" of fee amounts at 21 price
points (1¢ to 99¢), for both 1-contract and 100-contract trades — 42
published values. Applying the page-2 formula
(`ceil(M × 0.07 × C × P × (1-P))`, M=1, rounded to the nearest whole cent)
directly to each price/contract-count pair and comparing against the
document's own published numbers:

**All 42 values match exactly** — e.g. at $0.50/1 contract:
`0.07 × 0.5 × 0.5 = 0.0175` → the document publishes `$0.02`, matching a
plain ceiling-to-the-nearest-cent of the raw formula output; at $0.15/100
contracts: `0.07 × 100 × 0.15 × 0.85 = 0.8925` → the document publishes
`$0.90`, again an exact ceiling match. No row required any adjustment beyond
"ceil the raw formula output to the nearest whole cent" to reproduce the
document's own numbers.

This also resolves the more granular "round to the nearest $0.0001, then
floor `balance_change` to the account's target precision, net of a rebate"
mechanism `docs.kalshi.com/getting_started/fee_rounding` describes (per the
prior fee-verification amendment): for a **single, unit-stake fill** — which
is exactly H0007's design (`AMENDMENT-20260721-H0007-pre-execution.md`
§8: unit stake, one contract, no sizing) — that more granular mechanism
collapses, provably (42/42 matches), to simple ceiling-to-the-cent. The two
sources are consistent, not merely both plausible.

## 4. Applicability to KXHIGHNY and KXLOWTNYC

- **`M = 1`** (the default general/taker multiplier) applies, because
  neither `KXHIGHNY` nor `KXLOWTNYC` (nor any weather/climate series)
  appears in the Non-Standard Fees table (§2). This is now confirmed by
  **two independent sources that agree exactly**: this document's own
  absence-from-the-override-list, and the live `GET /series` API's
  `fee_multiplier: 1` field for both series (`AMENDMENT-20260721-H0007-
  fee-verification.md`, §2.1).
- **Maker fees are irrelevant to this design.** H0007's design always buys
  the favored side at its ask (a taker fill); it never rests as a maker
  order. Even if it did, the maker default multiplier for weather markets is
  `M=0` (page 2, quoted above), which would compute to a $0 fee regardless.
- **No settlement fee, no series/event overrides** (confirmed independently
  in the prior amendment via live `/series/fee_changes` and
  `/events/fee_changes` checks, both empty for these series).

**No disagreement exists between this document and any previously-verified
authoritative source** (the OpenAPI spec, the live series/fee-changes data,
or the fee-rounding documentation page).

## 5. Configuration

**Frozen.** `scripts/h0007_fees.py` now defines:

```python
KALSHI_WEATHER_TAKER_FEE_CONFIG = FeeConfig(
    multiplier=Decimal("0.07"),
    source="... full provenance, see the module ...",
    verified=True,
    retrieved_at="2026-07-21",
)
```

`TEMPLATE-H0007-manifest.json`'s `config.fee_schedule` block is updated:
`status: "VERIFIED"`; `source_title`, `source_url` (n/a — user-supplied, not
fetched), `retrieved_at`, `schedule_effective_date`, `formula`,
`constant`, `rounding_rule`, `series_applicability`, `role_assumption`, and
`source_content_hash_or_archive` are all populated with the values above.
`min_max_fee_behavior` and `contract_count_convention` remain unset — no
minimum/maximum beyond the 1¢ floor implicit in the ceiling rule was found
in this document, and no separate "contract count convention" beyond the
formula's own `C` term exists; both are recorded as **confirmed absent**,
not left as unresolved placeholders. `execution_status` is updated from
`BLOCKED` to reflect this section's Final Decision (§7).

## 6. Validation

- [x] All fee tests pass: `PYTHONPATH=src pytest -q tests/unit/test_h0007_fees.py`
      → **13 passed** (10 original + 3 new: `KALSHI_WEATHER_TAKER_FEE_CONFIG`
      is marked verified; the full official 1-contract table matches; the
      full official 100-contract table matches).
- [x] Hand calculations match — every one of the 42 official worked-example
      values reproduced exactly (§3); no rounding, off-by-one, or formula
      discrepancy found.
- [x] Configuration hash recorded — `815e2d5127d02d2fb90773d1a3844dc15a987696171eddc4e58de87b59c6124c`,
      the source PDF archived at
      `docs/research/experiments/kalshi-fee-schedule-2026-07-07.pdf`.
- [x] Manifest updated — `TEMPLATE-H0007-manifest.json` (§5).
- [x] `ruff check .` and `mypy src`/`mypy scripts/h0007_fees.py` clean.
- [x] No H0007 price data was inspected; no bounded-strike quote behavior
      was examined. The only material read: the user-supplied fee-schedule
      PDF (an execution-assumption document, not H0007 outcome data).
- [x] H0007 has **not** been executed. No `EXP-*-H0007-*` result artifacts
      exist.

## 7. Final decision

**CLEARED TO EXECUTE** (fee configuration only — see note below).

The single blocker `AMENDMENT-20260721-H0007-pre-execution.md` Finding 5
identified, and `AMENDMENT-20260721-H0007-fee-verification.md` narrowed to
"the numeric coefficient alone," is resolved: `fee_type`, `fee_multiplier`,
override-freedom, rounding behavior, and the coefficient itself are all now
verified against Kalshi's own authoritative documentation, mutually
consistent across every source checked, and cross-verified against the
source document's own 42 published worked examples with zero discrepancies.

**This amendment does not itself authorize running H0007** — per this
task's explicit instruction, H0007 execution is a separate, subsequent step
requiring the user's own initiation, exactly as every prior phase in this
project has required explicit sign-off before execution. What this amendment
establishes is that **no further pre-registration blocker remains**: the
fee configuration — the last open item across all three H0007 amendments —
is now frozen and verified.
