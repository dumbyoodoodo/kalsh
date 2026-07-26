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
from urllib.parse import urlparse

import typer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalshi_weather.config import Settings, get_settings
from kalshi_weather.dataset import pipeline as dataset_pipeline
from kalshi_weather.dataset.export import ExportFormat, default_version
from kalshi_weather.dataset.provenance import EnvironmentPolicy as DatasetEnvironmentPolicy
from kalshi_weather.domain.time import utc_now
from kalshi_weather.ingestion.backfill import run_backfill
from kalshi_weather.ingestion.collector import run_collector_loop
from kalshi_weather.ingestion.metadata_revision import (
    run_metadata_revision,
    run_metadata_revision_loop,
)
from kalshi_weather.ingestion.price_backfill import run_price_backfill, run_price_sync_loop
from kalshi_weather.ingestion.weather_collector import run_weather_collector_loop
from kalshi_weather.kalshi.client import KalshiClient
from kalshi_weather.kalshi.orderbook import reconstruct_best_quote
from kalshi_weather.kalshi.provenance import resolve_kalshi_environment
from kalshi_weather.logging import configure_logging, get_logger
from kalshi_weather.observatory import (
    ObservatoryConfig,
    Severity,
    build_observatory_report,
)
from kalshi_weather.observatory.backup_health import BackupHealthConfig
from kalshi_weather.ops import alerting, backup, backup_retention, monitor, restart_policy
from kalshi_weather.ops.forecast_cadence import CadenceConfig, run_forecast_cadence
from kalshi_weather.ops.health import build_health_report
from kalshi_weather.ops.heartbeat import (
    classify_heartbeat_status,
    load_heartbeat_state,
    run_heartbeat,
    write_heartbeat_state,
)
from kalshi_weather.ops.price_coverage import build_price_coverage_report
from kalshi_weather.ops.quality import run_quality_checks
from kalshi_weather.ops.snapshot import create_snapshot, default_snapshot_version
from kalshi_weather.settlement.parser import parse_settlement
from kalshi_weather.settlement.resolver import (
    CompositeSettlementResolver,
    ParserSettlementResolver,
    ResolutionReport,
    load_parser_inputs,
)
from kalshi_weather.settlement.spec import PARSER_VERSION
from kalshi_weather.storage.database import create_engine, create_session_factory, session_scope
from kalshi_weather.storage.repositories import (
    save_market_snapshot,
    save_orderbook_snapshot,
    save_raw_payload,
    save_series,
    save_settlement_spec,
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
settlement_app = typer.Typer(help="Resolve markets to settlement specifications.")
ops_app = typer.Typer(help="Operations: health, data quality, snapshots, combined runner.")
backup_app = typer.Typer(help="PostgreSQL backup: finalize, retention, status.")
prices_app = typer.Typer(help="Historical market price (candlestick) ingestion.")
app.add_typer(series_app, name="series")
app.add_typer(markets_app, name="markets")
app.add_typer(orderbook_app, name="orderbook")
app.add_typer(collector_app, name="collector")
app.add_typer(weather_app, name="weather")
weather_app.add_typer(weather_stations_app, name="stations")
app.add_typer(dataset_app, name="dataset")
app.add_typer(settlement_app, name="settlement")
app.add_typer(prices_app, name="prices")
app.add_typer(ops_app, name="ops")
ops_app.add_typer(backup_app, name="backup")
experiment_app = typer.Typer(help="Run formal modeling experiments on frozen canonical datasets.")
app.add_typer(experiment_app, name="experiment")


@experiment_app.command("h0018")
def experiment_h0018(
    out_dir: str = typer.Option(
        ..., help="Immutable output directory for the experiment artifact."
    ),
) -> None:
    """Run the H0018 weather-vs-market baseline experiment (see HYPOTHESES.md).
    Read-only: builds a frozen decision grain, verifies leakage safety, fits the
    pre-registered models, and writes results.json + predictions. Trains/scores
    no orders; uses no liquidity features."""
    from kalshi_weather.experiments.runner import run_h0018

    async def run() -> None:
        settings = get_settings()
        configure_logging(settings.log_level)
        async with _open_session(settings) as session:
            result = await run_h0018(session, out_dir=Path(out_dir))
        typer.echo(
            f"H0018: rows={result['n_rows']} events={result['n_events']} "
            f"held_out={result['held_out_test_evaluable']} "
            f"grain_sha256={result['grain_sha256'][:12]} -> {out_dir}/results.json"
        )

    asyncio.run(run())


@experiment_app.command("readiness")
def experiment_readiness(
    hypothesis: str = typer.Argument("h0019", help="Which registered future experiment."),
    log_path: str | None = typer.Option(
        None, help="Append-only readiness-log JSONL (defaults to the registration dir)."
    ),
) -> None:
    """READ-ONLY readiness monitor for the H0019 future replication. Reports
    whether enough genuinely point-in-time data has accumulated to run the
    held-out test. It NEVER trains a model, generates test predictions, or
    computes a test Brier score -- coverage counts only. Each invocation is
    appended to a readiness log (no outcome-based analysis is recorded)."""
    if hypothesis.lower() != "h0019":
        raise typer.BadParameter("only 'h0019' is registered")
    import json as _json

    from kalshi_weather.dataset.builder import load_source_frames as _load_sources
    from kalshi_weather.dataset.provenance import EnvironmentPolicy as _EP
    from kalshi_weather.experiments import readiness as rd
    from kalshi_weather.settlement.labels import build_labels as _build_labels

    async def run() -> None:
        settings = get_settings()
        configure_logging(settings.log_level)
        now = utc_now().date()
        async with _open_session(settings) as session:
            labels = await _build_labels(session)
            sources = await _load_sources(session, env_policy=_EP())
        report = rd.compute_readiness(sources, labels, now=now)
        typer.echo(f"H0019 readiness as of {now}: {report.state}")
        for split in ("train", "val", "test"):
            c = report.detail["split_coverage"][split]
            typer.echo(
                f"  {split}: events={c['events']} rows={c['rows']} pos={c['pos']} neg={c['neg']}"
            )
        det = report.detail
        typer.echo(
            f"  completion_cov={det['completion_coverage']} "
            f"(complete {det['complete_in_windows']}/{det['eligible_in_windows']}) "
            f"days_until_test_end={det['days_until']['test_end']}"
        )
        failing = [k for k, v in report.conditions.items() if not v]
        typer.echo(f"  failing conditions: {failing or 'none'}")
        # append-only readiness log -- records state, NEVER any test analysis
        lp = (
            Path(log_path)
            if log_path
            else Path("docs/research/experiments/EXP-FUTURE-H0019/readiness_log.jsonl")
        )
        lp.parent.mkdir(parents=True, exist_ok=True)
        with lp.open("a") as f:
            f.write(_json.dumps({"as_of": str(now), "state": report.state}) + "\n")

    asyncio.run(run())


@asynccontextmanager
async def _open_session(settings: Settings) -> AsyncIterator[AsyncSession]:
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            yield session
    finally:
        await engine.dispose()


def _backup_dir(settings: Settings) -> Path:
    """Local backup destination -- `BACKUP_LOCAL_DIR` when set (the
    launchd-writable internal-disk location; see `Settings.backup_local_dir`),
    otherwise the original `<KALSHI_DATA_DIR>/backups/postgres` derivation.
    Mirrors `scripts/backup_postgres.sh`'s own resolution exactly."""
    if settings.backup_local_dir is not None:
        return settings.backup_local_dir
    base = settings.data_dir if settings.data_dir.is_absolute() else Path.cwd() / settings.data_dir
    return base / "backups" / "postgres"


def _backup_health_config(settings: Settings) -> BackupHealthConfig:
    backup_dir = _backup_dir(settings)
    return BackupHealthConfig(
        backup_dir=backup_dir,
        status_path=backup_dir / backup.STATUS_FILENAME,
        remote_type=settings.backup_remote_type,
        remote_filesystem_path=settings.backup_remote_path,
        remote_s3_bucket=settings.backup_s3_bucket,
        remote_s3_prefix=settings.backup_s3_prefix,
        stale_after_hours=settings.backup_stale_after_hours,
        remote_stale_after_hours=settings.backup_remote_stale_after_hours,
        disk_warning_free_gb=settings.backup_disk_warning_free_gb,
        disk_critical_free_gb=settings.backup_disk_critical_free_gb,
    )


def _build_client(settings: Settings, session: AsyncSession | None = None) -> KalshiClient:
    """The research/archival data client. Reads from the configured DATA
    environment (`kalshi_data_env`, default production; ADR 0014) via the
    centralized `kalshi_data_base_url`, so discovery, snapshots, trades, order
    books, and settlement all share one environment. It is UNAUTHENTICATED:
    every endpoint this client uses is public (no `require_auth=True` anywhere),
    and demo signing credentials are reserved for the separate future execution
    client -- they are deliberately never attached here, so a demo execution
    client and the production data client can never be confused."""
    base_url = settings.kalshi_data_base_url

    raw_payload_sink = None
    if session is not None:
        environment = resolve_kalshi_environment(base_url).value  # ADR 0013

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
                environment=environment,
            )
            return raw.id

        raw_payload_sink = sink

    return KalshiClient(
        base_url=base_url,
        environment=settings.kalshi_data_env,
        min_request_interval_seconds=settings.kalshi_min_request_interval_seconds,
        raw_payload_sink=raw_payload_sink,
    )


