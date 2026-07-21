"""Resolver + persistence + dataset-integration tests for Milestone 2b.

The pivotal assertions: the parser-backed resolver produces the same
``list[MarketMapping]`` contract the dataset builder has consumed since
Milestone 4 (no builder changes), only RESOLVED markets ever become mappings,
and the config file now acts as a manual override layer with audit fields.
"""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.dataset import pipeline
from kalshi_weather.settlement.resolver import (
    CompositeSettlementResolver,
    ConfigSettlementResolver,
    ParserSettlementResolver,
    load_overrides,
)
from kalshi_weather.settlement.spec import PARSER_VERSION
from kalshi_weather.storage.models import Base
from kalshi_weather.storage.repositories import (
    get_latest_settlement_specs,
    save_event,
    save_market_snapshot,
    save_series,
    save_settlement_spec,
    save_weather_forecast,
    save_weather_observation,
    save_weather_station,
)

NYC_CLI_URL = "https://forecast.weather.gov/product.php?site=OKX&product=CLI&issuedby=NYC"
SNOW_URL = "https://www.weather.gov"

HIGH_RULES = (
    "If the highest temperature recorded in Central Park, New York for July 21, 2026 "
    "as reported by the National Weather Service's Climatological Report (Daily), "
    "is less than 79°, then the market resolves to Yes."
)


@pytest.fixture
async def session():  # type: ignore[no-untyped-def]
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _seed_market(
    session: AsyncSession,
    *,
    series_ticker: str = "KXHIGHNY",
    series_title: str = "Highest temperature in NYC",
    sources_json: str | None = json.dumps([{"name": "NWS", "url": NYC_CLI_URL}]),
    market_ticker: str = "KXHIGHNY-26JUL21-T79",
    event_ticker: str = "KXHIGHNY-26JUL21",
    rules: str | None = HIGH_RULES,
    with_event_row: bool = True,
) -> None:
    await save_series(
        session,
        series_ticker=series_ticker,
        category="Climate and Weather",
        title=series_title,
        frequency="daily",
        settlement_source=sources_json,
    )
    if with_event_row:
        await save_event(
            session,
            event_ticker=event_ticker,
            series_ticker=series_ticker,
            category="Climate and Weather",
            title=series_title,
            sub_title=None,
        )
    await save_market_snapshot(
        session,
        market_ticker=market_ticker,
        event_ticker=event_ticker,
        market_type="binary",
        title="t",
        subtitle="s",
        status="open",
        yes_bid_cents=40,
        yes_ask_cents=45,
        last_price_cents=42,
        volume=10,
        open_interest=5,
        close_time=datetime(2026, 7, 22, 4, 59, tzinfo=UTC),
        rules_primary=rules,
        rules_secondary=None,
        raw_payload_id=None,
    )


async def test_parser_resolver_maps_resolved_market(session: AsyncSession) -> None:
    await _seed_market(session)
    mappings = await ParserSettlementResolver().resolve(session)
    assert len(mappings) == 1
    m = mappings[0]
    assert (m.market_ticker, m.station_id, m.variable) == (
        "KXHIGHNY-26JUL21-T79",
        "NYC",
        "tmax_f",
    )
    assert m.target_date == date(2026, 7, 21)


async def test_unresolved_market_never_becomes_a_mapping(session: AsyncSession) -> None:
    """'No unresolved contract can reach a strategy': a market with an
    unsupported source yields a spec but no mapping."""
    await _seed_market(
        session,
        series_ticker="KXNYCSNOWM",
        series_title="NYC Snowfall monthly",
        sources_json=json.dumps([{"name": "NWS", "url": SNOW_URL}]),
        market_ticker="KXNYCSNOWM-26AUG-A",
        event_ticker="KXNYCSNOWM-26AUG",
        rules="If total snowfall...",
    )
    resolver = ParserSettlementResolver()
    specs = await resolver.resolve_specs(session)
    assert len(specs) == 1
    assert specs[0].status.value == "unsupported"
    assert await resolver.resolve(session) == []


