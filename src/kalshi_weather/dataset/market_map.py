"""Explicit Kalshi-market -> weather-target mapping.

This is a **documented stand-in for the `settlement_specs` table** that
Milestone 2b (settlement-rule parsing) will eventually produce automatically.
Until that exists there is no reliable programmatic link from a Kalshi market
ticker to the weather station/variable/date it settles against -- Kalshi
states that association only in free-text rules. Rather than guess by parsing
titles (explicitly forbidden by CLAUDE.md and the job of Milestone 2b), the
dataset layer reads the mapping from a versioned config file the researcher
maintains by hand, and records that file's content hash in every dataset
manifest so a build stays reproducible.

Markets absent from the mapping are not an error: they surface in the
validation report as "orphaned market records", exactly as they will once a
real settlement resolver leaves some markets unresolved.
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class MarketMapping:
    """One market's settlement target: which station/variable/day it resolves
    against. `variable` is the weather variable the market settles on
    (``tmax_f`` for a daily-high market, ``tmin_f`` for a daily-low)."""

    market_ticker: str
    station_id: str
    variable: str
    target_date: date


class MarketMapError(ValueError):
    """Raised when the mapping file is structurally invalid."""


_REQUIRED_KEYS = {"market_ticker", "station_id", "variable", "target_date"}
_ALLOWED_VARIABLES = {"tmax_f", "tmin_f"}


def parse_market_map(raw: Any) -> list[MarketMapping]:
    """Parse an already-loaded mapping document into typed rows.

    Kept separate from file IO so it's trivially unit-testable and so an
    in-memory mapping (e.g. from tests or a future settlement_specs query)
    can reuse the same validation.
    """
    if raw is None:
        return []
    if not isinstance(raw, dict) or "markets" not in raw:
        raise MarketMapError("mapping must be a mapping with a top-level 'markets' list")
    entries = raw["markets"]
    if not isinstance(entries, list):
        raise MarketMapError("'markets' must be a list")

    mappings: list[MarketMapping] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise MarketMapError(f"markets[{i}] must be a mapping")
        missing = _REQUIRED_KEYS - entry.keys()
        if missing:
            raise MarketMapError(f"markets[{i}] missing keys: {sorted(missing)}")
        ticker = str(entry["market_ticker"])
        if ticker in seen:
            raise MarketMapError(f"duplicate market_ticker {ticker!r} in mapping")
        seen.add(ticker)
        variable = str(entry["variable"])
        if variable not in _ALLOWED_VARIABLES:
            raise MarketMapError(
                f"markets[{i}] variable {variable!r} not in {sorted(_ALLOWED_VARIABLES)}"
            )
        target = entry["target_date"]
        target_date = target if isinstance(target, date) else date.fromisoformat(str(target))
        mappings.append(
            MarketMapping(
                market_ticker=ticker,
                station_id=str(entry["station_id"]),
                variable=variable,
                target_date=target_date,
            )
        )
    return mappings


def load_market_map(path: Path | None) -> list[MarketMapping]:
    """Load and validate the mapping file. A missing path (or a path that does
    not exist) yields an empty mapping -- a legitimate state that produces an
    empty market_weather dataset, not a crash."""
    if path is None or not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        return parse_market_map(yaml.safe_load(fh))