def _build_price_client(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> KalshiClient:
    """Client for price_backfill.run_price_backfill, whose per-market writes
    use their own short-lived sessions (one commit per market -- see
    ingestion/price_backfill.py). The raw-payload sink mirrors that: it opens
    its own mini-transaction per payload rather than sharing one long-lived
    session, so raw rows are always committed before the normalized candle
    rows that reference them (same FK-ordering rationale as weather_backfill's
    sink -- see `weather_backfill` below).

    Reads from the same centralized DATA environment as every other data
    client (`kalshi_data_base_url`, default production; ADR 0014). A market's
    candlestick/trade history only exists in the environment it was discovered
    in, and the entire settled-market archive this runs against comes from
    production's public "Climate and Weather" category (confirmed live,
    docs/adr/0002-ingestion-collector.md). No credentials are needed or used:
    candlestick data for a public market is unauthenticated, same as every
    other read in this client."""
    base_url = settings.kalshi_data_base_url
    environment = resolve_kalshi_environment(base_url).value  # ADR 0013

    async def sink(source: str, endpoint: str, request_key: str, status: int, payload: Any) -> int:
        async with session_scope(session_factory) as sink_session:
            raw = await save_raw_payload(
                sink_session,
                source=source,
                endpoint_or_channel=endpoint,
                request_key=request_key,
                http_status=status,
                payload_json=payload,
                environment=environment,
            )
            return raw.id

    return KalshiClient(
        base_url=base_url,
        environment=settings.kalshi_data_env,
        min_request_interval_seconds=settings.kalshi_min_request_interval_seconds,
        raw_payload_sink=sink,
    )


def _log_data_environment(settings: Settings) -> None:
    """Make the resolved research-data environment visible at startup (ADR
    0014). Logs the env label, the API host, and the provenance value new rows
    will carry -- host and env are public config, never secrets."""
    host = urlparse(settings.kalshi_data_base_url).hostname or "unknown"
    logger.info(
        "kalshi.data_environment",
        data_env=settings.kalshi_data_env.value,
        host=host,
        row_provenance=resolve_kalshi_environment(settings.kalshi_data_base_url).value,
        trading_env=settings.kalshi_env.value,
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
                environment=client.source_environment,
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
                environment=client.source_environment,
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

        _log_data_environment(settings)
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
                trade_bootstrap_lookback_days=settings.initial_trade_bootstrap_lookback_days,
                interval_seconds=(
                    interval if interval is not None else settings.collector_interval_seconds
                ),
                stop_event=stop_event,
                max_cycles=1 if once else None,
                settle_check_limit=settings.collector_settle_check_limit,
                settle_check_days=settings.collector_settle_check_days,
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

    return NwsProvider(
        user_agent=settings.weather_user_agent,
        min_request_interval_seconds=settings.weather_min_request_interval_seconds,
        raw_payload_sink=raw_payload_sink,
    )


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
        resolved_interval = interval if interval is not None else settings.weather_interval_seconds
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
    settings: Settings,
    *,
    which: str,
    start: date | None,
    end: date | None,
    version: str,
    canonical: bool = False,
) -> dataset_pipeline.BuildOutput:
    # Mappings come from the automated settlement parser (Milestone 2b), with
    # the config file retained as a manual override layer on top -- see
    # settlement/resolver.py and docs/adr/0005-settlement-resolution.md.
    resolver = CompositeSettlementResolver(settings.dataset_market_map_path)
    async with _open_session(settings) as session:
        mappings, report = await resolver.resolve_report(session)
        return await dataset_pipeline.build(
            session,
            database_url=settings.database_url,
            mappings=mappings,
            which=which,
            start=start,
            end=end,
            version=version,
            # Canonical build: production-only liquidity, provenance-aware
            # (ADR 0014). Default policy; explicit here so the choice is visible.
            env_policy=DatasetEnvironmentPolicy() if canonical else None,
            resolver_meta={
                "resolver": "parser+overrides",
                "parser_version": PARSER_VERSION,
                "resolution_counts": report.counts(),
                "overridden_markets": sorted(report.overridden),
            },
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
    which: str = typer.Option(
        "all",
        help=(
            "Dataset to build: all|weather_panel|market_weather|market_prices|market_price_weather."
        ),
    ),
    start: str | None = typer.Option(None, help="Inclusive start target date (YYYY-MM-DD)."),
    end: str | None = typer.Option(None, help="Inclusive end target date (YYYY-MM-DD)."),
    version: str | None = typer.Option(None, help="Dataset version (default: UTC timestamp)."),
    output_root: str | None = typer.Option(None, help="Export root (default: settings/env)."),
    export: bool = typer.Option(True, help="Write Parquet + manifest to disk."),
    canonical: bool = typer.Option(
        False,
        "--canonical/--no-canonical",
        help="Canonical build: liquidity from verified production only (ADR 0014).",
    ),
) -> None:
    """Build the research dataset(s): point-in-time join, validate, compute
    stats, and (by default) export a versioned Parquet directory. Use
    --canonical for the provenance-aware research dataset (production-only
    liquidity). See docs/runbooks/dataset.md."""

    async def run() -> None:
        settings = get_settings()
        configure_logging(settings.log_level)
        resolved_version = version or default_version()
        output = await _build_dataset(
            settings,
            which=which,
            start=_parse_date(start),
            end=_parse_date(end),
            version=resolved_version,
            canonical=canonical,
        )
        _echo_summary(output)
        if export:
            root = Path(output_root) if output_root else settings.ensure_dataset_root()
            result = dataset_pipeline.export(
                output, root=root, version=resolved_version, fmt=ExportFormat.PARQUET
            )
            typer.echo(f"exported to {result.output_dir}")
        if not output.validation.ok:
            raise typer.Exit(code=1)

    asyncio.run(run())


@dataset_app.command("validate")
def dataset_validate(
    which: str = typer.Option(
        "all",
        help=(
            "Dataset to build: all|weather_panel|market_weather|market_prices|market_price_weather."
        ),
    ),
    start: str | None = typer.Option(None, help="Inclusive start target date (YYYY-MM-DD)."),
    end: str | None = typer.Option(None, help="Inclusive end target date (YYYY-MM-DD)."),
) -> None:
    """Build in memory and print the validation report. Exits non-zero if any
    error-severity finding fired."""

    async def run() -> None:
        settings = get_settings()
        output = await _build_dataset(
            settings,
            which=which,
            start=_parse_date(start),
            end=_parse_date(end),
            version="validate",
        )
        typer.echo(json.dumps(output.validation.to_dict(), indent=2, default=str))
        if not output.validation.ok:
            raise typer.Exit(code=1)

    asyncio.run(run())


@dataset_app.command("stats")
def dataset_stats(
    which: str = typer.Option(
        "all",
        help=(
            "Dataset to build: all|weather_panel|market_weather|market_prices|market_price_weather."
        ),
    ),
    start: str | None = typer.Option(None, help="Inclusive start target date (YYYY-MM-DD)."),
    end: str | None = typer.Option(None, help="Inclusive end target date (YYYY-MM-DD)."),
) -> None:
    """Build in memory and print summary statistics."""

    async def run() -> None:
        settings = get_settings()
        output = await _build_dataset(
            settings,
            which=which,
            start=_parse_date(start),
            end=_parse_date(end),
            version="stats",
        )
        typer.echo(json.dumps(output.stats, indent=2, default=str))

    asyncio.run(run())


@dataset_app.command("export")
def dataset_export(
    which: str = typer.Option(
        "all",
        help=(
            "Dataset to build: all|weather_panel|market_weather|market_prices|market_price_weather."
        ),
    ),
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
            settings,
            which=which,
            start=_parse_date(start),
            end=_parse_date(end),
            version=resolved_version,
        )
        root = Path(output_root) if output_root else settings.ensure_dataset_root()
        result = dataset_pipeline.export(
            output, root=root, version=resolved_version, fmt=ExportFormat(export_format)
        )
        _echo_summary(output)
        typer.echo(f"exported to {result.output_dir}")

    asyncio.run(run())


def _echo_spec(spec: Any) -> None:
    typer.echo(f"market:       {spec.market_ticker}")
    typer.echo(f"series:       {spec.series_ticker}")
    typer.echo(f"status:       {spec.status.value} (confidence: {spec.confidence.value})")
    typer.echo(f"station:      {spec.station_id or '-'} ({spec.city or '-'})")
    typer.echo(f"variable:     {spec.variable or '-'}")
    typer.echo(f"target_date:  {spec.target_date.isoformat() if spec.target_date else '-'}")
    typer.echo(f"source:       {spec.settlement_source or '-'}")
    typer.echo(f"source_url:   {spec.source_url or '-'}")
    typer.echo(f"window/unit:  {spec.observation_window or '-'} / {spec.unit or '-'}")
    typer.echo(f"rounding:     {spec.rounding_rule or '-'}")
    typer.echo(f"parser:       v{spec.parser_version}  rules_hash={spec.rules_hash[:12]}...")
    for note in spec.notes:
        typer.echo(f"note:         {note}")


async def _persist_specs(session: AsyncSession, specs: list[Any]) -> tuple[int, int]:
    saved = duplicate = 0
    for s in specs:
        result = await save_settlement_spec(
            session,
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
        if result.was_duplicate:
            duplicate += 1
        else:
            saved += 1
    return saved, duplicate


@settlement_app.command("resolve")
def settlement_resolve(
    ticker: str,
    persist: bool = typer.Option(False, "--persist", help="Store the spec in settlement_specs."),
) -> None:
    """Resolve one market (by ticker) from its stored snapshot and print the
    resulting settlement specification."""

    async def run() -> None:
        settings = get_settings()
        async with _open_session(settings) as session:
            inputs = await load_parser_inputs(session)
            match = [(s, m) for s, m in inputs if m.ticker == ticker]
            if not match:
                typer.echo(f"no stored market snapshot for ticker {ticker!r}", err=True)
                raise typer.Exit(code=1)
            spec = parse_settlement(*match[0])
            _echo_spec(spec)
            if persist:
                saved, duplicate = await _persist_specs(session, [spec])
                typer.echo(f"persisted: {saved} new, {duplicate} already stored")
            if not spec.is_resolved:
                raise typer.Exit(code=1)

    asyncio.run(run())


@settlement_app.command("resolve-all")
def settlement_resolve_all(
    persist: bool = typer.Option(False, "--persist", help="Store specs in settlement_specs."),
) -> None:
    """Resolve every market with a stored snapshot; print a per-status summary."""

    async def run() -> None:
        settings = get_settings()
        async with _open_session(settings) as session:
            specs = await ParserSettlementResolver().resolve_specs(session)
            report = ResolutionReport(specs=specs, overridden=[])
            typer.echo(json.dumps(report.counts(), indent=2))
            if persist:
                saved, duplicate = await _persist_specs(session, specs)
                typer.echo(f"persisted: {saved} new, {duplicate} already stored")

    asyncio.run(run())


@settlement_app.command("report")
def settlement_report() -> None:
    """Full resolution report (parser + overrides): resolved, ambiguous,
    unresolved, and unsupported markets, with reasons."""

    async def run() -> None:
        settings = get_settings()
        resolver = CompositeSettlementResolver(settings.dataset_market_map_path)
        async with _open_session(settings) as session:
            _, report = await resolver.resolve_report(session)
        typer.echo(json.dumps(report.to_dict(), indent=2, default=str))

    asyncio.run(run())


@weather_app.command("backfill")
def weather_backfill(
    station: str = typer.Option(..., "--station", help="station_id from the registry."),
    start: str = typer.Option(..., "--start", help="Inclusive start date (YYYY-MM-DD)."),
    end: str = typer.Option(..., "--end", help="Inclusive end date (YYYY-MM-DD)."),
    chunk_days: int = typer.Option(30, "--chunk-days", help="Days per committed chunk."),
    skip_covered: bool = typer.Option(
        True,
        "--skip-covered/--no-skip-covered",
        help="Skip chunks whose every day already has stored observations (resume).",
    ),
) -> None:
    """Deep historical observation backfill: chunked, resumable, partial-failure
    tolerant. See docs/runbooks/operations.md for runtime expectations."""
    try:
        target_station = get_station(station)
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

        # The raw-payload sink commits each payload in its own small
        # transaction, so per-chunk observation commits (run_backfill uses
        # separate sessions) always reference already-committed raw rows --
        # a single long-lived sink session would break that FK ordering.
        async def sink(
            source: str, endpoint: str, request_key: str, status: int, payload: Any
        ) -> int:
            async with session_scope(session_factory) as sink_session:
                raw = await save_raw_payload(
                    sink_session,
                    source=source,
                    endpoint_or_channel=endpoint,
                    request_key=request_key,
                    http_status=status,
                    payload_json=payload,
                )
                return raw.id

        try:
            provider = NwsProvider(
                user_agent=settings.weather_user_agent,
                min_request_interval_seconds=settings.weather_min_request_interval_seconds,
                raw_payload_sink=sink,
            )
            async with provider:
                report = await run_backfill(
                    session_factory=session_factory,
                    provider=provider,
                    station=target_station,
                    start=date.fromisoformat(start),
                    end=date.fromisoformat(end),
                    chunk_days=chunk_days,
                    skip_covered=skip_covered,
                    stop_event=stop_event,
                )
        finally:
            await engine.dispose()
        typer.echo(json.dumps(report.totals(), indent=2))
        for chunk in report.failed_chunks:
            typer.echo(f"FAILED {chunk.start}..{chunk.end}: {chunk.error}", err=True)
        if not report.ok:
            typer.echo("re-run the same command to retry failed chunks", err=True)
            raise typer.Exit(code=1)

    asyncio.run(run())


@prices_app.command("backfill")
def prices_backfill(
    ticker: str | None = typer.Option(
        None, "--ticker", help="Backfill only these market ticker(s), comma-separated."
    ),
    event: str | None = typer.Option(None, "--event", help="Backfill only this event_ticker."),
    start: str | None = typer.Option(
        None, "--start", help="Inclusive close-date lower bound (YYYY-MM-DD)."
    ),
    end: str | None = typer.Option(
        None, "--end", help="Inclusive close-date upper bound (YYYY-MM-DD)."
    ),
    resolution: int | None = typer.Option(
        None, "--resolution", help="Candle resolution in minutes (default: settings/env)."
    ),
    limit: int | None = typer.Option(None, "--limit", help="Cap the number of markets attempted."),
    skip_covered: bool = typer.Option(
        True,
        "--skip-covered/--no-skip-covered",
        help="Skip markets whose candle coverage already reaches close (resume).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Classify without writing to the database."
    ),
) -> None:
    """Historical price (candlestick) backfill over settled markets, oldest
    close_time first (the rolling retention window makes the oldest markets
    the highest risk). See docs/runbooks/price_ingestion.md."""

    async def run() -> None:
        settings = get_settings()
        configure_logging(settings.log_level)
        engine = create_engine(settings.database_url)
        session_factory = create_session_factory(engine)
        resolved_resolution = resolution or settings.price_candle_resolution_minutes
        try:
            client = _build_price_client(settings, session_factory)
            async with client:
                report = await run_price_backfill(
                    session_factory=session_factory,
                    client=client,
                    tickers=[t.strip() for t in ticker.split(",")] if ticker else None,
                    event_ticker=event,
                    start_date=date.fromisoformat(start) if start else None,
                    end_date=date.fromisoformat(end) if end else None,
                    resolution_minutes=resolved_resolution,
                    limit=limit,
                    skip_covered=skip_covered,
                    dry_run=dry_run,
                )
        finally:
            await engine.dispose()

        typer.echo(json.dumps(report.totals(), indent=2))
        for r in report.failed:
            typer.echo(f"FAILED {r.market_ticker}: {r.error}", err=True)
        if not report.ok:
            typer.echo("re-run the same command to retry failed markets", err=True)
            raise typer.Exit(code=1)

    asyncio.run(run())


@ops_app.command("revision")
def ops_revision(
    ticker: str | None = typer.Option(
        None, "--ticker", help="Revise only these market ticker(s), comma-separated."
    ),
    limit: int | None = typer.Option(
        None, "--limit", help="Cap the number of markets attempted this run."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Classify without writing snapshots or verifications."
    ),
) -> None:
    """Post-expiration settled-metadata revision pass (ADR 0011).

    Kalshi keeps revising volume/open_interest/result after publishing
    status="finalized"; a market's own expiration_time (~7 days after close)
    is when they stop. This re-checks settled markets past that point and
    appends a snapshot only when something actually changed. Never rewrites a
    historical row. See docs/runbooks/settled_metadata_revision.md."""

    async def run() -> None:
        settings = get_settings()
        configure_logging(settings.log_level)
        engine = create_engine(settings.database_url)
        session_factory = create_session_factory(engine)
        try:
            client = _build_price_client(settings, session_factory)
            async with client:
                report = await run_metadata_revision(
                    session_factory=session_factory,
                    client=client,
                    tickers=[t.strip() for t in ticker.split(",")] if ticker else None,
                    limit=limit,
                    retention_days=settings.price_observed_retention_days,
                    dry_run=dry_run,
                )
        finally:
            await engine.dispose()
        typer.echo(json.dumps(report.totals(), indent=2))
        for r in report.results:
            if r.changed_fields:
                typer.echo(f"CHANGED {r.market_ticker}: {','.join(r.changed_fields)}")
            elif r.outcome in ("market_removed", "retention_expired"):
                typer.echo(f"{r.outcome.upper()} {r.market_ticker}")

    asyncio.run(run())


@prices_app.command("coverage")
def prices_coverage(
    resolution: int | None = typer.Option(
        None, "--resolution", help="Candle resolution in minutes (default: settings/env)."
    ),
) -> None:
    """Measured price-coverage report: markets attempted/captured, candle
    counts, coverage by event date/variable, missing intervals, markets
    likely lost to retention, and duplicate/settlement-label checks. Every
    number is a real query result -- see docs/runbooks/price_ingestion.md."""

    async def run() -> None:
        settings = get_settings()
        async with _open_session(settings) as session:
            report = await build_price_coverage_report(
                session,
                resolution_minutes=resolution or settings.price_candle_resolution_minutes,
                observed_retention_days=settings.price_observed_retention_days,
            )
        typer.echo(json.dumps(report.to_dict(), indent=2, default=str))

    asyncio.run(run())


@ops_app.command("quality")
def ops_quality() -> None:
    """Run data-quality checks against the store; exit non-zero on errors."""

    async def run() -> None:
        settings = get_settings()
        async with _open_session(settings) as session:
            report = await run_quality_checks(session)
        typer.echo(json.dumps(report.to_dict(), indent=2, default=str))
        if not report.ok:
            raise typer.Exit(code=1)

    asyncio.run(run())


@ops_app.command("forecast-cadence")
def ops_forecast_cadence(
    expected_per_day: int = typer.Option(
        2,
        help=(
            "Issuance-window floor per station-local day (minimum cadence "
            "verification of NWS's scheduled AM/PM packages). Values above 2 are "
            "clamped: the stored issue_time is the irregular, clustered NWS "
            "updateTime stream, so finer equal partitions produce false "
            "missing-cycle alerts, not better monitoring. Long silent stretches "
            "are covered separately by the schedule-agnostic issuance-gap alert."
        ),
    ),
) -> None:
    """Verify forecast-collection cadence with two complementary
    invariants: minimum cadence verification (the twice-daily NWS
    scheduled-floor window check) and schedule-agnostic outage detection
    (the maximum-gap alert), plus duplicates, ordering, delays, and
    per-station health. Forecast history has no backfill source, so
    capture gaps surfaced here are permanent unless fixed promptly."""

    async def run() -> None:
        if expected_per_day > 2:
            typer.echo(
                "note: --expected-per-day above 2 is clamped to 2 (see command help); "
                "the issuance-gap alert covers irregular-schedule outages.",
                err=True,
            )
        settings = get_settings()
        async with _open_session(settings) as session:
            report = await run_forecast_cadence(
                session, config=CadenceConfig(expected_issuances_per_day=expected_per_day)
            )
        typer.echo(json.dumps(report.to_dict(), indent=2, default=str))

    asyncio.run(run())


@ops_app.command("health")
def ops_health(
    as_json: bool = typer.Option(False, "--json", help="Emit the full report as JSON."),
) -> None:
    """Summarize platform health: collectors, freshness, coverage, quality."""

    async def run() -> None:
        settings = get_settings()
        async with _open_session(settings) as session:
            report = await build_health_report(
                session,
                kalshi_interval_seconds=settings.collector_interval_seconds,
                weather_interval_seconds=settings.weather_interval_seconds,
                stale_after_intervals=settings.ops_stale_after_intervals,
                price_sync_interval_seconds=settings.price_sync_interval_seconds,
                price_resolution_minutes=settings.price_candle_resolution_minutes,
                price_observed_retention_days=settings.price_observed_retention_days,
                price_retention_warning_buffer_days=settings.price_retention_warning_buffer_days,
            )
        if as_json:
            typer.echo(json.dumps(report.to_dict(), indent=2, default=str))
            return
        typer.echo(f"generated: {report.generated_at}")
        for c in report.collectors:
            status = "STALE" if c.stale else "ok"
            rate = f"{c.success_rate_recent:.0%}" if c.success_rate_recent is not None else "-"
            typer.echo(
                f"collector {c.collector:8s} [{status}] last={c.last_run_at or 'never'} "
                f"runs={c.runs_recorded} success_rate={rate}"
            )
        for name, ts in report.newest_data.items():
            typer.echo(f"newest {name:22s} {ts or '-'}")
        size = report.database_size_bytes
        typer.echo(f"database size: {size / 1_048_576:.1f} MiB" if size else "database size: n/a")
        sc = report.station_coverage
        typer.echo(f"stations with data: {sc['with_observations']}/{sc['registry']}")
        st = report.settlement_coverage
        typer.echo(
            f"settlement: {st['resolved']}/{st['markets']} resolved "
            f"(backlog {st['unresolved_backlog']}, confidence {st['confidence']})"
        )
        for sid, info in report.dataset_completeness.items():
            typer.echo(
                f"completeness {sid}: {info['observation_days']} days, "
                f"range {info['range'] or '-'}, coverage {info['coverage']}"
            )
        pr = report.price_retention
        oldest = pr["oldest_uncaptured_market"]
        typer.echo(
            "price retention: oldest_uncaptured="
            + (f"{oldest['market_ticker']} ({oldest['age_days']}d)" if oldest else "-")
            + f" nearing_expiry={pr['markets_nearing_expiry']}"
            f" incomplete_coverage={pr['markets_incomplete_coverage']}"
        )
        typer.echo(
            f"quality: {'OK' if report.quality_ok else 'ERRORS'} "
            + json.dumps(report.quality_counts)
        )

    asyncio.run(run())


@ops_app.command("observatory")
def ops_observatory(
    as_json: bool = typer.Option(False, "--json", help="Emit the full report as JSON."),
) -> None:
    """Data quality observatory: continuous verification, across all five
    collection streams, that incoming data remain scientifically usable
    -- archive continuity, missing collection cycles, duplicate records,
    timestamp monotonicity, point-in-time consistency, schema drift,
    station/issuance-schedule drift, parser failures, forecast
    eligibility, and observation completeness. Detection only: no
    automatic fixes, no research statistic, no hypothesis executed.
    Findings are classified info/warning/critical; exit non-zero if any
    critical finding fired."""

    async def run() -> None:
        settings = get_settings()
        async with _open_session(settings) as session:
            report = await build_observatory_report(
                session,
                ObservatoryConfig(
                    kalshi_interval_seconds=settings.collector_interval_seconds,
                    weather_interval_seconds=settings.weather_interval_seconds,
                    price_sync_interval_seconds=settings.price_sync_interval_seconds,
                    stale_after_intervals=settings.ops_stale_after_intervals,
                    cadence_run_window_hours=settings.cadence_run_window_hours,
                    backup_health=_backup_health_config(settings),
                ),
            )
        if as_json:
            typer.echo(json.dumps(report.to_dict(), indent=2, default=str))
        else:
            typer.echo(f"generated: {report.generated_at}")
            typer.echo(f"status: {report.status.value.upper()}")
            for severity in (Severity.CRITICAL, Severity.WARNING, Severity.INFO):
                findings = report.by_severity(severity)
                if severity is Severity.INFO and not as_json:
                    continue  # info-level findings are noisy for the terminal view
                for f in findings:
                    typer.echo(
                        f"[{f.severity.value.upper():8s}] {f.domain:12s} {f.check:32s} "
                        f"count={f.count}  {f.message}"
                    )
        if report.status is Severity.CRITICAL:
            raise typer.Exit(code=1)

    asyncio.run(run())


@ops_app.command("monitor")
def ops_monitor(
    state_path: str = typer.Option(..., help="Path to the persisted alert-state JSON."),
    history_path: str = typer.Option(..., help="Path to the append-only alert-history JSONL log."),
    as_json: bool = typer.Option(False, "--json", help="Emit the run's decision as JSON."),
) -> None:
    """Run the data quality observatory and, on a severity transition, send
    an alert via the configured transport (ALERT_TRANSPORT; see
    docs/runbooks/monitoring_alerting.md). Suppresses repeat alerts while
    severity is unchanged; sends a recovery notification on returning to
    INFO. Detection and notification only -- never repairs anything, never
    executes a hypothesis. A non-blocking lock (<state-path>.lock) skips
    this run rather than overlapping with one still in progress -- intended
    to run on a schedule (scripts/service/monitor.sh under launchd), not
    interactively. Exit non-zero iff the observatory's current status is
    CRITICAL."""
    lock_path = Path(f"{state_path}.lock")
    lock_handle = monitor.try_acquire_lock(lock_path)
    if lock_handle is None:
        typer.echo("another `ops monitor` run is still in progress; skipping this cycle")
        return

    async def run() -> None:
        settings = get_settings()
        now = utc_now()
        async with _open_session(settings) as session:
            report = await build_observatory_report(
                session,
                ObservatoryConfig(
                    kalshi_interval_seconds=settings.collector_interval_seconds,
                    weather_interval_seconds=settings.weather_interval_seconds,
                    price_sync_interval_seconds=settings.price_sync_interval_seconds,
                    stale_after_intervals=settings.ops_stale_after_intervals,
                    cadence_run_window_hours=settings.cadence_run_window_hours,
                    backup_health=_backup_health_config(settings),
                ),
            )
        prior = monitor.load_state(Path(state_path))
        decision = monitor.decide_alert(report.status, prior, now=now)

        delivered: bool | None = None
        detail = "suppressed (no severity transition)"
        if decision.should_alert:
            telegram = None
            if settings.alert_telegram_bot_token and settings.alert_telegram_chat_id:
                # Unwrapped here and nowhere else: TelegramConfig is consumed
                # immediately by send_alert, which puts the token only in the
                # request URL (ops/alerting.py) and never in a log or result.
                telegram = alerting.TelegramConfig(
                    bot_token=settings.alert_telegram_bot_token.get_secret_value(),
                    chat_id=settings.alert_telegram_chat_id,
                )
            message = monitor.format_alert_message(report, decision)
            result = await alerting.send_alert(settings.alert_transport, message, telegram=telegram)
            delivered, detail = result.delivered, result.detail

        monitor.save_state(Path(state_path), decision.new_state)
        monitor.append_history(
            Path(history_path),
            timestamp=now.isoformat(),
            severity=report.status,
            transition=decision.transition,
            reason=monitor.summarize_reason(report, decision),
            delivered=delivered,
            detail=detail,
        )

        if as_json:
            typer.echo(
                json.dumps(
                    {
                        "status": report.status.value,
                        "transition": decision.transition,
                        "alerted": decision.should_alert,
                        "delivered": delivered,
                        "detail": detail,
                    },
                    indent=2,
                )
            )
        else:
            typer.echo(
                f"status={report.status.value} transition={decision.transition} "
                f"alerted={decision.should_alert} delivered={delivered} detail={detail!r}"
            )
        if report.status is Severity.CRITICAL:
            raise typer.Exit(code=1)

    try:
        asyncio.run(run())
    finally:
        lock_handle.close()


@ops_app.command("heartbeat")
def ops_heartbeat(
    state_path: str | None = typer.Option(
        None, help="Persist the attempt outcome to this JSON file for `ops heartbeat-status`."
    ),
) -> None:
    """Emit an external uptime heartbeat (dead-man's-switch). Pings HEARTBEAT_URL
    only while the collector is healthy; withholds the ping if the machine is
    off (nothing runs), wedged, or the DB is unreachable, so an EXTERNAL monitor
    alerts. This is the off-device complement to `ops monitor`/Telegram, which
    run on this machine and can't fire when it's dead. Intended to run on a
    short schedule under launchd (scripts/service/heartbeat.sh). Always exits 0
    -- the signal is the ping's presence/absence, not this process's exit code.
    Sends a network request (the ping); `ops heartbeat-status` does not.
    See docs/runbooks/monitoring_alerting.md."""

    async def run() -> None:
        settings = get_settings()
        now = utc_now()
        engine = create_engine(settings.database_url)
        session_factory = create_session_factory(engine)
        try:
            result = await run_heartbeat(settings, session_factory, now=now)
        finally:
            await engine.dispose()
        if state_path is not None:
            write_heartbeat_state(Path(state_path), result, now)
        typer.echo(f"heartbeat outcome={result.outcome} emitted={result.emitted} {result.detail}")

    asyncio.run(run())


@ops_app.command("heartbeat-status")
def ops_heartbeat_status(
    state_path: str = typer.Option(
        ..., help="Path to the heartbeat state JSON written by ops heartbeat."
    ),
    period_seconds: float = typer.Option(300.0, help="Heartbeat ping interval, seconds."),
    grace_seconds: float = typer.Option(300.0, help="Extra staleness grace, seconds."),
    url_configured: bool | None = typer.Option(
        None,
        "--url-configured/--no-url-configured",
        help="Whether HEARTBEAT_URL is set (caller supplies it; default: read from settings).",
    ),
    agent_running: bool = typer.Option(
        False,
        "--agent-running/--no-agent-running",
        help="Whether the launchd heartbeat agent is running (supplied by status.sh).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit the status report as JSON."),
) -> None:
    """Read-only heartbeat status: report the last attempt and classify the
    heartbeat into one of not-configured / agent-not-running / no-attempt /
    last-attempt-failed / healthy, plus a staleness warning. Reads only the
    local state file and the supplied facts -- never sends a network request
    and never prints HEARTBEAT_URL. Intended to be called by
    scripts/service/status.sh, which supplies the launchd/config facts."""
    configured = (
        (get_settings().heartbeat_url is not None) if url_configured is None else url_configured
    )
    state = load_heartbeat_state(Path(state_path))
    report = classify_heartbeat_status(
        url_configured=configured,
        agent_running=agent_running,
        state=state,
        now=utc_now(),
        period_seconds=period_seconds,
        grace_seconds=grace_seconds,
    )
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "label": report.label,
                    "stale": report.stale,
                    "collector_healthy": report.collector_healthy,
                    "last_attempt_at": (
                        report.last_attempt_at.isoformat() if report.last_attempt_at else None
                    ),
                    "last_outcome": report.last_outcome,
                    "last_success_at": (
                        report.last_success_at.isoformat() if report.last_success_at else None
                    ),
                    "summary": report.summary,
                },
                indent=2,
            )
        )
        return
    health = (
        "n/a"
        if report.collector_healthy is None
        else ("healthy" if report.collector_healthy else "unhealthy")
    )
    if report.last_attempt_at is not None:
        typer.echo(
            f"  last attempt: {report.last_attempt_at.isoformat()} "
            f"outcome={report.last_outcome} (collector {health})"
        )
        last_success = report.last_success_at.isoformat() if report.last_success_at else "never"
        typer.echo(f"  last success: {last_success}")
    typer.echo(f"  status: {report.label.upper()} -- {report.summary}")


