"""CLI for read-only Kalshi market-data collection and inspection.

Every command persists raw payloads (append-only) as they're fetched; the
`market`/`orderbook` commands additionally persist normalized snapshots, and
`collector run` runs the full historical ingestion pipeline (see
ingestion/collector.py and docs/runbooks/collector.md). No order-submission
command exists here or anywhere else in this milestone.
"""

import asyncio
import json
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import typer
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.config import Environment, Settings, get_settings
from kalshi_weather.dataset import pipeline as dataset_pipeline
from kalshi_weather.dataset.export import ExportFormat, default_version
from kalshi_weather.dataset.market_map import load_market_map
from kalshi_weather.ingestion.collector import run_collector_loop
from kalshi_weather.ingestion.weather_collector import run_weather_collector_loop
from kalshi_weather.kalshi.auth import load_private_key_from_setting
from kalshi_weather.kalshi.client import KalshiClient
from kalshi_weather.kalshi.orderbook import reconstruct_best_quote
from kalshi_weather.logging import configure_logging, get_logger
from kalshi_weather.storage.database import create_engine, create_session_factory, session_scope
from kalshi_weather.storage.repositories import (
    save_market_snapshot,
    save_orderbook_snapshot,
    save_raw_payload,
    save_series,
)
from kalshi_weather.weather.provider import NwsProvider
from kalshi_weather.weather.stations import UnknownStationError, get_station, list_stations

logger = get_logger(__name__)

app = typer.Typer(help="Kalshi weather market-data CLI (read-only).")
series_app = typer.Typer(help="Inspect Kalshi series.")
markets_app = typer.Typer(help="Inspect Kalshi markets.")
orderbook_app = typer.Typer(help="Inspect Kalshi order books.")
collector_app = typer.Typer(help="Run the historical market-data collector.")
weather_app = typer.Typer(help="Weather station data and collector.")
weather_stations_app = typer.Typer(help="Inspect the weather station registry.")
dataset_app = typer.Typer(help="Build, validate, and export research datasets.")
app.add_typer(series_app, name="series")
app.add_typer(markets_app, name="markets")
app.add_typer(orderbook_app, name="orderbook")
app.add_typer(collector_app, name="collector")
app.add_typer(weather_app, name="weather")
weather_app.add_typer(weather_stations_app, name="stations")
app.add_typer(dataset_app, name="dataset")


@asynccontextmanager
async def _open_session(settings: Settings) -> AsyncIterator[AsyncSession]:
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            yield session
    finally:
        await engine.dispose()


def _build_client(settings: Settings, session: AsyncSession | None = None) -> KalshiClient:
    base_url = settings.base_url_for(settings.kalshi_env)
    key_id: str | None = None
    private_key: rsa.RSAPrivateKey | None = None
    if settings.kalshi_env == Environment.DEMO and settings.kalshi_demo_private_key:
        key_id = settings.kalshi_demo_api_key_id
        private_key = load_private_key_from_setting(settings.kalshi_demo_private_key)

    raw_payload_sink = None
    if session is not None:

        async def sink(
            source: str, endpoint: str, request_key: str, status: int, payload: Any
        ) -> int:
            raw = await save_raw_payload(
                session,
                source=source,
                endpoint_or_channel=endpoint,
                request_key=request_key,
                http_status=status,
                payload_json=payload,
            )
            return raw.id

        raw_payload_sink = sink

    return KalshiClient(
        base_url=base_url,
        environment=settings.kalshi_env,
        key_id=key_id,
        private_key=private_key,
        raw_payload_sink=raw_payload_sink,
    )


@series_app.command("list")
def series_list(
    category: str | None = typer.Option(None, help="Filter by series category."),
) -> None:
    """List Kalshi series, optionally filtered by category (e.g. weather)."""

    async def run() -> None:
        settings = get_settings()
        async with _open_session(settings) as session, _build_client(settings, session) as client:
            series = await client.list_series(category=category)
            for s in series:
                await save_series(
                    session,
                    series_ticker=s.ticker,
                    category=s.category,
                    title=s.title,
                    frequency=s.frequency,
                    raw_payload_id=client.last_raw_payload_id,
                )
                typer.echo(f"{s.ticker}\t{s.category or '-'}\t{s.title or '-'}")

    asyncio.run(run())


