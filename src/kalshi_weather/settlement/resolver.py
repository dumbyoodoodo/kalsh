"""Settlement resolvers: things that produce the dataset layer's market ->
weather mappings.

The dataset builder's contract is unchanged from Milestone 4: it consumes a
``list[MarketMapping]`` (dataset/market_map.py). A ``SettlementResolver`` is
anything that can produce that list from a database session -- so swapping the
temporary config file for the automated parser required no dataset-builder
changes (the compatibility requirement of Milestone 2b).

Implementations:

- ``ParserSettlementResolver`` -- the automated path. Reads each market's
  latest snapshot + its series' stored settlement-source citation from the DB,
  runs the deterministic parser, and maps **only RESOLVED specs** ("no
  unresolved contract can reach a strategy", TASKS.md).
- ``ConfigSettlementResolver`` -- the Milestone 4 config file, retained as the
  *manual override file*: entries take precedence over parsed output (with
  optional audit fields), and it still works standalone for tests/backfills.
- ``CompositeSettlementResolver`` -- parser output overlaid with config
  overrides; the default for `dataset build`.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.dataset.market_map import MarketMapping, parse_market_map
from kalshi_weather.logging import get_logger
from kalshi_weather.settlement.parser import MarketInfo, SeriesInfo, parse_settlement
from kalshi_weather.settlement.spec import SettlementSpec
from kalshi_weather.storage.models import EventRecord, MarketSnapshot, SeriesRecord

logger = get_logger(__name__)


class SettlementResolver(Protocol):
    """Produces the market->weather mappings the dataset builder consumes."""

    async def resolve(self, session: AsyncSession) -> list[MarketMapping]: ...


def specs_to_mappings(specs: list[SettlementSpec]) -> list[MarketMapping]:
    """Only RESOLVED specs become mappings -- ambiguous/unresolved/unsupported
    markets stay visible as dataset 'orphans', never silently mapped."""
    return [
        MarketMapping(
            market_ticker=s.market_ticker,
            station_id=s.station_id,
            variable=s.variable,
            target_date=s.target_date,
        )
        for s in specs
        if s.is_resolved
        and s.station_id is not None
        and s.variable is not None
        and s.target_date is not None
    ]


@dataclass(frozen=True, slots=True)
class OverrideEntry:
    """A manual mapping override with its audit trail (TASKS.md Milestone 2b:
    'manual override file with audit fields')."""

    mapping: MarketMapping
    reason: str | None
    author: str | None
    added_on: str | None


def load_overrides(path: Path | None) -> list[OverrideEntry]:
    """Read the override file: the Milestone 4 market-map format, with each
    entry optionally carrying ``reason``/``author``/``added_on`` audit fields.
    A missing file is an empty override set."""
    if path is None or not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    mappings = {m.market_ticker: m for m in parse_market_map(raw)}
    entries: list[OverrideEntry] = []
    for item in (raw or {}).get("markets", []):
        mapping = mappings[str(item["market_ticker"])]
        entries.append(
            OverrideEntry(
                mapping=mapping,
                reason=item.get("reason"),
                author=item.get("author"),
                added_on=str(item["added_on"]) if item.get("added_on") is not None else None,
            )
        )
    return entries


async def load_parser_inputs(
    session: AsyncSession,
) -> list[tuple[SeriesInfo, MarketInfo]]:
    """Assemble (series, market) parser inputs from the DB: each market's
    latest snapshot, its series resolved via the events table where possible
    (falling back to the event ticker's series segment -- the documented
    Kalshi ticker structure), and the series' stored settlement-source JSON."""
    latest_ids = (
        select(func.max(MarketSnapshot.id))
        .group_by(MarketSnapshot.market_ticker)
        .scalar_subquery()
    )
    snapshots = (
        await session.scalars(select(MarketSnapshot).where(MarketSnapshot.id.in_(latest_ids)))
    ).all()

    events = {e.event_ticker: e for e in (await session.scalars(select(EventRecord))).all()}
    series_rows = {s.series_ticker: s for s in (await session.scalars(select(SeriesRecord))).all()}

    inputs: list[tuple[SeriesInfo, MarketInfo]] = []
    for snap in snapshots:
        series_ticker: str | None = None
        if snap.event_ticker:
            event = events.get(snap.event_ticker)
            if event is not None and event.series_ticker:
                series_ticker = event.series_ticker
            else:
                series_ticker = snap.event_ticker.split("-")[0]
        if series_ticker is None:
            series_ticker = snap.market_ticker.split("-")[0]

        series_row = series_rows.get(series_ticker)
        sources: list[dict[str, Any]] = []
        if series_row is not None and series_row.settlement_source:
            try:
                loaded = json.loads(series_row.settlement_source)
                if isinstance(loaded, list):
                    sources = loaded
            except json.JSONDecodeError:
                logger.warning(
                    "settlement.bad_settlement_source_json", series_ticker=series_ticker
                )
        inputs.append(
            (
                SeriesInfo(
                    ticker=series_ticker,
                    title=series_row.title if series_row is not None else None,
                    frequency=series_row.frequency if series_row is not None else None,
                    settlement_sources=sources,
                ),
                MarketInfo(
                    ticker=snap.market_ticker,
                    event_ticker=snap.event_ticker,
                    title=snap.title,
                    rules_primary=snap.rules_primary,
                    close_time=snap.close_time,
                ),
            )
        )
    return inputs