@ops_app.command("snapshot")
def ops_snapshot(
    version: str | None = typer.Option(None, help="Snapshot version (default: snapshot-YYYYMMDD)."),
    output_root: str | None = typer.Option(None, help="Export root (default: settings/env)."),
) -> None:
    """Cut a versioned daily research snapshot: dataset export + manifest +
    validation + stats + quality + ops sidecars."""

    async def run() -> None:
        settings = get_settings()
        configure_logging(settings.log_level)
        resolved_version = version or default_snapshot_version()
        root = Path(output_root) if output_root else settings.ensure_dataset_root()
        async with _open_session(settings) as session:
            result = await create_snapshot(
                session,
                database_url=settings.database_url,
                overrides_path=settings.dataset_market_map_path,
                root=root,
                version=resolved_version,
                kalshi_interval_seconds=settings.collector_interval_seconds,
                weather_interval_seconds=settings.weather_interval_seconds,
                stale_after_intervals=settings.ops_stale_after_intervals,
            )
        typer.echo(f"snapshot {result.version} -> {result.output_dir}")
        typer.echo(f"dataset_ok={result.dataset_ok} quality_ok={result.quality_ok}")
        if not (result.dataset_ok and result.quality_ok):
            raise typer.Exit(code=1)

    asyncio.run(run())