@markets_app.command("list")
def markets_list(
    event_ticker: str | None = typer.Option(None),
    status: str | None = typer.Option(None),
) -> None:
    """List Kalshi markets, optionally filtered by event ticker and status."""

    async def run() -> None:
        settings = get_settings()
        async with _open_session(settings) as session, _build_client(settings, session) as client:
            markets = await client.list_markets(event_ticker=event_ticker, status=status)
            for m in markets:
                typer.echo(f"{m.ticker}\t{m.status or '-'}\t{m.title or '-'}")

    asyncio.run(run())


@app.command("market")
def market_show(ticker: str) -> None:
    """Show a single market snapshot and persist it."""

    async def run() -> None:
        settings = get_settings()
        async with _open_session(settings) as session, _build_client(settings, session) as client:
            market = await client.get_market(ticker)
            await save_market_snapshot(
                session,
                market_ticker=market.ticker,
                event_ticker=market.event_ticker,
                market_type=market.market_type,
                title=market.title,
                subtitle=market.subtitle,
                status=market.status,
                yes_bid_cents=market.yes_bid,
                yes_ask_cents=market.yes_ask,
                last_price_cents=market.last_price,
                volume=market.volume,
                open_interest=market.open_interest,
                close_time=market.close_time,
                rules_primary=market.rules_primary,
                rules_secondary=market.rules_secondary,
                raw_payload_id=client.last_raw_payload_id,
            )
            typer.echo(f"ticker:         {market.ticker}")
            typer.echo(f"title:          {market.title or '-'}")
            typer.echo(f"status:         {market.status or '-'}")
            if market.yes_bid is not None:
                typer.echo(f"yes_bid:        {Decimal(market.yes_bid) / 100}")
            if market.yes_ask is not None:
                typer.echo(f"yes_ask:        {Decimal(market.yes_ask) / 100}")
            volume = market.volume if market.volume is not None else "-"
            open_interest = market.open_interest if market.open_interest is not None else "-"
            typer.echo(f"volume:         {volume}")
            typer.echo(f"open_interest:  {open_interest}")

    asyncio.run(run())


@orderbook_app.command("show")
def orderbook_show(ticker: str, depth: int | None = typer.Option(None)) -> None:
    """Show best derived YES/NO bid and ask for a market's order book and persist it."""

    async def run() -> None:
        settings = get_settings()
        async with _open_session(settings) as session, _build_client(settings, session) as client:
            book = await client.get_orderbook(ticker, depth=depth)
            await save_orderbook_snapshot(
                session,
                market_ticker=ticker,
                yes_levels=book.orderbook.yes,
                no_levels=book.orderbook.no,
                raw_payload_id=client.last_raw_payload_id,
            )
            yes_bids = [(level[0], level[1]) for level in book.orderbook.yes]
            no_bids = [(level[0], level[1]) for level in book.orderbook.no]
            quote = reconstruct_best_quote(yes_bids, no_bids)
            typer.echo(f"best_yes_bid: {quote.best_yes_bid_cents}")
            typer.echo(f"best_yes_ask: {quote.best_yes_ask_cents}")
            typer.echo(f"best_no_bid:  {quote.best_no_bid_cents}")
            typer.echo(f"best_no_ask:  {quote.best_no_ask_cents}")
            typer.echo(f"yes_spread:   {quote.yes_spread_cents}")

    asyncio.run(run())


@collector_app.command("run")
def collector_run(
    once: bool = typer.Option(
        False, "--once", help="Run a single collection cycle and exit (e.g. for cron)."
    ),
    interval: float | None = typer.Option(
        None, "--interval", help="Seconds between cycles (default: settings/env)."
    ),
    category: str | None = typer.Option(
        None, "--category", help="Series category to collect (default: settings/env)."
    ),
    status: str | None = typer.Option(
        None, "--status", help="Market status filter (default: settings/env)."
    ),
) -> None:
    """Run the historical Kalshi market-data collector.

    Continuous by default: runs a collection cycle, waits `--interval`
    seconds, repeats, until interrupted (Ctrl-C / SIGTERM) -- the current
    cycle finishes before exiting, nothing is left half-written. Use
    `--once` to run a single cycle and exit instead, e.g. from cron.
    See docs/runbooks/collector.md for operational details.
    """

    async def run() -> None:
        settings = get_settings()
        configure_logging(settings.log_level)
        engine = create_engine(settings.database_url)
        session_factory = create_session_factory(engine)
        stop_event = asyncio.Event()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

        logger.info(
            "collector.starting",
            once=once,
            category=category or settings.collector_category,
            market_status=status or settings.collector_market_status,
            interval_seconds=interval
            if interval is not None
            else settings.collector_interval_seconds,
        )
        try:
            await run_collector_loop(
                session_factory=session_factory,
                client_factory=lambda session: _build_client(settings, session),
                category=category or settings.collector_category,
                market_status=status or settings.collector_market_status,
                interval_seconds=(
                    interval if interval is not None else settings.collector_interval_seconds
                ),
                stop_event=stop_event,
                max_cycles=1 if once else None,
            )
        finally:
            await engine.dispose()
        logger.info("collector.stopped")

    asyncio.run(run())


