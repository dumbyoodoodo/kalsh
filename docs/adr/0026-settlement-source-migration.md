# ADR 0026 — Rules-text fallback for CLI settlement, and the Weather Company migration

**Status:** accepted (2026-08-31). Amends [ADR 0005](0005-settlement-resolution.md);
does not supersede it — the structured-citation path remains preferred and unchanged.

## Context

ADR 0005 built settlement resolution on a stated assumption:

> **Kalshi's series objects carry a structured settlement citation.**
> `settlement_sources[].url` for every daily-temperature series is a
> machine-parseable NWS CLI product URL.

That was true when observed live during Milestone 2b. It is no longer true.

### What changed upstream

Two independent upstream changes, discovered 2026-08-31 while diagnosing why
H0019 readiness reported zero event-groups in *every* split — including `train`,
whose window closed weeks earlier and whose contents cannot legitimately change.

**1. The structured citation was genericised.** Every weather series now returns

```json
[{"name": "The Weather Company", "url": "https://weather.com/kalshi"}]
```

with no `product=CLI`, no `issuedby=`, no `site=`. `_parse_cli_source` requires
all three, so it correctly returned "not an NWS CLI product URL" — and the
parser correctly classified every market `UNSUPPORTED`. **The parser was not
broken; its designed input was withdrawn.** `settlement resolve-all` returned
0 resolved / 43,203 unsupported / 1 unresolved, which produced zero settlement
labels, which produced H0019's all-zero readiness.

**2. Daily settlement moved to a different authority.** Independently, and
later, the *rules text itself* changed. A hard cutover, measured across the
preserved corpus:

| Event date | rules cite NWS CLI | rules cite The Weather Company |
|---|---|---|
| 2026-08-07 … 08-13 | 100% | 0 |
| 2026-08-15 … 08-20 | 0 | 100% |

Before: *"the highest temperature recorded in Central Park, New York for August
06, 2026 as reported by the National Weather Service's Climatological Report
(Daily)"*. After: *"the maximum temperature recorded at New York City (CLINYC)
for Aug 20, 2026 ... according to The Weather Company"*. The CLI location code
survives as a label; the settlement authority does not.

These are separate facts and must not be conflated. Change 1 broke markets that
are still genuinely NWS-settled. Change 2 ended NWS settlement for new markets.

## Decision

### Add a rules-text fallback, strictly narrower than the path it replaces

The structured citation stays the **preferred** path and is tried first. Only
when it cannot yield a CLI source does the parser read the authoritative rules
prose. `CLAUDE.md` is explicit — *"Treat contract settlement rules as
authoritative"* — and where the structured field and the prose disagree, the
prose is the contract.

The fallback requires **all** of:

- explicit `National Weather Service` wording, **and**
- explicit `Climatological Report` wording (two required matches, not one loose
  one, so generic weather language cannot open the path);
- **no** `The Weather Company` mention;
- **no** time-of-day expression (`8 AM EDT`, `13:00`);
- a parseable `recorded at|in <location> for` phrase;
- **exactly one** unambiguous station match in the existing registry.

Anything else fails closed with a recorded reason. All pre-existing invariants
survive: the variable/date cross-check against the event ticker, the
non-temperature exclusion table, and the ambiguity-is-an-outcome rule.

### Station identity comes only from the registry

The location phrase is matched against registry-derived keys — the station's
`city` ("New York") and the landmark portion of its `name` ("Central Park").
Both phrasings occur in live prose. A phrase matching two stations is
`AMBIGUOUS`, never broken by preference order; a phrase matching none is
refused with an instruction to extend the registry.

**Ticker inference remains prohibited.** No `KXHIGHNY → NYC` table exists or may
be added. ADR 0005 forbade it, `CLAUDE.md` forbids it (*"Do not infer the
weather station ... from the title alone"*), and a test enforces it by walking
the parser's AST for ticker literals in executable code — deliberately
excluding docstrings, so the module may still *document* the ticker date format
without tripping its own guard.

### Provenance is recorded, not inferred

`SettlementSpec.source_provenance` is `structured_cli_url` or `rules_text`, and
`notes` records why the fallback engaged. Any downstream consumer can tell
which specs rest on a machine-parseable citation and which on prose. `PARSER_VERSION`
moves `1 → 2`, so specs produced under each regime remain distinguishable
forever.

### Hourly Weather Company contracts stay out of scope

`KXTEMP*H` markets settle on an instantaneous reading at a named hour, by The
Weather Company, sometimes citing raw coordinates (`KORD`). They are a different
product from the CLI daily calendar-day aggregate this project models. They are
excluded by the NWS/CLI requirement, and again by the Weather Company and
time-of-day disqualifiers — deliberately redundant, because an hourly contract
must never resolve even if its prose is reworded.

## Consequences

**Recovered.** 2,652 markets resolve where zero did before: all seven registry
stations, 1,326 `tmax_f` and 1,326 `tmin_f`, target dates 2026-05-15 → 2026-08-13.
All via `rules_text`; the structured path now yields nothing because Kalshi
genericised every citation.

**Lost — and not by our choice.** No market with a target date after 2026-08-13
is eligible, because none is settled by the NWS CLI product. The project's
settlement model — `weather/cli_parser.py`, the CLI observation timeline, the
station registry keyed on CLI location codes — targets a product Kalshi has
stopped settling against. This ADR does not address that; it is a strategic
question, not a parsing one.

**A defense was weakened.** In the structured path the registry's `wfo_site` is
cross-checked against the URL's `site=` parameter — two independent sources
agreeing. In the fallback both sides come from the registry, so that check is
vacuous. The compensating control is that the fallback is strictly narrower: two
required wording matches, two disqualifiers, and a single-unambiguous-match
requirement. This is a real reduction in defense-in-depth and is recorded rather
than papered over.

**Experiment membership changed from zero to non-zero.** This is a repair
restoring the pre-existing eligible population, performed outcome-blind and
before any H0019 rerun — not a change made after observing results. H0019 could
not execute at all beforehand.

**H0019 is materially affected.** Its registered test window is 2026-08-12 →
08-25. Only 08-12 and 08-13 fall before the settlement migration, so at most two
of fourteen test dates carry eligible markets. That is a question about the
experiment's validity, addressed separately and deliberately not resolved here.