@ops_app.command("run")
def ops_run(
    kalshi_interval: float | None = typer.Option(None, help="Kalshi cycle interval seconds."),
    weather_interval: float | None = typer.Option(None, help="Weather cycle interval seconds."),
    price_sync_interval: float | None = typer.Option(
        None, help="Price sync cycle interval seconds."
    ),
) -> None:
    """Run the Kalshi, weather, and price-sync collectors concurrently in one
    supervised process (the recommended long-running deployment; see
    docs/runbooks/operations.md). Price sync is a bounded, oldest-first pass
    over newly-settled markets each cycle -- this is what keeps candlestick
    history from aging out of the rolling retention window uncaptured (see
    docs/research/investigations/INV-20260721-price-history-recovery.md)."""

    async def run() -> None:
        settings = get_settings()
        configure_logging(settings.log_level)
        engine = create_engine(settings.database_url)
        session_factory = create_session_factory(engine)
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

        _log_data_environment(settings)
        logger.info(
            "ops.run.starting",
            kalshi_interval=kalshi_interval or settings.collector_interval_seconds,
            weather_interval=weather_interval or settings.weather_interval_seconds,
            price_sync_interval=price_sync_interval or settings.price_sync_interval_seconds,
            revision_interval=settings.revision_sync_interval_seconds,
        )
        try:
            await asyncio.gather(
                run_collector_loop(
                    session_factory=session_factory,
                    client_factory=lambda session: _build_client(settings, session),
                    category=settings.collector_category,
                    market_status=settings.collector_market_status,
                    trade_bootstrap_lookback_days=settings.initial_trade_bootstrap_lookback_days,
                    interval_seconds=kalshi_interval or settings.collector_interval_seconds,
                    stop_event=stop_event,
                    settle_check_limit=settings.collector_settle_check_limit,
                    settle_check_days=settings.collector_settle_check_days,
                ),
                run_weather_collector_loop(
                    session_factory=session_factory,
                    provider_factory=lambda session: _build_weather_provider(settings, session),
                    stations=None,
                    backfill_days=settings.weather_backfill_days,
                    interval_seconds=weather_interval or settings.weather_interval_seconds,
                    stop_event=stop_event,
                ),
                run_price_sync_loop(
                    session_factory=session_factory,
                    client_factory=lambda: _build_price_client(settings, session_factory),
                    resolution_minutes=settings.price_candle_resolution_minutes,
                    limit_per_cycle=settings.price_sync_limit_per_cycle,
                    interval_seconds=price_sync_interval or settings.price_sync_interval_seconds,
                    stop_event=stop_event,
                ),
                run_metadata_revision_loop(
                    session_factory=session_factory,
                    client_factory=lambda: _build_price_client(settings, session_factory),
                    limit_per_cycle=settings.revision_sync_limit_per_cycle,
                    retention_days=settings.price_observed_retention_days,
                    interval_seconds=settings.revision_sync_interval_seconds,
                    stop_event=stop_event,
                ),
            )
        finally:
            await engine.dispose()
        logger.info("ops.run.stopped")

    asyncio.run(run())