def _build_weather_provider(settings: Settings, session: AsyncSession | None = None) -> NwsProvider:
    raw_payload_sink = None
    if session is not None:

        async def sink(
            source: str, endpoint: str, request_key: str, status: int, payload: Any
        ) -> int:
            raw = await save_raw_payload(
                session,
                source=source,
                endpoint_or_channel=endpoint,
                request_key=request_key,
                http_status=status,
                payload_json=payload,
            )
            return raw.id

        raw_payload_sink = sink

    return NwsProvider(user_agent=settings.weather_user_agent, raw_payload_sink=raw_payload_sink)


@weather_stations_app.command("list")
def weather_stations_list() -> None:
    """List the static weather station registry (weather/stations.py)."""
    for station in list_stations():
        typer.echo(
            f"{station.station_id}\t{station.name}\t{station.latitude},{station.longitude}"
            f"\t{station.timezone}"
        )


@weather_app.command("collect")
def weather_collect(
    once: bool = typer.Option(
        False, "--once", help="Run a single collection cycle and exit (e.g. for cron)."
    ),
    interval: float | None = typer.Option(
        None, "--interval", help="Seconds between cycles (default: settings/env)."
    ),
    station: str | None = typer.Option(
        None, "--station", help="Collect only this station_id (default: all registered)."
    ),
    backfill_days: int | None = typer.Option(
        None, "--backfill-days", help="Initial backfill window for a new station."
    ),
) -> None:
    """Run the historical weather data collector (stations/observations/forecasts).

    Continuous by default; `--once` runs a single cycle and exits, e.g. from
    cron. See docs/runbooks/weather_collector.md for operational details.
    """
    stations = None
    if station is not None:
        try:
            stations = [get_station(station)]
        except UnknownStationError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

    async def run() -> None:
        settings = get_settings()
        configure_logging(settings.log_level)
        engine = create_engine(settings.database_url)
        session_factory = create_session_factory(engine)
        stop_event = asyncio.Event()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

        resolved_backfill_days = (
            backfill_days if backfill_days is not None else settings.weather_backfill_days
        )
        resolved_interval = (
            interval if interval is not None else settings.weather_interval_seconds
        )
        logger.info(
            "weather_collector.starting",
            once=once,
            stations=[s.station_id for s in stations] if stations else "all",
            backfill_days=resolved_backfill_days,
            interval_seconds=resolved_interval,
        )
        try:
            await run_weather_collector_loop(
                session_factory=session_factory,
                provider_factory=lambda session: _build_weather_provider(settings, session),
                stations=stations,
                backfill_days=resolved_backfill_days,
                interval_seconds=resolved_interval,
                stop_event=stop_event,
                max_cycles=1 if once else None,
            )
        finally:
            await engine.dispose()
        logger.info("weather_collector.stopped")

    asyncio.run(run())


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


async def _build_dataset(
    settings: Settings, *, which: str, start: date | None, end: date | None, version: str
) -> dataset_pipeline.BuildOutput:
    mappings = load_market_map(settings.dataset_market_map_path)
    async with _open_session(settings) as session:
        return await dataset_pipeline.build(
            session,
            database_url=settings.database_url,
            mappings=mappings,
            which=which,
            start=start,
            end=end,
            version=version,
        )


def _echo_summary(output: dataset_pipeline.BuildOutput) -> None:
    for name, frame in output.frames.items():
        typer.echo(f"{name}: {frame.height} rows, {len(frame.columns)} columns")
    report = output.validation
    typer.echo(f"validation: {'OK' if report.ok else 'ERRORS'} ({report.error_count} errors)")
    for finding in report.findings:
        if finding.count:
            typer.echo(f"  [{finding.severity}] {finding.check}: {finding.count}")


