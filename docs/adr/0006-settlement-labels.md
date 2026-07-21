# ADR 0006: Canonical settlement-time labels

## Status

Accepted.

## Context

A senior research review of the program identified a label-validity gap: the
research datasets' `settled_value` is "the value from the latest CLI issuance
ever stored", but Kalshi determines settlement at a specific moment — and CLI
corrections can be published *after* that moment (H0002 measured revisions up
to 18°F). Scoring market experiments against the eventual-final value would
grade the market against an answer sheet it never saw. E-A makes the
settlement timeline explicit before any market experiment runs.

## Settlement-timeline findings (live-verified, 2026-07-21)

Fetched from finalized production markets — all **directly observed**, not
inferred:

1. **Kalshi exposes the exact settlement timestamp** (`settlement_ts`, e.g.
   `2026-07-15T12:01:05Z` ≈ 8:01am ET) and **the underlying value it paid on**
   (`expiration_value`, e.g. `"90.00"` °F), plus `result` (yes/no) and the
   structured strike definition (`floor_strike`/`cap_strike`/`strike_type`).
   The "value apparently used by Kalshi" is therefore observable, not
   reconstructed guesswork.
2. **The timeline genuinely has distinct stages.** Market close is ~00:59 ET
   — *before* the final morning CLI report (~2:15am ET); settlement follows
   the final report (~8am ET); corrections can arrive after settlement. So
   value-at-close ≠ value-at-settlement ≠ latest-final in general.
3. **Historical market retention is limited.** Events are listed back to 2021
   (1,808 for the NYC-high family), but `/markets?event_ticker=` returns
   markets only for roughly the most recent 2–3 months of events; older
   events return none. Documented as an API limitation: deep market history
   is *not* retroactively fetchable, which independently confirms the
   "run the collector continuously" imperative.

Remaining unavailable: nothing material for labels — the one inference is the
strike-semantics mapping (below), verified empirically rather than assumed.

## Decisions

1. **A typed, versioned `SettlementLabel`** (`settlement/labels.py`)
   distinguishing five values per market: preliminary (first issuance), value
   at close, value at settlement, Kalshi's paid `expiration_value`, and
   latest final. Strict as-of selection means a future issuance can never
   enter an earlier stage (tested). Statuses: `resolved` (exact
   `settlement_ts`), `bounded` (missing `settlement_ts` → conservative
   close+48h window, never silently substituted), `ambiguous`,
   `unsupported`, `missing_source_data`. Only resolved/bounded labels are
   usable downstream. `RECONSTRUCTION_VERSION` gates comparability, recorded
   in every dataset manifest.
2. **Labels are derived data, not a table.** They are deterministic functions
   of stored snapshots + issuance history, so they live in the dataset layer
   (a `settlement_labels` frame, content-hashed in the manifest) and are
   rebuilt per export — same rationale as `weather_panel` (ADR 0004). The
   *inputs* needed persistence: migration `0006` (additive) adds the six
   settlement fields to `market_snapshots`; `result`/`expiration_value`
   joined the snapshot content hash so a settlement transition appends one
   final snapshot.
3. **`market_weather` gains explicit stage columns** (`value_at_close`,
   `value_at_settlement`, `latest_final_value`, `settlement_label_status`,
   `settlement_label_version`) via left join; the existing `settled_value`
   column is **not redefined** (it keeps latest-final semantics,
   documented) — no downstream consumer breaks, and the stage distinction is
   opt-in by column choice. Market experiments must use
   `value_at_settlement`.
4. **Strike semantics are verified, not assumed.** `strike_type="between"` →
   `floor ≤ v ≤ cap`; `"greater"` → `v > floor`; `"less"` → `v < cap`
   (candidates implemented explicitly; unknown types yield no implied result
   rather than a guess). The empirical gate: over every finalized market
   with Kalshi settlement data, `implied_result(expiration_value)` must
   reproduce Kalshi's own `result`, and the reconstruction's
   `value_at_settlement` must equal `expiration_value`. Mismatches are
   listed individually in the validation report (see the E-A experiment
   record for rates).
5. **H0002's result is extended, not altered.** The 18.76%
   preliminary-to-final rate conflates intraday information flow (the 4pm
   preliminary precedes the end of its own day — an expected update) with
   genuine label instability. The stage-specific decision-relevant
   quantities are pre-registered as **H0012** (close-label instability;
   settlement-label stability) and measured in the E-A experiment record.

## Consequences

- Market calibration/mispricing experiments (H0005–H0007) must consume
  `value_at_settlement` (or Kalshi's `expiration_value` where present) as
  the label, never `settled_value`/latest-final. The dataset now makes the
  wrong choice visible instead of silent.
- The collector now persists settlement fields on every snapshot, so the
  going-forward record contains each market's settlement outcome natively.
- Older market history (>~3 months) is unrecoverable via the API; the
  payout-agreement validation window is bounded by that retention, and the
  finding strengthens the continuous-collection imperative.
- A future reconstruction change must bump `RECONSTRUCTION_VERSION`;
  manifests pin it alongside dataset content hashes.