@ops_app.command("restart-check")
def ops_restart_check(
    manifest: str = typer.Option(
        ...,
        help="Manifest written at service start (usually <KALSHI_LOG_DIR>/collector.manifest.json;"
        " operators should prefer scripts/service/check_restart.sh, which supplies this).",
    ),
    repo_root: str = typer.Option(".", help="Repository root to compare against."),
) -> None:
    """Report whether the running collector needs a restart to pick up the
    working tree's code. Read-only: never restarts anything (use
    scripts/service/check_restart.sh --restart for that). Exit codes:
    0 no restart needed, 10 restart required, 11 reinstall required,
    12 unknown (no manifest)."""
    report = restart_policy.compare(Path(repo_root).resolve(), Path(manifest))
    typer.echo(restart_policy.format_report(report))
    raise typer.Exit(report.exit_code)


@backup_app.command("finalize")
def backup_finalize(
    outcome: str = typer.Option(..., help="'success' or 'failed' -- the local dump+verify result."),
    backup_dir: str = typer.Option(
        ..., help="Directory the backup lands in (for the status file)."
    ),
    local_path: str | None = typer.Option(None, help="Path to the verified local .dump file."),
    entries: int | None = typer.Option(None, help="pg_restore --list entry count."),
    size_bytes: int | None = typer.Option(None, help="Size of the local .dump file."),
    duration_seconds: float | None = typer.Option(None, help="Wall-clock duration of the dump."),
    error: str | None = typer.Option(None, help="Error detail when outcome=failed."),
) -> None:
    """Called by scripts/backup_postgres.sh after its own pg_dump/pg_restore
    --list work (never before -- this command never touches Postgres
    itself). Writes the machine-readable status record
    observatory/backup_health.py reads, and -- only when outcome=success --
    attempts the configured off-machine copy with independent integrity
    verification. Local backup success is never contingent on this
    command's own exit code or on the remote copy succeeding; this always
    exits 0 (a finalize-step problem is surfaced via the status record and
    the observatory, not by failing the backup script's own exit code)."""
    settings = get_settings()
    remote_config = backup.RemoteConfig(
        remote_type=settings.backup_remote_type,
        filesystem_path=settings.backup_remote_path,
        s3_bucket=settings.backup_s3_bucket,
        s3_prefix=settings.backup_s3_prefix,
    )
    status = backup.finalize_backup(
        now=utc_now(),
        local_outcome=outcome,
        local_path=Path(local_path) if local_path else None,
        size_bytes=size_bytes,
        entries=entries,
        duration_seconds=duration_seconds,
        error=error,
        remote_config=remote_config,
    )
    status_path = Path(backup_dir) / backup.STATUS_FILENAME
    backup.write_backup_status(status_path, status)
    typer.echo(
        f"local={status.local_outcome} remote={status.remote_outcome} ({status.remote_detail})"
    )


