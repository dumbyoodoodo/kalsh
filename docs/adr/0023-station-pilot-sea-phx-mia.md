# ADR 0023: Three-city station pilot (SEA, PHX, MIA)

Date: 2026-07-27
Status: Accepted

## Context

The 2026-07-27 settlement-coverage audit found ~1,470 genuine NWS-CLI
daily-temperature markets unsupported solely because their cities are not
in the station registry — the parser grammar already handles them. The
registry doubles as the weather-collection roster, so adding a city is a
data-collection commitment, taken deliberately (feasibility audit, same
date): Kalshi-side cost is zero (those markets' snapshots/books/trades are
already collected category-wide; 596 candidate-city tickers already in the
per-ticker poll ledger), and the weather-side increment is small.

## Decision

Add exactly three stations as a **pilot**: SEA (Seattle-Tacoma Intl, WFO
SEW), PHX (Phoenix Sky Harbor, WFO PSR), MIA (Miami Intl, WFO MFL) —
chosen for maximal climate-regime diversity per city (Pacific marine /
Sonoran desert / tropical Atlantic) and because PHX exercises the one
nontrivial timezone case (America/Phoenix, **no daylight saving**).

Every mapping is authoritative, no fuzzy matching: (code, WFO) pairs
verbatim from Kalshi's own settlement-source URLs in collected production
data; coordinates and timezones from NWS station metadata
(`api.weather.gov/stations/K<code>`), cross-verified via `/points`
(same WFO, same timezone) and IEM CLI availability (2026 and 2023) on
2026-07-27. Registry data only — **no parser, schema, config, or service
change**; frozen H0018/H0019/H0020 station sets are literal tuples in the
experiment modules and are test-asserted unchanged.

Expected cost: ~+21 weather requests per ~43-min cycle (+~750/day to
NWS/IEM), ~+200–400 forecast/observation/CLI rows/day, ≤1 MB/day storage.
Benefit: ~252 historical temperature markets (~190 already settled) become
deterministically resolvable retroactively; prospective point-in-time
forecasts for three new climate regimes accrue for **future** hypotheses
(H0021+) only.

## Pilot scope and review

One-week observation period (checklist in
`docs/runbooks/next_operator_actions.md` conventions / final task report):
per-city collection continuity, forecast cadence, observation
completeness, CLI availability, cycle duration, API failures, DB/backup
growth, observatory warnings, settlement-resolution gain. The remaining
11 audited cities (DFW, ATL, BOS, DCA, HOU, AUS, PHL, LAS, MSY, MSP, SFO
— plus discovered OKC/SAT) are **deferred pending pilot review and a
separate explicit human approval**; nothing in this ADR approves them.

Rollback: revert the pilot commit, restart only the collector (single-
instance guard), retain all collected rows append-only.
