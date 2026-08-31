"""Canonical settlement specification: everything needed to identify the
authoritative weather observation a Kalshi market settles against.

A `SettlementSpec` is the *output contract* of settlement resolution
(`docs/adr/0005-settlement-resolution.md`). Downstream code (the research
dataset's market->weather join) consumes only the small resolved subset via
`MarketMapping`; the full spec exists so every resolution -- including
failures -- is recorded, reproducible, and auditable.

Reproducibility fields beyond the obvious ones:

- ``parser_version`` -- bumped whenever parsing logic changes meaning; a spec
  is only comparable to another spec of the same parser version.
- ``rules_hash`` -- sha256 over the exact inputs parsed (rules text, source
  URL, event ticker), so a stored spec can be proven to describe the same
  rules text it was derived from, and a re-parse of unchanged inputs is a
  detectable no-op.
- ``source_url`` / ``wfo_site`` / ``source_location_code`` -- the structured
  settlement-source citation preserved verbatim, so the station match can be
  re-verified without re-fetching Kalshi.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

#: Bump whenever parser behavior changes in a way that could alter any spec.
PARSER_VERSION = "2"


class SettlementStatus(StrEnum):
    #: Fully identified: station, variable, and target date all determined
    #: and cross-checks passed. Only these become dataset mappings.
    RESOLVED = "resolved"
    #: Signals conflict (e.g. rules date != ticker date). Never guessed.
    AMBIGUOUS = "ambiguous"
    #: A required input is missing (no rules text, no date, no variable).
    UNRESOLVED = "unresolved"
    #: Recognized, but outside what this parser supports (non-CLI settlement
    #: source, station not in the registry, non-daily product).
    UNSUPPORTED = "unsupported"


class Confidence(StrEnum):
    #: Every available cross-check agreed (ticker date == rules date, title
    #: agrees with rules on variable, WFO site matches the registry).
    HIGH = "high"
    #: Resolved, but a secondary signal was missing or weak (noted in notes).
    MEDIUM = "medium"
    #: Not resolved; no confidence applies.
    NONE = "none"


@dataclass(frozen=True, slots=True)
class SettlementSpec:
    market_ticker: str
    series_ticker: str
    status: SettlementStatus
    confidence: Confidence
    parser_version: str
    rules_hash: str
    event_ticker: str | None = None
    city: str | None = None
    station_id: str | None = None
    variable: str | None = None  # "tmax_f" | "tmin_f"
    target_date: date | None = None
    settlement_source: str | None = None  # e.g. "NWS Climatological Report (Daily)"
    source_url: str | None = None
    wfo_site: str | None = None  # e.g. "OKX", from the source URL
    source_location_code: str | None = None  # e.g. "NYC", from the source URL
    unit: str | None = None  # "F"
    observation_window: str | None = None  # "local_calendar_day"
    rounding_rule: str | None = None  # "integer_f" (CLI reports whole degrees)
    #: How the CLI settlement source was established. ``structured_cli_url``
    #: means Kalshi's machine-parseable citation supplied it; ``rules_text``
    #: means the structured field was unusable and the authoritative rules
    #: prose was read instead (ADR 0026). Never inferred from a ticker.
    source_provenance: str | None = None
    market_close_time: datetime | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def is_resolved(self) -> bool:
        return self.status is SettlementStatus.RESOLVED


def rules_hash(*parts: str | None) -> str:
    """Stable hash of the exact inputs a spec was parsed from."""
    canonical = "\x1f".join(p if p is not None else "" for p in parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