async def test_series_link_falls_back_to_event_ticker_prefix(session: AsyncSession) -> None:
    """Without an events row, the series is derived from the event ticker's
    documented SERIES-DATE structure."""
    await _seed_market(session, with_event_row=False)
    mappings = await ParserSettlementResolver().resolve(session)
    assert len(mappings) == 1


async def test_market_without_series_row_is_unresolved(session: AsyncSession) -> None:
    """A market whose series was never collected has no settlement source ->
    unresolved, not an error and not a guess."""
    await save_market_snapshot(
        session,
        market_ticker="KXMYSTERY-26JUL21-T50",
        event_ticker="KXMYSTERY-26JUL21",
        market_type="binary",
        title="t",
        subtitle=None,
        status="open",
        yes_bid_cents=None,
        yes_ask_cents=None,
        last_price_cents=None,
        volume=None,
        open_interest=None,
        close_time=None,
        rules_primary=HIGH_RULES,
        rules_secondary=None,
        raw_payload_id=None,
    )
    resolver = ParserSettlementResolver()
    specs = await resolver.resolve_specs(session)
    assert specs[0].status.value == "unresolved"
    assert await resolver.resolve(session) == []


# --- overrides ---------------------------------------------------------------


def _write_overrides(tmp_path: Path) -> Path:
    path = tmp_path / "overrides.yaml"
    path.write_text(
        "markets:\n"
        "  - market_ticker: KXHIGHNY-26JUL21-T79\n"
        "    station_id: NYC\n"
        "    variable: tmin_f\n"  # deliberately different from parsed tmax_f
        "    target_date: 2026-07-21\n"
        "    reason: manual correction for test\n"
        "    author: researcher\n"
        "    added_on: 2026-07-21\n"
        "  - market_ticker: KXMANUAL-26JUL21-T60\n"
        "    station_id: NYC\n"
        "    variable: tmax_f\n"
        "    target_date: 2026-07-21\n",
        encoding="utf-8",
    )
    return path


def test_load_overrides_reads_audit_fields(tmp_path: Path) -> None:
    entries = load_overrides(_write_overrides(tmp_path))
    assert len(entries) == 2
    first = next(e for e in entries if e.mapping.market_ticker == "KXHIGHNY-26JUL21-T79")
    assert first.reason == "manual correction for test"
    assert first.author == "researcher"
    assert first.added_on == "2026-07-21"
    second = next(e for e in entries if e.mapping.market_ticker == "KXMANUAL-26JUL21-T60")
    assert second.reason is None  # audit fields optional (backward compatible)


async def test_composite_resolver_overrides_take_precedence(
    session: AsyncSession, tmp_path: Path
) -> None:
    await _seed_market(session)
    resolver = CompositeSettlementResolver(_write_overrides(tmp_path))
    mappings, report = await resolver.resolve_report(session)

    by_ticker = {m.market_ticker: m for m in mappings}
    # parsed tmax_f replaced by the override's tmin_f
    assert by_ticker["KXHIGHNY-26JUL21-T79"].variable == "tmin_f"
    # override-only market present even though the parser never saw it
    assert "KXMANUAL-26JUL21-T60" in by_ticker
    assert set(report.overridden) == {"KXHIGHNY-26JUL21-T79", "KXMANUAL-26JUL21-T60"}
    assert report.counts() == {"resolved": 1}


async def test_config_resolver_alone_matches_milestone4_behavior(
    session: AsyncSession, tmp_path: Path
) -> None:
    mappings = await ConfigSettlementResolver(_write_overrides(tmp_path)).resolve(session)
    assert {m.market_ticker for m in mappings} == {
        "KXHIGHNY-26JUL21-T79",
        "KXMANUAL-26JUL21-T60",
    }


async def test_composite_resolver_without_overrides_file(session: AsyncSession) -> None:
    await _seed_market(session)
    mappings, report = await CompositeSettlementResolver(None).resolve_report(session)
    assert len(mappings) == 1
    assert report.overridden == []