@backup_app.command("prune")
def backup_prune(
    dry_run: bool = typer.Option(True, help="Log decisions without deleting anything."),
    target: str = typer.Option("local", help="'local' or 'remote' (filesystem-type only)."),
    daily_days: int | None = typer.Option(None, help="Override BACKUP_RETENTION_DAILY_DAYS."),
    weekly_weeks: int | None = typer.Option(None, help="Override BACKUP_RETENTION_WEEKLY_WEEKS."),
    monthly_months: int | None = typer.Option(
        None, help="Override BACKUP_RETENTION_MONTHLY_MONTHS."
    ),
) -> None:
    """Apply the grandfather-father-son retention policy (docs/runbooks/
    backup_recovery.md "Retention policy"). Defaults to --dry-run=True --
    pass --no-dry-run to actually delete anything. Never deletes the
    single newest backup; refuses to run at all if the target directory
    has zero files matching the naming convention (an empty/wrong
    directory is far more likely than "no backups yet" for a path this
    command was pointed at). Also cleans up abandoned `.partial` files
    (an interrupted dump) older than the grace period. Auto-pruning from
    the backup script itself is a separate, explicit opt-in
    (BACKUP_AUTO_PRUNE) -- this command existing does not mean pruning
    runs automatically."""
    settings = get_settings()
    if target == "local":
        directory = _backup_dir(settings)
    elif target == "remote":
        if settings.backup_remote_type != "filesystem":
            typer.echo(
                f"--target remote only supports BACKUP_REMOTE_TYPE=filesystem "
                f"(currently {settings.backup_remote_type!r})"
            )
            raise typer.Exit(code=1)
        if not settings.backup_remote_path:
            typer.echo("BACKUP_REMOTE_PATH is not set")
            raise typer.Exit(code=1)
        directory = Path(settings.backup_remote_path)
    else:
        typer.echo(f"unknown --target {target!r} (expected 'local' or 'remote')")
        raise typer.Exit(code=1)

    files = backup_retention.list_backup_files(directory)
    plan = backup_retention.compute_retention_plan(
        files,
        now=utc_now().replace(tzinfo=None),
        daily_days=daily_days if daily_days is not None else settings.backup_retention_daily_days,
        weekly_weeks=(
            weekly_weeks if weekly_weeks is not None else settings.backup_retention_weekly_weeks
        ),
        monthly_months=(
            monthly_months
            if monthly_months is not None
            else settings.backup_retention_monthly_months
        ),
    )
    if plan.aborted:
        typer.echo(f"ABORTED: {plan.abort_reason} ({directory})")
        raise typer.Exit(code=1)

    log = backup_retention.apply_retention_plan(plan, dry_run=dry_run)
    partials = backup_retention.find_stale_partial_files(
        directory, now=utc_now().replace(tzinfo=None)
    )
    log += backup_retention.apply_partial_cleanup(partials, dry_run=dry_run)

    prefix = "[DRY RUN] " if dry_run else ""
    for entry in log:
        verb = "would delete" if entry.dry_run and entry.action == "delete" else entry.action
        typer.echo(f"{prefix}{verb}: {entry.path} -- {entry.reason}")
    kept = sum(1 for e in log if e.action == "keep")
    deleted = sum(1 for e in log if e.action == "delete")
    typer.echo(f"{prefix}{kept} kept, {deleted} {'would be ' if dry_run else ''}deleted")