@dataclass(frozen=True, slots=True)
class ResolutionReport:
    """Outcome of a resolution pass, grouped by status for the validation
    report ('unknown cases surfaced rather than hidden')."""

    specs: list[SettlementSpec]
    overridden: list[str]  # market tickers whose mapping came from an override

    def by_status(self) -> dict[str, list[SettlementSpec]]:
        grouped: dict[str, list[SettlementSpec]] = {}
        for s in self.specs:
            grouped.setdefault(s.status.value, []).append(s)
        return grouped

    def counts(self) -> dict[str, int]:
        return {status: len(specs) for status, specs in sorted(self.by_status().items())}

    def to_dict(self) -> dict[str, Any]:
        return {
            "counts": self.counts(),
            "overridden": sorted(self.overridden),
            "markets": {
                status: [
                    {
                        "market_ticker": s.market_ticker,
                        "confidence": s.confidence.value,
                        "station_id": s.station_id,
                        "variable": s.variable,
                        "target_date": s.target_date.isoformat() if s.target_date else None,
                        "notes": list(s.notes),
                    }
                    for s in specs
                ]
                for status, specs in sorted(self.by_status().items())
            },
        }


class ParserSettlementResolver:
    """The automated resolver: parse every market in the DB."""

    async def resolve_specs(self, session: AsyncSession) -> list[SettlementSpec]:
        return [parse_settlement(s, m) for s, m in await load_parser_inputs(session)]

    async def resolve(self, session: AsyncSession) -> list[MarketMapping]:
        return specs_to_mappings(await self.resolve_specs(session))


class ConfigSettlementResolver:
    """The Milestone 4 config-file mapping, unchanged in behavior."""

    def __init__(self, path: Path | None) -> None:
        self._path = path

    async def resolve(self, session: AsyncSession) -> list[MarketMapping]:
        return [entry.mapping for entry in load_overrides(self._path)]


class CompositeSettlementResolver:
    """Parser output with config-file overrides layered on top. An override
    both replaces a parsed mapping for the same market and supplies mappings
    for markets the parser could not resolve."""

    def __init__(self, overrides_path: Path | None) -> None:
        self._overrides_path = overrides_path

    async def resolve_report(
        self, session: AsyncSession
    ) -> tuple[list[MarketMapping], ResolutionReport]:
        specs = await ParserSettlementResolver().resolve_specs(session)
        parsed = {m.market_ticker: m for m in specs_to_mappings(specs)}
        overrides = load_overrides(self._overrides_path)
        for entry in overrides:
            parsed[entry.mapping.market_ticker] = entry.mapping
        report = ResolutionReport(
            specs=specs, overridden=[e.mapping.market_ticker for e in overrides]
        )
        return sorted(parsed.values(), key=lambda m: m.market_ticker), report

    async def resolve(self, session: AsyncSession) -> list[MarketMapping]:
        mappings, _ = await self.resolve_report(session)
        return mappings