# --- persistence -------------------------------------------------------------


async def test_persisted_specs_dedupe_and_version(session: AsyncSession) -> None:
    await _seed_market(session)
    specs = await ParserSettlementResolver().resolve_specs(session)
    s = specs[0]
    kwargs = dict(
        market_ticker=s.market_ticker,
        series_ticker=s.series_ticker,
        event_ticker=s.event_ticker,
        status=s.status.value,
        confidence=s.confidence.value,
        city=s.city,
        station_id=s.station_id,
        variable=s.variable,
        target_date=s.target_date,
        settlement_source=s.settlement_source,
        source_url=s.source_url,
        wfo_site=s.wfo_site,
        source_location_code=s.source_location_code,
        unit=s.unit,
        observation_window=s.observation_window,
        rounding_rule=s.rounding_rule,
        market_close_time=s.market_close_time,
        notes=list(s.notes),
        parser_version=s.parser_version,
        rules_hash=s.rules_hash,
    )
    first = await save_settlement_spec(session, **kwargs)  # type: ignore[arg-type]
    second = await save_settlement_spec(session, **kwargs)  # type: ignore[arg-type]
    assert first.was_duplicate is False
    assert second.was_duplicate is True

    # a parser upgrade (new version) appends a new row; history is preserved
    upgraded = {**kwargs, "parser_version": "999"}
    third = await save_settlement_spec(session, **upgraded)  # type: ignore[arg-type]
    assert third.was_duplicate is False

    latest = await get_latest_settlement_specs(session)
    assert len(latest) == 1  # one market -> one latest row
    assert latest[0].parser_version == "999"
    assert latest[0].observation_window == "local_calendar_day"


# --- dataset integration (the compatibility requirement) ---------------------


async def test_dataset_builds_market_weather_from_parser_resolver(
    session: AsyncSession,
) -> None:
    """End to end: seeded series+market+weather -> parser-backed resolver ->
    unchanged Milestone 4 pipeline -> populated market_weather. The pipeline
    is called exactly as Milestone 4 defined it, proving no builder changes
    were needed."""
    await _seed_market(session)
    await save_weather_station(
        session,
        station_id="NYC",
        provider="nws",
        source_location_code="NYC",
        office="OKX",
        latitude=Decimal("40.7829"),
        longitude=Decimal("-73.9654"),
        name="Central Park, NY",
        timezone="America/New_York",
    )
    await save_weather_observation(
        session,
        station_id="NYC",
        provider="nws",
        variable="tmax_f",
        value=Decimal(81),
        unit="F",
        observation_date=date(2026, 7, 21),
        issuance_time=datetime(2026, 7, 22, 6, 0, tzinfo=UTC),
        source_product_id="p1",
        raw_payload_id=None,
    )
    await save_weather_forecast(
        session,
        station_id="NYC",
        provider="nws",
        variable="temperature",
        point_estimate=Decimal(83),
        unit="F",
        issue_time=datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
        valid_start=datetime(2026, 7, 21, 16, 0, tzinfo=UTC),
        valid_end=datetime(2026, 7, 21, 23, 0, tzinfo=UTC),
        raw_payload_id=None,
    )

    mappings, _report = await CompositeSettlementResolver(None).resolve_report(session)
    output = await pipeline.build(
        session,
        database_url="sqlite://",
        mappings=mappings,
        which="all",
        version="v1",
        resolver_meta={"resolver": "parser+overrides", "parser_version": PARSER_VERSION},
    )

    mw = output.frames["market_weather"]
    assert mw.height == 1  # automatically populated, no config file involved
    row = mw.row(0, named=True)
    assert row["station_id"] == "NYC"
    assert row["variable"] == "tmax_f"
    assert row["settled_value"] == 81.0
    assert output.manifest.config["resolver"]["parser_version"] == PARSER_VERSION
    assert output.validation.ok