@backup_app.command("status")
def backup_status(
    as_json: bool = typer.Option(False, "--json", help="Emit the status record as JSON."),
) -> None:
    """Show the last recorded backup outcome and current local backup
    inventory -- "how to confirm the last successful local and remote
    backup" (docs/runbooks/backup_recovery.md)."""
    settings = get_settings()
    backup_dir = _backup_dir(settings)
    status = backup.read_backup_status(backup_dir / backup.STATUS_FILENAME)
    files = backup_retention.list_backup_files(backup_dir)
    newest = max(files, key=lambda f: f.timestamp) if files else None

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "status": status.to_dict() if status else None,
                    "local_backup_count": len(files),
                    "newest_local_backup": newest.path.name if newest else None,
                    "newest_local_timestamp": newest.timestamp.isoformat() if newest else None,
                },
                indent=2,
            )
        )
        return

    if status is None:
        typer.echo("no backup has ever run")
    else:
        typer.echo(f"last run:      {status.timestamp}")
        typer.echo(
            f"local outcome: {status.local_outcome}"
            + (f" ({status.error})" if status.error else "")
        )
        typer.echo(f"remote outcome: {status.remote_outcome} -- {status.remote_detail}")
    typer.echo(f"local backups on disk: {len(files)}")
    if newest:
        typer.echo(f"newest local backup:   {newest.path.name} ({newest.timestamp.isoformat()})")


if __name__ == "__main__":
    app()