@dataset_app.command("build")
def dataset_build(
    which: str = typer.Option("all", help="Dataset to build: all|weather_panel|market_weather."),
    start: str | None = typer.Option(None, help="Inclusive start target date (YYYY-MM-DD)."),
    end: str | None = typer.Option(None, help="Inclusive end target date (YYYY-MM-DD)."),
    version: str | None = typer.Option(None, help="Dataset version (default: UTC timestamp)."),
    output_root: str | None = typer.Option(None, help="Export root (default: settings/env)."),
    export: bool = typer.Option(True, help="Write Parquet + manifest to disk."),
) -> None:
    """Build the research dataset(s): point-in-time join, validate, compute
    stats, and (by default) export a versioned Parquet directory. See
    docs/runbooks/dataset.md."""

    async def run() -> None:
        settings = get_settings()
        configure_logging(settings.log_level)
        resolved_version = version or default_version()
        output = await _build_dataset(
            settings, which=which, start=_parse_date(start), end=_parse_date(end),
            version=resolved_version,
        )
        _echo_summary(output)
        if export:
            root = Path(output_root) if output_root else settings.dataset_root
            result = dataset_pipeline.export(
                output, root=root, version=resolved_version, fmt=ExportFormat.PARQUET
            )
            typer.echo(f"exported to {result.output_dir}")
        if not output.validation.ok:
            raise typer.Exit(code=1)

    asyncio.run(run())


@dataset_app.command("validate")
def dataset_validate(
    which: str = typer.Option("all", help="Dataset to build: all|weather_panel|market_weather."),
    start: str | None = typer.Option(None, help="Inclusive start target date (YYYY-MM-DD)."),
    end: str | None = typer.Option(None, help="Inclusive end target date (YYYY-MM-DD)."),
) -> None:
    """Build in memory and print the validation report. Exits non-zero if any
    error-severity finding fired."""

    async def run() -> None:
        settings = get_settings()
        output = await _build_dataset(
            settings, which=which, start=_parse_date(start), end=_parse_date(end),
            version="validate",
        )
        typer.echo(json.dumps(output.validation.to_dict(), indent=2, default=str))
        if not output.validation.ok:
            raise typer.Exit(code=1)

    asyncio.run(run())


@dataset_app.command("stats")
def dataset_stats(
    which: str = typer.Option("all", help="Dataset to build: all|weather_panel|market_weather."),
    start: str | None = typer.Option(None, help="Inclusive start target date (YYYY-MM-DD)."),
    end: str | None = typer.Option(None, help="Inclusive end target date (YYYY-MM-DD)."),
) -> None:
    """Build in memory and print summary statistics."""

    async def run() -> None:
        settings = get_settings()
        output = await _build_dataset(
            settings, which=which, start=_parse_date(start), end=_parse_date(end),
            version="stats",
        )
        typer.echo(json.dumps(output.stats, indent=2, default=str))

    asyncio.run(run())


@dataset_app.command("export")
def dataset_export(
    which: str = typer.Option("all", help="Dataset to build: all|weather_panel|market_weather."),
    start: str | None = typer.Option(None, help="Inclusive start target date (YYYY-MM-DD)."),
    end: str | None = typer.Option(None, help="Inclusive end target date (YYYY-MM-DD)."),
    version: str | None = typer.Option(None, help="Dataset version (default: UTC timestamp)."),
    output_root: str | None = typer.Option(None, help="Export root (default: settings/env)."),
    export_format: str = typer.Option("parquet", "--format", help="parquet (duckdb is future)."),
) -> None:
    """Build and write a versioned dataset directory in the chosen format."""

    async def run() -> None:
        settings = get_settings()
        resolved_version = version or default_version()
        output = await _build_dataset(
            settings, which=which, start=_parse_date(start), end=_parse_date(end),
            version=resolved_version,
        )
        root = Path(output_root) if output_root else settings.dataset_root
        result = dataset_pipeline.export(
            output, root=root, version=resolved_version, fmt=ExportFormat(export_format)
        )
        _echo_summary(output)
        typer.echo(f"exported to {result.output_dir}")

    asyncio.run(run())


if __name__ == "__main__":
    app()
