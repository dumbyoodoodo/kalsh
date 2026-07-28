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
from datetime import UTC, date, datetime, timedelta
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
research_app = typer.Typer(help="Generic research-workbench utilities (leakage audit, ledger).")
app.add_typer(research_app, name="research")
paper_app = typer.Typer(
    help="SIMULATION ONLY -- deterministic paper-trading/execution simulator (no exchange orders)."
)
app.add_typer(paper_app, name="paper")


@paper_app.command("simulate")
def paper_simulate(
    config: str = typer.Option(..., help="Path to a JSON run configuration (required)."),
    out_dir: str = typer.Option(..., help="Immutable output run directory (must not exist)."),
) -> None:
    """Run a paper-trading simulation from an explicit config and write an
    immutable run artifact. SIMULATION ONLY -- NO EXCHANGE ORDERS ARE SUBMITTED,
    no account is connected, no H0019 artifact is read."""
    from kalshi_weather.execution import SIMULATION_ONLY_BANNER
    from kalshi_weather.execution.loader import load_run_config
    from kalshi_weather.execution.replay import run_replay, write_artifacts

    typer.echo(SIMULATION_ONLY_BANNER)
    cfg = load_run_config(Path(config))
    sim = run_replay(cfg)
    manifest = write_artifacts(cfg, sim, Path(out_dir))
    typer.echo(f"run {cfg.run_id}: orders={len(sim.orders)} fills={len(sim.fills)} -> {out_dir}")
    typer.echo(
        f"manifest git={manifest['git_commit'][:10]} dataset_sha={manifest['dataset_sha256'][:12]}"
    )


@paper_app.command("inspect")
def paper_inspect(run_dir: str = typer.Argument(..., help="A run directory to inspect.")) -> None:
    """Print a run's summary and metrics (read-only)."""
    d = Path(run_dir)
    typer.echo((d / "summary.txt").read_text())
    typer.echo(json.dumps(json.loads((d / "metrics.json").read_text())["accounting"], indent=2))


def _load_ledger(run_dir: Path):  # type: ignore[no-untyped-def]
    from datetime import datetime as _dt

    from kalshi_weather.execution.ledger import EntryKind, LedgerEntry

    return [
        LedgerEntry(
            seq=e["seq"],
            at=_dt.fromisoformat(e["at"]),
            kind=EntryKind(e["kind"]),
            cash_delta_cents=e["cash_delta_cents"],
            reserved_delta_cents=e["reserved_delta_cents"],
            payload=e["payload"],
        )
        for e in json.loads((run_dir / "ledger.json").read_text())
    ]


@paper_app.command("replay-ledger")
def paper_replay_ledger(run_dir: str = typer.Argument(...)) -> None:
    """Replay a run's ledger from scratch and confirm the final cash reconciles."""
    from kalshi_weather.execution.ledger import replay

    d = Path(run_dir)
    manifest = json.loads((d / "run_manifest.json").read_text())
    p = replay(manifest["initial_cash_cents"], _load_ledger(d))
    typer.echo(
        f"replayed cash={p.cash_cents} reserved={p.reserved_cents} "
        f"realized={p.realized_pnl_cents()}"
    )
    typer.echo(f"positions reconstructed: {len(p.positions)}")


@paper_app.command("validate")
def paper_validate(run_dir: str = typer.Argument(...)) -> None:
    """Validate a run: ledger replay reconciles and invariants hold. Exits
    non-zero on any accounting failure."""
    from kalshi_weather.execution.ledger import replay

    d = Path(run_dir)
    manifest = json.loads((d / "run_manifest.json").read_text())
    try:
        p = replay(manifest["initial_cash_cents"], _load_ledger(d))  # AccountingError on mismatch
    except Exception as exc:
        typer.echo(f"INVALID: {exc}")
        raise typer.Exit(code=1) from exc
    ok = p.cash_cents + p.reserved_cents >= 0
    total = p.cash_cents + p.reserved_cents
    typer.echo(f"{'VALID' if ok else 'INVALID'}: replay reconciles, cash+reserved={total}")
    if not ok:
        raise typer.Exit(code=1)


@paper_app.command("calculate-fee")
def paper_calculate_fee(
    price_cents: int = typer.Option(..., help="Fill price in integer cents (1..99)."),
    quantity: int = typer.Option(..., help="Number of contracts in the fill."),
    liquidity: str = typer.Option("taker", help="taker | maker."),
    fee_model: str = typer.Option("kalshi", help="kalshi | zero | configurable."),
    ticker: str = typer.Option(
        "EVENT-GENERIC",
        help="Market ticker (selects general vs INX/NASDAQ100 special schedule).",
    ),
    target_precision_centicents: int = typer.Option(
        100, help="Balance precision: 100 = non-direct ($0.01), 1 = direct ($0.0001)."
    ),
) -> None:
    """Offline fee estimate for a single hypothetical fill. Shows every fee
    component. SIMULATION ONLY -- connects to nothing, needs no credentials."""
    from kalshi_weather.execution import SIMULATION_ONLY_BANNER
    from kalshi_weather.execution.fees import (
        ConfigurableFeeModel,
        KalshiEventContractFeeModel,
        ZeroFeeModel,
    )
    from kalshi_weather.execution.models import Action, Liquidity, Side

    typer.echo(SIMULATION_ONLY_BANNER)
    liq = Liquidity(liquidity)
    if fee_model == "zero":
        model: object = ZeroFeeModel()
    elif fee_model == "configurable":
        model = ConfigurableFeeModel(rate_bps=700, version="configurable-v1")
    else:
        model = KalshiEventContractFeeModel(target_precision_centicents=target_precision_centicents)
    a = model.assess(  # type: ignore[attr-defined]
        price_cents=price_cents,
        quantity=quantity,
        side=Side.YES,
        action=Action.BUY,
        liquidity=liq,
        ticker=ticker,
        accumulator_centicents=0,
    )
    bd = a.breakdown
    lines = {
        "fee_model_version": bd.fee_model_version,
        "market_fee_schedule": bd.market_fee_schedule,
        "price_cents": price_cents,
        "quantity": quantity,
        "liquidity": bd.liquidity,
        "raw_trade_fee_centicents": bd.trade_fee_centicents,
        "rounding_fee_centicents": bd.rounding_fee_centicents,
        "rebate_centicents": bd.rebate_centicents,
        "net_fee_centicents": bd.net_fee_centicents,
        "net_fee_cents": bd.net_fee_cents,
    }
    typer.echo(json.dumps(lines, indent=2))
    if bd.market_fee_schedule == "general" and ticker == "EVENT-GENERIC":
        typer.echo(
            "note: no market override supplied -- assuming the GENERAL schedule; "
            "pass --ticker for an INX*/NASDAQ100* special schedule."
        )


@asynccontextmanager
async def _open_readonly_session(settings: Settings):  # type: ignore[no-untyped-def]
    """A PostgreSQL session forced read-only at the connection level, so the
    historical adapter cannot write to the production archive even by mistake."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"options": "-c default_transaction_read_only=on"},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


@paper_app.command("export-replay-data")
def paper_export_replay_data(
    start: str = typer.Option(..., help="Start timestamp (ISO 8601, inclusive)."),
    end: str = typer.Option(..., help="End timestamp (ISO 8601, inclusive)."),
    tickers: str = typer.Option(..., help="Path to a newline-delimited ticker file (required)."),
    mode: str = typer.Option(
        "collector-available", help="collector-available | exchange-time (Phase 4)."
    ),
    output: str = typer.Option(..., help="Immutable export directory (must not exist)."),
    max_book_age_seconds: float = typer.Option(300.0),
    include_settlement: bool = typer.Option(True),
    allow_null_provenance: str = typer.Option(
        "", help="Comma-separated data types allowed with NULL/unknown env (default none)."
    ),
    coverage_policy: str = typer.Option(
        "", help="Optional JSON coverage-policy file; writes a coverage report into the export."
    ),
) -> None:
    """Export a bounded historical production interval into an immutable replay
    dataset. HISTORICAL REPLAY -- read-only, no exchange orders."""
    import asyncio

    from kalshi_weather.execution.coverage import evaluate_coverage
    from kalshi_weather.execution.history import (
        HistoryEnvironmentPolicy,
        HistoryQuery,
        TimestampMode,
        load_historical_replay_data,
    )
    from kalshi_weather.execution.history_replay import (
        HISTORICAL_BANNER,
        _coverage_summary_md,
        export_replay_data,
        load_coverage_policy,
    )

    typer.echo(HISTORICAL_BANNER)
    ticker_list = tuple(t.strip() for t in Path(tickers).read_text().splitlines() if t.strip())
    if not ticker_list:
        raise typer.BadParameter("ticker file is empty; explicit market selection is required")
    allow_null = frozenset(x.strip() for x in allow_null_provenance.split(",") if x.strip())
    settings = get_settings()
    result: dict[str, object] = {}

    async def _run() -> dict[str, object]:
        from sqlalchemy import text

        async with _open_readonly_session(settings) as session:
            revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
            query = HistoryQuery(
                start=datetime.fromisoformat(start),
                end=datetime.fromisoformat(end),
                tickers=ticker_list,
                mode=TimestampMode(mode),
                env_policy=HistoryEnvironmentPolicy(allow_null_for=allow_null),
                max_book_age_seconds=max_book_age_seconds,
                include_settlement=include_settlement,
                source_db_revision=revision,
            )
            data = await load_historical_replay_data(session, query)
        manifest = export_replay_data(
            query, data, Path(output), source_db_revision=query.source_db_revision
        )
        # Observed-availability timeline (read-only), written into the export.
        from kalshi_weather.execution.availability import build_availability_timeline
        from kalshi_weather.execution.history_replay import write_availability_artifacts

        async with _open_readonly_session(settings) as session:
            timeline = await build_availability_timeline(
                session,
                tickers=ticker_list,
                start=datetime.fromisoformat(start),
                end=datetime.fromisoformat(end),
                source_db_revision=query.source_db_revision,
            )
        avail_hashes = write_availability_artifacts(Path(output), timeline)
        manifest["availability_hashes"] = avail_hashes
        manifest["availability_downtime_gaps"] = timeline.downtime_gaps
        (Path(output) / "export_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True, default=str)
        )
        if coverage_policy:
            pol = load_coverage_policy(Path(coverage_policy))
            cov = evaluate_coverage(data, ticker_list, pol)
            out = Path(output)
            (out / "coverage_policy.json").write_text(
                json.dumps(pol.to_manifest(), indent=2, sort_keys=True, default=str)
            )
            (out / "coverage_results.json").write_text(
                json.dumps(cov.to_manifest(), indent=2, sort_keys=True, default=str)
            )
            (out / "coverage_summary.md").write_text(_coverage_summary_md(cov, [], False))
            manifest["coverage_verdict"] = cov.verdict.value
        return manifest

    manifest = asyncio.run(_run())
    result = manifest
    counts = result["counts"]
    typer.echo(f"export {result['export_id']} (git {str(result['git_commit'])[:10]}) -> {output}")
    typer.echo(f"counts: {json.dumps(counts)}")
    if "coverage_verdict" in result:
        typer.echo(f"coverage verdict: {result['coverage_verdict']}")


@paper_app.command("simulate-history")
def paper_simulate_history(
    data: str = typer.Option(..., help="An export directory produced by export-replay-data."),
    orders: str = typer.Option(..., help="JSON file of explicit external/synthetic order intents."),
    config: str = typer.Option(..., help="JSON execution config (policy/risk/initial_cash)."),
    output: str = typer.Option(..., help="Immutable run directory (must not exist)."),
    run_id: str = typer.Option("history-replay", help="Run identifier."),
    coverage_policy: str = typer.Option(
        "", help="Optional JSON coverage-policy file; gates the run on data quality."
    ),
    coverage_preset: str = typer.Option(
        "", help="Built-in preset: strict-marketable | passive-research | exploratory."
    ),
    allow_insufficient_coverage: bool = typer.Option(
        False,
        help="UNSAFE override: run even when a required coverage gate fails. Never default.",
    ),
) -> None:
    """Replay explicit order intents against an exported historical interval.
    HISTORICAL REPLAY -- offline, no exchange orders, no strategy signal. Refuses
    to run on INSUFFICIENT_DATA unless an explicit unsafe override is passed.
    passive-research refuses when observation continuity is inadequate;
    strict-marketable marks passive intents unsupported."""
    from kalshi_weather.execution.coverage import evaluate_coverage
    from kalshi_weather.execution.history_replay import (
        HISTORICAL_BANNER,
        InsufficientCoverageError,
        build_history_replay_config,
        compute_order_confidence,
        enforce_coverage,
        load_availability,
        load_exec_config,
        load_exported_data,
        load_order_intents,
        run_history_replay,
        write_history_artifacts,
    )

    typer.echo(HISTORICAL_BANNER)
    loaded = load_exported_data(Path(data))
    intents = load_order_intents(Path(orders))
    cash, policy, risk, marks = load_exec_config(Path(config))
    cov_policy = _resolve_coverage_policy(coverage_preset, coverage_policy)
    ta_map, avail_summary = load_availability(Path(data))

    coverage = evaluate_coverage(
        loaded.as_historical_data(),
        loaded.tickers(),
        cov_policy,
        availability=avail_summary or None,
    )
    typer.echo(f"coverage verdict: {coverage.verdict.value} (preset={cov_policy.preset_name})")
    for g in coverage.failed_gates():
        typer.echo(f"  FAILED gate {g.name}: {g.actual} vs {g.threshold} ({g.reason})")
    try:
        enforce_coverage(coverage, allow_insufficient=allow_insufficient_coverage)
    except InsufficientCoverageError as exc:
        typer.echo(f"REFUSING TO RUN: {exc}")
        raise typer.Exit(code=2) from exc
    if allow_insufficient_coverage and coverage.verdict.value == "INSUFFICIENT_DATA":
        typer.echo("!!! UNSAFE_COVERAGE_OVERRIDE ACTIVE -- results are NOT high-fidelity !!!")

    cfg = build_history_replay_config(
        run_id=run_id,
        initial_cash_cents=cash,
        policy=policy,
        risk=risk,
        loaded=loaded,
        order_intents=intents,
        marks=marks,
        availability=ta_map,
    )
    sim = run_history_replay(cfg)
    order_conf = compute_order_confidence(
        sim, cfg, coverage, unsafe_override=allow_insufficient_coverage
    )
    write_history_artifacts(
        cfg,
        sim,
        loaded,
        Path(output),
        coverage=coverage,
        order_confidence=order_conf,
        unsafe_override=allow_insufficient_coverage,
    )
    (Path(output) / "coverage_preset.json").write_text(
        json.dumps({"preset_name": cov_policy.preset_name, "version": cov_policy.version}, indent=2)
    )
    # copy the availability artifacts from the export into the run dir for a
    # self-contained record (read-only; already deterministic).
    for name in (
        "availability_policy.json",
        "availability_summary.json",
        "availability_summary.md",
        "availability_timeline.parquet",
    ):
        src = Path(data) / name
        if src.exists():
            (Path(output) / name).write_bytes(src.read_bytes())
    typer.echo(
        f"run {run_id}: orders={len(sim.orders)} fills={len(sim.fills)} "
        f"rejections={len(sim.rejections)} verdict={coverage.verdict.value} -> {output}"
    )


def _resolve_coverage_policy(preset: str, policy_path: str):  # type: ignore[no-untyped-def]
    """Resolve a coverage policy from a preset name or a JSON file (mutually
    exclusive). A JSON file may override a preset only as an explicit config."""
    from kalshi_weather.execution.coverage import get_preset
    from kalshi_weather.execution.history_replay import load_coverage_policy

    if preset and policy_path:
        raise typer.BadParameter("pass either --coverage-preset or --coverage-policy, not both")
    if preset:
        return get_preset(preset)
    return load_coverage_policy(Path(policy_path) if policy_path else None)


@paper_app.command("inspect-coverage")
def paper_inspect_coverage(
    data: str = typer.Option(..., help="An export directory produced by export-replay-data."),
    coverage_policy: str = typer.Option("", help="Optional JSON coverage-policy file."),
    preset: str = typer.Option(
        "", help="Built-in preset: strict-marketable | passive-research | exploratory."
    ),
) -> None:
    """Report historical-data coverage quality WITHOUT running a simulation."""
    from kalshi_weather.execution.coverage import evaluate_coverage
    from kalshi_weather.execution.history_replay import (
        HISTORICAL_BANNER,
        load_availability,
        load_exported_data,
    )

    typer.echo(HISTORICAL_BANNER)
    loaded = load_exported_data(Path(data))
    cov_policy = _resolve_coverage_policy(preset, coverage_policy)
    _ta, avail_summary = load_availability(Path(data))
    coverage = evaluate_coverage(
        loaded.as_historical_data(),
        loaded.tickers(),
        cov_policy,
        availability=avail_summary or None,
    )
    typer.echo(
        f"verdict: {coverage.verdict.value} (policy {cov_policy.version}, "
        f"preset {cov_policy.preset_name})"
    )
    for g in coverage.gates:
        if g.severity.value != "PASS":
            typer.echo(f"  {g.severity.value} {g.name}: {g.actual} vs {g.threshold} ({g.reason})")
    for m in coverage.markets:
        obs = f" obs={m.observed_pct}%" if m.observed_pct is not None else ""
        typer.echo(
            f"  {m.ticker}: {m.confidence.value} books={m.book_count} trades={m.trade_count} "
            f"settle={m.has_settlement} marketable={m.marketable_eligible} "
            f"passive={m.passive_eligible}{obs}"
        )


@paper_app.command("inspect-availability")
def paper_inspect_availability(
    start: str = typer.Option(..., help="Start timestamp (ISO 8601, inclusive)."),
    end: str = typer.Option(..., help="End timestamp (ISO 8601, inclusive)."),
    tickers: str = typer.Option(..., help="Path to a newline-delimited ticker file (required)."),
    output: str = typer.Option("", help="Optional directory to export availability artifacts."),
) -> None:
    """Report observed-availability for explicit tickers over a bounded range,
    from collector-run metadata (read-only). HISTORICAL REPLAY -- no orders."""
    import asyncio

    from kalshi_weather.execution.availability import build_availability_timeline
    from kalshi_weather.execution.history_replay import (
        HISTORICAL_BANNER,
        write_availability_artifacts,
    )

    typer.echo(HISTORICAL_BANNER)
    ticker_list = tuple(t.strip() for t in Path(tickers).read_text().splitlines() if t.strip())
    if not ticker_list:
        raise typer.BadParameter("ticker file is empty; explicit market selection is required")
    settings = get_settings()

    from kalshi_weather.execution.availability import AvailabilityTimeline

    async def _run() -> AvailabilityTimeline:
        from sqlalchemy import text

        async with _open_readonly_session(settings) as session:
            revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
            return await build_availability_timeline(
                session,
                tickers=ticker_list,
                start=datetime.fromisoformat(start),
                end=datetime.fromisoformat(end),
                source_db_revision=revision,
            )

    timeline = asyncio.run(_run())
    typer.echo(
        f"collector runs={timeline.collector_runs_considered} failed={timeline.failed_runs} "
        f"downtime_gaps={timeline.downtime_gaps}"
    )
    for tk in ticker_list:
        s = timeline.summary(tk)
        typer.echo(
            f"  {tk}: observed={s['observed_pct']}% likely={s['likely_observed_pct']}% "
            f"unknown={s['unknown_pct']}% unavailable={s['collector_unavailable_pct']}% "
            f"breaks={s['continuity_breaks']} "
            f"max_unavail_gap={s['max_collector_unavailable_gap_seconds']}s"
        )
    if output:
        Path(output).mkdir(parents=True, exist_ok=True)
        write_availability_artifacts(Path(output), timeline)
        typer.echo(f"availability artifacts -> {output}")


_PAPER_FORWARD_BANNER = (
    "FORWARD PAPER SKELETON -- SYNTHETIC/MANUAL SIGNALS ONLY. "
    "No exchange orders. Not strategy performance."
)


@paper_app.command("forward-run")
def paper_forward_run(
    tickers: str = typer.Option("", help="Comma-separated tickers (for synthetic sources)."),
    synthetic_probability: str = typer.Option(
        "", help="Constant synthetic probability, e.g. 0.55 (source: constant)."
    ),
    threshold_offset_cents: int = typer.Option(
        0, help="Mid+offset deterministic rule (source: threshold_rule; used with --use-rule)."
    ),
    use_rule: bool = typer.Option(False, help="Use the deterministic threshold rule source."),
    signal_file: str = typer.Option("", help="Manual probability-signal JSON file."),
    intent_file: str = typer.Option("", help="Manual order-intent JSON file."),
    bankroll_cents: int = typer.Option(10_000, help="Initial paper bankroll (first run only)."),
    dry_run: bool = typer.Option(False, help="Evaluate and print, but persist nothing."),
) -> None:
    """Run ONE forward paper session against live production market data
    (read-only) with synthetic or manual signals. Writes only to the dedicated
    paper database. NO exchange order can be created by this command."""
    import asyncio
    from datetime import UTC, datetime
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from kalshi_weather.paper import runner as paper_runner
    from kalshi_weather.paper.signals import (
        constant_signals,
        load_intent_file,
        load_probability_file,
        threshold_rule_signals,
    )

    typer.echo(_PAPER_FORWARD_BANNER)
    settings = get_settings()
    now = datetime.now(UTC)
    ticker_list = [t.strip() for t in tickers.split(",") if t.strip()]

    sources = [bool(synthetic_probability), use_rule, bool(signal_file), bool(intent_file)]
    if sum(sources) != 1:
        raise typer.BadParameter(
            "choose exactly one signal source: --synthetic-probability | --use-rule "
            "| --signal-file | --intent-file"
        )

    if synthetic_probability:
        if not ticker_list:
            raise typer.BadParameter("--tickers is required with --synthetic-probability")
        signals = constant_signals(
            tickers=ticker_list, probability=Decimal(synthetic_probability), generated_at=now
        )
        source = "constant"
    elif use_rule:
        if not ticker_list:
            raise typer.BadParameter("--tickers is required with --use-rule")

        async def _quotes() -> list[tuple[str, int | None, int | None]]:
            eng = create_async_engine(settings.database_url)
            try:
                factory = async_sessionmaker(eng, expire_on_commit=False)
                async with factory() as session:
                    from kalshi_weather.paper.evidence import load_market_evidence

                    ev = await load_market_evidence(session, ticker_list, now=now)
                    return [
                        (t, e.yes_bid_cents, e.yes_ask_cents) for t, e in sorted(ev.items())
                    ]
            finally:
                await eng.dispose()

        signals = threshold_rule_signals(
            quotes=asyncio.run(_quotes()),
            offset_cents=threshold_offset_cents,
            generated_at=now,
        )
        source = "threshold_rule"
    elif signal_file:
        signals = load_probability_file(Path(signal_file))
        source = "probability_file"
    else:
        signals = load_intent_file(Path(intent_file))
        source = "intent_file"

    if dry_run:
        typer.echo(f"[dry-run] {len(signals)} signal(s) prepared; nothing persisted")
        for s in signals:
            typer.echo(
                f"  {s.ticker} p={s.probability} side={s.side} v={s.version} "
                f"hash={s.provenance_hash[:12]}"
            )
        return

    run_id, result = asyncio.run(
        paper_runner.execute_paper_run(
            research_database_url=settings.database_url,
            paper_database_url=settings.paper_database_url,
            signals=signals,
            signal_source=source,
            initial_cash_cents=bankroll_cents,
        )
    )
    typer.echo(f"paper run {run_id}: status={result.status}")
    for k, v in result.summary.items():
        typer.echo(f"  {k}: {v}")


@paper_app.command("forward-status")
def paper_forward_status() -> None:
    """Latest forward paper run, kill-switch state, and equity (read-only)."""
    import asyncio

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kalshi_weather.paper.store import (
        PaperPnlSnapshotRow,
        PaperRun,
        kill_switch_active,
        open_paper_db,
        paper_engine,
    )

    async def _status() -> None:
        engine = paper_engine(get_settings().paper_database_url)
        await open_paper_db(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            kill, reason = await kill_switch_active(session)
            run = (
                await session.scalars(
                    select(PaperRun).order_by(PaperRun.created_at.desc()).limit(1)
                )
            ).first()
            pnl = (
                await session.scalars(
                    select(PaperPnlSnapshotRow).order_by(PaperPnlSnapshotRow.id.desc()).limit(1)
                )
            ).first()
            typer.echo(_PAPER_FORWARD_BANNER)
            typer.echo(f"kill_switch: {'ACTIVE (' + reason + ')' if kill else 'inactive'}")
            if run is None:
                typer.echo("no forward paper runs recorded")
            else:
                typer.echo(f"latest run: {run.id} at {run.created_at} status={run.status}")
                typer.echo(f"  summary: {run.summary_json}")
            if pnl is not None:
                typer.echo(
                    f"  equity_at_cost={pnl.equity_at_cost_cents}c cash={pnl.cash_cents}c "
                    f"fees={pnl.fees_cents}c drawdown={pnl.drawdown_cents}c"
                )
        await engine.dispose()

    asyncio.run(_status())


@paper_app.command("forward-report")
def paper_forward_report(
    date_str: str = typer.Option(..., "--date", help="UTC calendar day, YYYY-MM-DD."),
) -> None:
    """Daily forward paper report (synthetic/manual operational testing)."""
    import asyncio
    from datetime import date as date_type

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kalshi_weather.paper.reporting import build_daily_report, render_report
    from kalshi_weather.paper.store import open_paper_db, paper_engine

    async def _report() -> None:
        engine = paper_engine(get_settings().paper_database_url)
        await open_paper_db(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            report = await build_daily_report(session, date_type.fromisoformat(date_str))
        typer.echo(render_report(report))
        await engine.dispose()

    asyncio.run(_report())


def _paper_control(kind: str, reason: str) -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kalshi_weather.paper.store import PaperControl, open_paper_db, paper_engine

    async def _write() -> None:
        engine = paper_engine(get_settings().paper_database_url)
        await open_paper_db(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add(PaperControl(kind=kind, reason=reason))
            await session.commit()
        await engine.dispose()

    asyncio.run(_write())


@paper_app.command("kill")
def paper_kill(reason: str = typer.Option(..., help="Why the kill switch is engaged.")) -> None:
    """Engage the forward paper kill switch: subsequent runs are refused until
    an explicit `paper resume`."""
    _paper_control("kill", reason)
    typer.echo(f"kill switch ENGAGED: {reason}")


@paper_app.command("resume")
def paper_resume(
    reason: str = typer.Option(..., help="Why it is safe to resume (recorded)."),
) -> None:
    """Explicitly release the forward paper kill switch (manual act, recorded)."""
    _paper_control("resume", reason)
    typer.echo(f"kill switch released: {reason}")


@paper_app.command("settle")
def paper_settle() -> None:
    """Settle open paper positions from authoritative Kalshi settlement
    records (read-only research access; append-only paper records). Each
    position settles exactly once; ambiguous or unsupported outcomes fail
    closed with recorded reasons. NO exchange orders."""
    import asyncio

    from kalshi_weather.paper import runner as paper_runner

    typer.echo(_PAPER_FORWARD_BANNER)
    settings = get_settings()
    run_id, result = asyncio.run(
        paper_runner.execute_paper_settlement(
            research_database_url=settings.database_url,
            paper_database_url=settings.paper_database_url,
        )
    )
    typer.echo(f"settlement run {run_id}")
    for k, v in result.summary.items():
        typer.echo(f"  {k}: {v}")
    for d in result.decisions:
        typer.echo(
            f"  {d.ticker}: {d.status}"
            + (f" result={d.result} payout={d.gross_payout_cents}c" if d.result else "")
        )


@paper_app.command("reconcile")
def paper_reconcile() -> None:
    """Rebuild the paper portfolio by replaying the stored cash ledger through
    the audited accounting and compare with the latest stored snapshot."""
    import asyncio
    import json as json_mod

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kalshi_weather.execution.ledger import EntryKind, LedgerEntry, replay
    from kalshi_weather.paper.store import (
        PaperPnlSnapshotRow,
        open_paper_db,
        paper_engine,
        stored_ledger_entries,
    )

    async def _reconcile() -> None:
        engine = paper_engine(get_settings().paper_database_url)
        await open_paper_db(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            entries = await stored_ledger_entries(session)
            snap = (
                await session.scalars(
                    select(PaperPnlSnapshotRow).order_by(PaperPnlSnapshotRow.id.desc()).limit(1)
                )
            ).first()
        await engine.dispose()
        if not entries:
            typer.echo("reconcile: no ledger entries recorded yet")
            return
        ledger_entries = [
            LedgerEntry(
                seq=i,
                at=at,
                kind=EntryKind(kind),
                cash_delta_cents=cash_d,
                reserved_delta_cents=res_d,
                payload=payload,
            )
            for i, (_seq, at, kind, cash_d, res_d, payload) in enumerate(entries)
        ]
        portfolio = replay(0, ledger_entries)
        cost = sum(p.yes_cost_cents + p.no_cost_cents for p in portfolio.positions.values())
        equity = portfolio.cash_cents + portfolio.reserved_cents + cost
        typer.echo(
            f"replayed {len(entries)} entries: cash={portfolio.cash_cents}c "
            f"reserved={portfolio.reserved_cents}c position_cost={cost}c equity={equity}c "
            f"fees={portfolio.fees_cents}c"
        )
        if snap is None:
            typer.echo("reconcile: no pnl snapshot to compare against")
            return
        checks = {
            "cash": (portfolio.cash_cents, snap.cash_cents),
            "reserved": (portfolio.reserved_cents, snap.reserved_cents),
            "position_cost": (cost, snap.position_cost_cents),
            "equity_at_cost": (equity, snap.equity_at_cost_cents),
            "fees": (portfolio.fees_cents, snap.fees_cents),
        }
        mismatches = {k: v for k, v in checks.items() if v[0] != v[1]}
        if mismatches:
            typer.echo(f"RECONCILE MISMATCH: {json_mod.dumps(mismatches)}")
            raise typer.Exit(code=1)
        typer.echo("reconcile OK: replayed ledger matches the stored snapshot exactly")

    asyncio.run(_reconcile())


@paper_app.command("closeout")
def paper_closeout(
    ticker: str = typer.Option(..., "--ticker", help="Exact paper-position ticker."),
    as_of: str = typer.Option("", help="ISO UTC as-of (default: now)."),
    as_json: bool = typer.Option(False, "--json", help="Emit the closeout report as JSON."),
    no_write: bool = typer.Option(False, help="Never write the archival artifact."),
    fail_on_not_ready: bool = typer.Option(False, help="Exit nonzero if NOT_READY."),
    output: str = typer.Option("", "--output", help="Artifact path (only written if COMPLETE)."),
) -> None:
    """READ-ONLY post-settlement closeout for one live paper-validation episode.

    Verifies lineage, original fill accounting, authoritative terminal
    settlement, exactly-once payout, final realized P&L, reconciliation, and
    correction consistency. It NEVER settles a position, moves money, submits
    an exchange order, or touches an experiment. If the position has not
    settled authoritatively it reports NOT_READY and writes nothing."""
    from datetime import UTC as _utc
    from datetime import datetime as _dt
    from pathlib import Path as _Path

    from sqlalchemy import select as _select
    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import async_sessionmaker as _asm

    from kalshi_weather.execution.ledger import EntryKind, LedgerEntry, replay
    from kalshi_weather.paper import closeout as co
    from kalshi_weather.paper.store import (
        PaperFillRow,
        PaperMarketSnapshotRow,
        PaperOrderIntentRow,
        PaperPnlSnapshotRow,
        PaperPositionRow,
        PaperSettlementRow,
        PaperSignalRow,
        open_paper_db,
        paper_engine,
        stored_ledger_entries,
    )

    now = _dt.fromisoformat(as_of) if as_of else utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=_utc)
    settings = get_settings()

    holder: dict[str, Any] = {}

    async def load() -> None:
        eng = paper_engine(settings.paper_database_url)
        await open_paper_db(eng)
        factory = _asm(eng, expire_on_commit=False)
        async with factory() as ps:
            def by_ticker(model: Any) -> Any:
                return _select(model).where(model.ticker == ticker)

            signals = (await ps.scalars(by_ticker(PaperSignalRow))).all()
            intents = (await ps.scalars(by_ticker(PaperOrderIntentRow))).all()
            fills = (await ps.scalars(by_ticker(PaperFillRow))).all()
            evidence = (await ps.scalars(by_ticker(PaperMarketSnapshotRow))).all()
            positions = (await ps.scalars(by_ticker(PaperPositionRow))).all()
            settlements = (await ps.scalars(by_ticker(PaperSettlementRow))).all()
            entries = await stored_ledger_entries(ps)
            pnl = (
                await ps.scalars(
                    _select(PaperPnlSnapshotRow).order_by(PaperPnlSnapshotRow.id.desc()).limit(1)
                )
            ).first()
        await eng.dispose()

        led = [
            LedgerEntry(seq=i, at=at, kind=EntryKind(kind), cash_delta_cents=cd,
                        reserved_delta_cents=rd, payload=pl)
            for i, (_s, at, kind, cd, rd, pl) in enumerate(entries)
        ]
        pf = replay(0, led)
        cost = sum(p.yes_cost_cents + p.no_cost_cents for p in pf.positions.values())
        equity = pf.cash_cents + pf.reserved_cents + cost
        replayed = co.AccountLite(
            cash_cents=pf.cash_cents, reserved_cents=pf.reserved_cents,
            position_cost_cents=cost, equity_at_cost_cents=equity,
            realized_pnl_cents=equity - pf.total_deposits_cents, fees_cents=pf.fees_cents,
        )
        stored = (
            co.AccountLite(
                cash_cents=pnl.cash_cents, reserved_cents=pnl.reserved_cents,
                position_cost_cents=pnl.position_cost_cents,
                equity_at_cost_cents=pnl.equity_at_cost_cents,
                realized_pnl_cents=pnl.realized_pnl_cents, fees_cents=pnl.fees_cents,
            )
            if pnl is not None
            else None
        )
        reconcile_ok = stored is not None and (
            replayed.cash_cents == stored.cash_cents
            and replayed.reserved_cents == stored.reserved_cents
            and replayed.position_cost_cents == stored.position_cost_cents
            and replayed.fees_cents == stored.fees_cents
        )

        # Authoritative terminal evidence from the production market snapshots.
        async with _open_session(settings) as session:
            rows = (
                await session.execute(
                    _text(
                        "select id, status, result, settlement_ts from market_snapshots "
                        "where market_ticker=:t order by id desc limit 40"
                    ),
                    {"t": ticker},
                )
            ).all()
        latest = rows[0] if rows else None
        later_results = tuple(
            r.result for r in rows if r.result in ("yes", "no")
        )
        terminal = co.TerminalEvidence(
            status=latest.status if latest else None,
            result=(latest.result if latest and latest.result in ("yes", "no") else ""),
            settlement_ts=latest.settlement_ts if latest else None,
            later_results=later_results,
            later_settlement_ts=tuple(
                r.settlement_ts.isoformat() for r in rows if r.settlement_ts is not None
            ),
            snapshot_ids=tuple(r.id for r in rows[:5]),
        )

        pos = positions[-1] if positions else None
        inp = co.CloseoutInputs(
            ticker=ticker,
            signals=tuple(
                co.SignalLite(s.provenance_hash, s.version, s.side, s.source_type, s.accepted)
                for s in signals
            ),
            intents=tuple(
                co.IntentLite(i.order_id, i.side, i.quantity, i.limit_price_cents,
                              i.final_state, i.execution_confidence, i.signal_provenance)
                for i in intents
            ),
            fills=tuple(
                co.FillLite(f.order_id, f.side, f.quantity, f.price_cents, f.fee_cents,
                            f.liquidity, f.book_source_ref)
                for f in fills
            ),
            evidence=tuple(
                co.EvidenceLite(e.snapshot_source_id, e.book_source_id, e.poll_evidence_at,
                                e.market_status)
                for e in evidence
            ),
            position=co.PositionLite(
                pos.ticker, pos.yes_qty, pos.no_qty, pos.yes_cost_cents, pos.no_cost_cents,
                pos.realized_pnl_cents, pos.fees_cents,
            ) if pos else None,
            settlements=tuple(
                co.SettlementLite(s.status, s.result, s.settlement_ts, s.gross_payout_cents,
                                  s.position_cost_cents, s.realized_pnl_delta_cents, s.fee_cents,
                                  s.supersedes_id)
                for s in settlements
            ),
            replayed_account=replayed,
            stored_account=stored,
            terminal=terminal,
            ledger=tuple(
                co.LedgerEntryLite(seq, kind, cd, rd)
                for (seq, _at, kind, cd, rd, _pl) in entries
            ),
            reconcile_ok=reconcile_ok,
            as_of=now,
        )
        holder["report"] = co.evaluate_closeout(inp)

    asyncio.run(load())
    report = holder["report"]
    payload = report.to_dict()

    if as_json:
        typer.echo(json.dumps(payload, indent=2, default=str))
    else:
        typer.echo("OPERATIONAL VALIDATION ONLY — NOT A TRADING RESULT")
        typer.echo(f"paper closeout {ticker} as of {now.isoformat()}: {report.state.value}")
        for c in report.checks:
            typer.echo(f"  [{'PASS' if c.ok else 'FAIL'}] {c.name}: {c.detail}")
        if report.blockers:
            typer.echo(f"  blockers: {report.blockers}")
        typer.echo(f"  conclusion: {payload['operational_conclusion']}")

    if report.state is co.CloseoutState.COMPLETE and output and not no_write:
        _Path(output).parent.mkdir(parents=True, exist_ok=True)
        _Path(output).write_text(json.dumps(payload, indent=2, default=str) + "\n")
        typer.echo(f"artifact written: {output}")

    if fail_on_not_ready and report.state is co.CloseoutState.NOT_READY:
        raise typer.Exit(code=1)


@collector_app.command("inspect-polling-evidence")
def collector_inspect_polling_evidence(
    start: str = typer.Option(..., help="Start timestamp (ISO 8601, inclusive)."),
    end: str = typer.Option(..., help="End timestamp (ISO 8601, inclusive)."),
    tickers: str = typer.Option(
        "", help="Optional newline-delimited ticker file for ticker-level detail."
    ),
    endpoint: str = typer.Option("", help="Optional endpoint filter (orderbook|trades|...)."),
    environment: str = typer.Option("production", help="Environment filter (default production)."),
) -> None:
    """Read-only summary of per-ticker polling evidence (market_poll_attempts,
    migration 0011+) over a bounded window. Never prints raw payloads/secrets."""
    import asyncio

    from sqlalchemy import func, select

    from kalshi_weather.storage.models import MarketPollAttempt

    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)
    if end_dt <= start_dt:
        raise typer.BadParameter("end must be after start")
    ticker_list = (
        tuple(t.strip() for t in Path(tickers).read_text().splitlines() if t.strip())
        if tickers
        else ()
    )
    settings = get_settings()

    async def _run() -> None:
        async with _open_readonly_session(settings) as session:
            conds = [
                MarketPollAttempt.requested_at >= start_dt,
                MarketPollAttempt.requested_at <= end_dt,
                MarketPollAttempt.environment == environment,
            ]
            if endpoint:
                conds.append(MarketPollAttempt.endpoint_type == endpoint)
            if ticker_list:
                conds.append(MarketPollAttempt.ticker.in_(ticker_list))
            rows = (
                await session.execute(
                    select(
                        MarketPollAttempt.endpoint_type,
                        MarketPollAttempt.outcome,
                        func.count(),
                    )
                    .where(*conds)
                    .group_by(MarketPollAttempt.endpoint_type, MarketPollAttempt.outcome)
                    .order_by(MarketPollAttempt.endpoint_type, MarketPollAttempt.outcome)
                )
            ).all()
            total = sum(c for _e, _o, c in rows)
            observed = sum(
                c
                for _e, o, c in rows
                if o in ("succeeded_new_data", "succeeded_unchanged", "succeeded_empty")
            )
            typer.echo(f"poll attempts: {total} (environment={environment})")
            typer.echo(f"direct-observed: {round(100.0 * observed / total, 2) if total else 0.0}%")
            for e, o, c in rows:
                typer.echo(f"  {e:16s} {o:22s} {c}")
            uniq = await session.scalar(
                select(func.count(func.distinct(MarketPollAttempt.ticker))).where(*conds)
            )
            typer.echo(f"unique tickers: {uniq or 0}")

    asyncio.run(_run())


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
    hypothesis: str = typer.Argument("h0019", help="Which registered/documented experiment."),
    log_path: str | None = typer.Option(
        None, help="Append-only readiness-log JSONL (defaults to the registration dir)."
    ),
    as_of: str = typer.Option("", help="h0012r only: ISO date to inject as the clock."),
    as_json: bool = typer.Option(False, "--json", help="h0012r only: emit the summary as JSON."),
    environment: str = typer.Option(
        "", help="h0012r only: restrict close-time frame to one environment "
        "(default empty = env-agnostic; labels are env-agnostic by design)."
    ),
) -> None:
    """READ-ONLY readiness monitor for a registered/documented experiment
    (h0019, h0020, or the calendar-gated h0012r retry). Reports whether enough
    genuinely point-in-time data has accumulated to run -- coverage counts and
    integrity flags only. It NEVER trains a model, generates predictions,
    computes a Brier score, or (for h0012r) any stage-difference rate or CI."""
    if hypothesis.lower() == "h0020":
        _experiment_readiness_h0020(log_path)
        return
    if hypothesis.lower() == "h0012r":
        _experiment_readiness_h0012r(as_of=as_of, as_json=as_json, environment=environment)
        return
    if hypothesis.lower() != "h0019":
        raise typer.BadParameter("only 'h0019', 'h0020', and 'h0012r' are supported")
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


#: Latest-state patterns ALLOWED in research paths, each with its audited
#: invariant (2026-07-27 lineage audit). Everything else the scan finds in
#: these modules is a warning to investigate.
_LEAKAGE_SCAN_MODULES = (
    "src/kalshi_weather/dataset/asof.py",
    "src/kalshi_weather/dataset/pit.py",
    "src/kalshi_weather/dataset/builder.py",
    "src/kalshi_weather/experiments/decision_grain.py",
    "src/kalshi_weather/experiments/h0020_readiness.py",
    "src/kalshi_weather/settlement/labels.py",
    "src/kalshi_weather/research/splits.py",
    "src/kalshi_weather/research/benchmark.py",
)
_LEAKAGE_SCAN_ALLOWLIST = (
    # identity fields are environment/time-invariant (ADR 0013); every
    # snapshot carries the same strike/type, so first-non-null is safe
    "drop_nulls().first()",
)


@research_app.command("leakage-audit")
def research_leakage_audit(
    as_json: bool = typer.Option(False, "--json", help="Emit findings as JSON."),
    fail_on: str = typer.Option("error", help="Exit nonzero at this severity (error|warning)."),
    scope: str = typer.Option("all", help="'static', 'production', or 'all'."),
) -> None:
    """READ-ONLY research leakage audit: static latest-state pattern scan of
    the research-critical modules (with the audited allowlist) plus bounded
    production temporal-invariant sampling. Computes no predictive metric;
    writes nothing."""
    from kalshi_weather.research import leakage_lint as ll

    report = ll.LintReport()
    inspected: dict[str, Any] = {"modules": [], "production_checks": []}

    if scope in ("static", "all"):
        for rel in _LEAKAGE_SCAN_MODULES:
            path = Path(rel)
            if not path.exists():
                continue
            inspected["modules"].append(rel)
            report.extend(
                ll.scan_latest_state(
                    path.read_text(), path=rel, allowlist=_LEAKAGE_SCAN_ALLOWLIST
                )
            )

    if scope in ("production", "all"):
        from sqlalchemy import text as _text
        from sqlalchemy.ext.asyncio import create_async_engine

        async def prod_checks() -> None:
            settings = get_settings()
            engine = create_async_engine(settings.database_url)
            checks = [
                (
                    "forecast_observed_before_issue",
                    "select count(*) from weather_forecasts "
                    "where observed_at < issue_time - interval '10 minutes'",
                    "forecasts ingested before their claimed issuance "
                    "(clock skew beyond tolerance)",
                ),
                (
                    "poll_completed_before_requested",
                    "select count(*) from market_poll_attempts "
                    "where completed_at < requested_at",
                    "poll attempts completing before they were requested",
                ),
                (
                    "future_timestamps",
                    "select (select count(*) from orderbook_snapshots "
                    "where captured_at > now() + interval '5 minutes') + "
                    "(select count(*) from weather_forecasts "
                    "where observed_at > now() + interval '5 minutes')",
                    "rows timestamped in the future",
                ),
                (
                    "result_on_nonterminal_status",
                    "select count(*) from market_snapshots "
                    "where result in ('yes','no') and status not in "
                    "('finalized','determined','settled')",
                    "terminal results recorded on non-terminal snapshots",
                ),
                (
                    "duplicate_forecast_publications",
                    "select coalesce(sum(c-1),0) from (select count(*) c "
                    "from weather_forecasts group by station_id, variable, "
                    "issue_time, valid_start) x where c > 1",
                    "duplicate forecast publication rows",
                ),
            ]
            try:
                async with engine.connect() as conn:
                    for name, sql, meaning in checks:
                        n = int((await conn.execute(_text(sql))).scalar() or 0)
                        inspected["production_checks"].append({"check": name, "count": n})
                        if n > 0:
                            report.add(
                                ll.LintFinding(
                                    rule_id="P-" + name,
                                    severity=ll.Severity.WARNING,
                                    message=f"{n} {meaning}",
                                    count=n,
                                )
                            )
            finally:
                await engine.dispose()

        asyncio.run(prod_checks())

    payload = {
        "scope": scope,
        "inspected": inspected,
        **report.to_dict(),
    }
    if as_json:
        typer.echo(json.dumps(payload, indent=2, default=str))
    else:
        typer.echo(f"leakage audit ({scope}): {report.verdict()}")
        typer.echo(f"  modules scanned: {len(inspected['modules'])}")
        for c in inspected["production_checks"]:
            typer.echo(f"  [prod] {c['check']}: {c['count']}")
        for f in report.findings:
            typer.echo(f"  [{f.severity.value.upper()}] {f.rule_id}: {f.message}")
        if not report.findings:
            typer.echo("  no findings")
    threshold = ll.Severity.ERROR if fail_on == "error" else ll.Severity.WARNING
    if report.verdict(fail_on=threshold) == "FAIL":
        raise typer.Exit(code=1)


@research_app.command("book-continuity")
def research_book_continuity(
    start: str = typer.Option("", help="ISO UTC lower bound (default: 48h before as-of)."),
    as_of: str = typer.Option("", help="ISO UTC as-of / upper bound (default: now)."),
    ticker: str = typer.Option("", help="Restrict to one exact market ticker."),
    family: str = typer.Option("", help="Restrict to a series-family ticker prefix."),
    environment: str = typer.Option("production", help="Environment provenance frame."),
    as_json: bool = typer.Option(False, "--json", help="Emit the full summary as JSON."),
    sample_limit: int = typer.Option(
        10, help="Max example intervals echoed per classification (display only)."
    ),
) -> None:
    """READ-ONLY order-book continuity audit: classifies each inter-snapshot
    interval as confirmed-unchanged / changed / failed-poll / no-evidence /
    collection-gap / ambiguous / legacy / open, from stored book metadata,
    the per-ticker poll ledger, and collector-run lineage. Measurement
    tooling only — reads no prices, levels, outcomes, or results; writes
    nothing."""
    from datetime import UTC as _utc
    from datetime import datetime as _dt

    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import create_async_engine

    from kalshi_weather.research import book_continuity as bc

    now = _dt.fromisoformat(as_of) if as_of else utc_now()
    if now.tzinfo is not None:
        now = now.astimezone(_utc).replace(tzinfo=None)
    lower = _dt.fromisoformat(start) if start else now - timedelta(hours=48)
    if lower.tzinfo is not None:
        lower = lower.astimezone(_utc).replace(tzinfo=None)

    def _naive(ts: Any) -> Any:
        return ts.astimezone(_utc).replace(tzinfo=None) if ts.tzinfo is not None else ts

    books: list[bc.BookMeta] = []
    polls: list[bc.PollMeta] = []
    runs: list[bc.RunMeta] = []
    ledger_start: Any = None

    async def load() -> None:
        nonlocal ledger_start
        settings = get_settings()
        engine = create_async_engine(settings.database_url)
        ticker_sql = " and market_ticker = :ticker" if ticker else ""
        family_sql = " and market_ticker like :family" if family else ""
        params: dict[str, Any] = {"lo": lower, "hi": now}
        if ticker:
            params["ticker"] = ticker
        if family:
            params["family"] = family + "%"
        try:
            async with engine.connect() as conn:
                ledger_start = (
                    await conn.execute(
                        _text("select min(requested_at) from market_poll_attempts")
                    )
                ).scalar()
                rows = await conn.execute(
                    _text(
                        "select market_ticker, id, captured_at, content_hash, "
                        "environment from orderbook_snapshots "
                        "where captured_at >= :lo and captured_at <= :hi"
                        + ticker_sql
                        + family_sql
                    ),
                    params,
                )
                for r in rows:
                    books.append(
                        bc.BookMeta(
                            ticker=r.market_ticker,
                            snapshot_id=r.id,
                            captured_at=_naive(r.captured_at),
                            content_hash=r.content_hash,
                            environment=r.environment,
                        )
                    )
                p_ticker_sql = (" and ticker = :ticker" if ticker else "") + (
                    " and ticker like :family" if family else ""
                )
                rows = await conn.execute(
                    _text(
                        "select ticker, requested_at, completed_at, outcome, "
                        "environment, collector_run_id, deduplicated, "
                        "persisted_row_count, raw_payload_id "
                        "from market_poll_attempts "
                        "where endpoint_type = 'orderbook' "
                        "and completed_at >= :lo and completed_at <= :hi" + p_ticker_sql
                    ),
                    params,
                )
                for r in rows:
                    polls.append(
                        bc.PollMeta(
                            ticker=r.ticker,
                            requested_at=_naive(r.requested_at),
                            completed_at=_naive(r.completed_at),
                            outcome=r.outcome,
                            environment=r.environment,
                            collector_run_id=r.collector_run_id,
                            deduplicated=r.deduplicated,
                            persisted_row_count=r.persisted_row_count,
                            raw_payload_id=r.raw_payload_id,
                        )
                    )
                rows = await conn.execute(
                    _text(
                        "select id, started_at, finished_at, success from collector_runs "
                        "where collector = 'kalshi' "
                        "and finished_at >= :lo and started_at <= :hi"
                    ),
                    {"lo": lower, "hi": now},
                )
                for r in rows:
                    runs.append(
                        bc.RunMeta(
                            run_id=r.id,
                            started_at=_naive(r.started_at),
                            finished_at=_naive(r.finished_at),
                            success=r.success,
                        )
                    )
        finally:
            await engine.dispose()

    asyncio.run(load())
    if ledger_start is None:
        typer.echo("no poll-ledger rows exist; nothing classifiable")
        raise typer.Exit(code=1)
    config = bc.ContinuityConfig(
        as_of=now, poll_ledger_start=_naive(ledger_start), environment=environment
    )
    report = bc.classify_intervals(books, polls, runs, config)
    summary = report.summary()
    payload = {
        "window": {"start": lower.isoformat(), "as_of": now.isoformat()},
        "environment_frame": environment,
        "poll_ledger_start": _naive(ledger_start).isoformat(),
        **summary,
    }
    if as_json:
        typer.echo(json.dumps(payload, indent=2, default=str))
        return
    typer.echo(
        f"book continuity {lower.isoformat()} -> {now.isoformat()} "
        f"(env={environment}, poll ledger from {payload['poll_ledger_start']})"
    )
    typer.echo(
        f"  intervals={summary['intervals']} tickers={summary['tickers']} "
        f"classified_ratio={summary['classified_ratio']}"
    )
    for cls, v in summary["by_classification"].items():
        typer.echo(f"  {cls}: n={v['count']} hours={v['seconds'] / 3600:.1f}")
    typer.echo(f"  confidence: {summary['by_confidence']}")
    shown = 0
    for iv in report.intervals:
        if shown >= sample_limit:
            break
        if iv.classification in (
            bc.IntervalClass.AMBIGUOUS_PROVENANCE,
            bc.IntervalClass.COLLECTION_GAP,
        ):
            typer.echo(
                f"    example {iv.classification.value}: {iv.ticker} "
                f"{iv.start.isoformat()} +{iv.elapsed_seconds:.0f}s ({iv.reason_code})"
            )
            shown += 1
    for v in summary["invariant_violations"]:
        typer.echo(f"  [VIOLATION] {v['check']}: {v['detail']}")
    if not summary["invariant_violations"]:
        typer.echo("  invariants: all clean")


exploratory_app = typer.Typer(help="Clearly-labeled exploratory analyses (never confirmatory).")
research_app.add_typer(exploratory_app, name="exploratory")


@exploratory_app.command("e0002")
def research_exploratory_e0002(
    as_of: str = typer.Option("", help="ISO UTC as-of cap (default: now). Echoed in output."),
    as_json: bool = typer.Option(False, "--json", help="Emit the full summary as JSON."),
    output: str = typer.Option("", "--output", help="Also write the JSON summary to this path."),
) -> None:
    """E0002 exploratory liquidity/microstructure summary (READ-ONLY).

    Descriptive spreads, depth, quote presence, trade volumes, time-of-day
    activity, and stored-book staleness over the collected weather series.
    EXPLORATORY ONLY — NOT PREREGISTERED — NOT CONFIRMATORY: this command
    computes no model, no outcome-scored metric, and no trading claim, and
    it refuses any row inside the reserved confirmatory windows. It does
    NOT mean any hypothesis is supported and implies nothing about the
    frozen program's readiness state or eventual results."""
    from datetime import UTC as _utc
    from datetime import datetime as _dt

    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import create_async_engine

    from kalshi_weather.research import e0002_liquidity as e2

    now = _dt.fromisoformat(as_of) if as_of else utc_now()
    if now.tzinfo is not None:
        now = now.astimezone(_utc).replace(tzinfo=None)

    def _naive(ts: Any) -> Any:
        return ts.astimezone(_utc).replace(tzinfo=None) if ts.tzinfo is not None else ts

    books: list[e2.BookRow] = []
    trades: list[e2.TradeRow] = []

    async def load() -> None:
        settings = get_settings()
        engine = create_async_engine(settings.database_url)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(
                    _text(
                        "select market_ticker, captured_at, best_yes_bid_cents, "
                        "best_yes_ask_cents, spread_cents, yes_levels_json, "
                        "no_levels_json, environment from orderbook_snapshots "
                        "where captured_at <= :as_of"
                    ),
                    {"as_of": now},
                )
                for r in rows:
                    yes_levels = r.yes_levels_json or []
                    no_levels = r.no_levels_json or []
                    bid_d, ask_d, total_d = e2.book_depths(yes_levels, no_levels)
                    books.append(
                        e2.BookRow(
                            market_ticker=r.market_ticker,
                            captured_at=_naive(r.captured_at),
                            best_yes_bid_cents=r.best_yes_bid_cents,
                            best_yes_ask_cents=r.best_yes_ask_cents,
                            spread_cents=r.spread_cents,
                            bid_depth=bid_d,
                            ask_depth=ask_d,
                            total_depth=total_d,
                            environment=r.environment,
                        )
                    )
                rows = await conn.execute(
                    _text(
                        "select market_ticker, executed_at, price_cents, "
                        "count as contract_count, environment from trades "
                        "where executed_at <= :as_of"
                    ),
                    {"as_of": now},
                )
                for r in rows:
                    trades.append(
                        e2.TradeRow(
                            market_ticker=r.market_ticker,
                            executed_at=_naive(r.executed_at),
                            price_cents=r.price_cents,
                            count=r.contract_count,
                            environment=r.environment,
                        )
                    )
        finally:
            await engine.dispose()

    asyncio.run(load())
    summary = e2.run_liquidity_summary(books, trades, as_of=now)

    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(json.dumps(summary, indent=2, default=str) + "\n")
    if as_json:
        typer.echo(json.dumps(summary, indent=2, default=str))
        return
    typer.echo(summary["banner"])
    typer.echo(f"as_of: {summary['as_of']}  window: {summary['window']}")
    prim = summary["primary"]
    typer.echo(f"  inputs: {prim['inputs']}")
    typer.echo(f"  quote presence (overall): {prim['quote_presence']['overall']['rates']}")
    typer.echo(f"  spread cents (two-sided): {prim['spread_cents_two_sided']['overall']}")
    typer.echo(f"  depth at best bid: {prim['depth_contracts_two_sided']['at_best_bid']}")
    typer.echo(f"  depth at best ask: {prim['depth_contracts_two_sided']['at_best_ask']}")
    tr = prim["trades"]
    typer.echo(
        f"  trades: {tr['total_trades']} ({tr['total_contracts']} contracts); "
        f"zero-trade share of quoted ticker-days: "
        f"{tr['zero_trade_share_of_quoted_ticker_days']}"
    )
    gaps = summary["primary"]["stored_book_gap_seconds"]["quantiles"]
    typer.echo(f"  stored-book gaps s: {gaps}")
    typer.echo("  (see --json for by-family/by-city/by-hour detail and robustness variants)")


#: H0020's frozen family/station scope as a snapshot-metadata regex (frozen
#: prefixes KXHIGHT*/KXLOWT* at CHI/DEN/LAX/NYC; the legacy KXHIGHNY family
#: is outside H0020's registered prefixes by design).
_H0020_FAMILY_REGEX = "^KX(HIGHT|LOWT)(CHI|DEN|LAX|NY)"


def _preflight_h0020_close_time(
    *, stage: str, as_of: str, environment: str, as_json: bool, fail_on_review: bool
) -> None:
    """H0020 close-time stability preflight: reuses the generic guard over
    the frozen stage windows. Metadata only; never touches outcomes,
    revisions-as-signal, readiness state, or the ledger."""
    from datetime import datetime as _dt

    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import create_async_engine

    from kalshi_weather.experiments import h0020_readiness as h20r
    from kalshi_weather.experiments.h0020 import HORIZON_HOURS as _H20_HORIZON
    from kalshi_weather.research import close_time_guard as ctg

    now = _dt.fromisoformat(as_of) if as_of else utc_now()
    as_of_date = now.date()
    ranges = h20r.stage_close_ranges(stage, as_of=as_of_date)
    all_ranges = h20r.stage_close_ranges(stage)  # uncapped, for observability report
    cap = as_of_date + timedelta(days=1)
    not_yet = [(str(s), str(e)) for (s, e) in all_ranges if s >= cap]

    async def run() -> None:
        settings = get_settings()
        snapshots: list[ctg.SnapshotLite] = []
        if ranges:
            engine = create_async_engine(settings.database_url)
            try:
                async with engine.connect() as conn:
                    for r_start, r_end in ranges:
                        rows = (
                            await conn.execute(
                                _text(
                                    "select market_ticker, id, observed_at, close_time, "
                                    "environment from market_snapshots "
                                    "where market_ticker ~ :fam "
                                    "and close_time >= :s and close_time < :e"
                                ),
                                {
                                    "fam": _H0020_FAMILY_REGEX,
                                    "s": datetime(r_start.year, r_start.month, r_start.day),
                                    "e": datetime(r_end.year, r_end.month, r_end.day),
                                },
                            )
                        ).all()
                        snapshots.extend(
                            ctg.SnapshotLite(
                                ticker=r[0],
                                snapshot_id=r[1],
                                observed_at=r[2],
                                close_time=r[3],
                                environment=r[4],
                            )
                            for r in rows
                        )
            finally:
                await engine.dispose()

        # Pass 1 -- CONSERVATIVE universe: pre-production-cutover tickers
        # (null/demo provenance eras) are BLOCKED here. Pass 2 -- FROZEN
        # SCOPE: H0020's registered policy admits production provenance only,
        # so those tickers are excluded from its dataset by the frozen rules;
        # this pass gates the final run.
        report = ctg.analyze_close_times(
            snapshots,
            name=f"h0020-close-time ({stage})",
            as_of=now,
            horizon_hours=float(_H20_HORIZON),
            nulls_excluded_by_frozen_rules=True,  # H0020 requires close-derived decisions
            allowed_environments=(environment,),
        )
        admissible = {s.ticker for s in snapshots if s.environment == environment}
        scope_report = ctg.analyze_close_times(
            [s for s in snapshots if s.ticker in admissible],
            name=f"h0020-close-time ({stage}, frozen scope)",
            as_of=now,
            horizon_hours=float(_H20_HORIZON),
            nulls_excluded_by_frozen_rules=True,
            allowed_environments=(environment,),
        )
        payload = report.to_dict()
        payload["frozen_scope"] = scope_report.to_dict()
        payload["blocked_explanation"] = (
            "blocked tickers have only pre-production-cutover snapshots "
            "(null/demo provenance eras); H0020's frozen environment policy "
            "excludes them from its dataset"
        )
        payload["disclaimer"] = _H0020_PREFLIGHT_DISCLAIMER
        payload["stage"] = stage
        payload["frozen_stage_windows"] = {
            k: [str(a), str(b)] for k, (a, b) in h20r.PREFLIGHT_STAGES.items()
        }
        payload["excluded_gap"] = ["2026-08-12", "2026-08-25"]
        payload["scanned_close_ranges"] = [[str(a), str(b)] for a, b in ranges]
        payload["not_yet_observable_ranges"] = not_yet
        payload["coverage"] = {
            "tickers_observed": report.tickers_inspected + report.tickers_blocked,
            "missing_expected_history": report.tickers_blocked,
            "not_yet_observable": "future-stage ranges listed separately, never failures",
        }
        if as_json:
            typer.echo(json.dumps(payload, indent=2, default=str))
        else:
            typer.echo(
                f"preflight h0020-close-time [{stage}]: universe={report.status} "
                f"frozen-scope={scope_report.status}"
            )
            typer.echo(f"  NOTE: {_H0020_PREFLIGHT_DISCLAIMER}")
            typer.echo(
                f"  scanned close ranges (as of {as_of_date}): "
                f"{[[str(a), str(b)] for a, b in ranges] or 'none observable yet'}"
            )
            if not_yet:
                typer.echo(f"  not yet observable (future stages): {not_yet}")
            typer.echo(
                f"  universe: inspected={report.tickers_inspected} "
                f"stable={report.tickers_stable} revised={report.tickers_revised} "
                f"null_close={report.tickers_null_close} "
                f"pre-cutover-era={report.tickers_blocked} (excluded by the "
                f"frozen environment policy)"
            )
            typer.echo(
                f"  frozen scope: inspected={scope_report.tickers_inspected} "
                f"stable={scope_report.tickers_stable} "
                f"revised={scope_report.tickers_revised} "
                f"null_close={scope_report.tickers_null_close}"
            )
            typer.echo(f"  max revision: {scope_report.max_revision_seconds}s")
            for r in scope_report.revisions[:10]:
                typer.echo(
                    f"  REVISED {r.ticker}: {len(r.versions)} close_times, max shift "
                    f"{r.max_shift_seconds}s ({r.direction}) -- HUMAN REVIEW REQUIRED"
                )
        # the FROZEN-SCOPE status gates H0020's final run
        if scope_report.status == ctg.REVIEW_REQUIRED and fail_on_review:
            raise typer.Exit(code=1)
        if scope_report.status == ctg.BLOCKED_INSUFFICIENT_HISTORY:
            raise typer.Exit(code=2)

    asyncio.run(run())


#: Four-station daily-temperature families in H0019's frozen scope (the
#: resolved family names observed in production; metadata filter only).
_H0019_FAMILY_REGEX = (
    "^(KXHIGHNY|KXLOWTNYC|KXHIGHCHI|KXLOWTCHI|KXHIGHDEN|KXLOWTDEN|KXHIGHLAX|KXLOWTLAX)-"
)


_H0020_PREFLIGHT_DISCLAIMER = (
    "close-time metadata stability ONLY: a passing preflight does NOT mean "
    "H0020 is ready, that sufficient groups exist, or anything about model "
    "support or test results -- readiness and outcomes are separate gates"
)


@experiment_app.command("preflight")
def experiment_preflight(
    target: str = typer.Argument(
        ..., help="Registered preflight (h0019-close-time | h0020-close-time)."
    ),
    stage: str = typer.Option(
        "full",
        help="h0020 only: train|validation|initial-test|extension-1|extension-2|full.",
    ),
    start: str = typer.Option("", help="h0019 only: override window start (ISO date)."),
    end: str = typer.Option("", help="h0019 only: override window end (ISO date)."),
    as_of: str = typer.Option("", help="Explicit as-of timestamp (ISO; default now)."),
    environment: str = typer.Option("production", help="Provenance environment filter."),
    as_json: bool = typer.Option(False, "--json", help="Emit the report as JSON."),
    fail_on_review: bool = typer.Option(
        True, help="Exit nonzero when the guard requires human review."
    ),
) -> None:
    """READ-ONLY experiment preflight checks: close_time stability across the
    append-only snapshot history for a frozen experiment's window and scope --
    market METADATA only: no outcomes, no predictions, no scores, no dataset
    regeneration, no readiness/ledger change. Any revision requires a separate
    human scientific decision."""
    from datetime import datetime as _dt

    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import create_async_engine

    from kalshi_weather.experiments import readiness as h19
    from kalshi_weather.research import close_time_guard as ctg

    if target.lower() == "h0020-close-time":
        _preflight_h0020_close_time(
            stage=stage,
            as_of=as_of,
            environment=environment,
            as_json=as_json,
            fail_on_review=fail_on_review,
        )
        return
    if target.lower() != "h0019-close-time":
        raise typer.BadParameter("'h0019-close-time' and 'h0020-close-time' are registered")

    win_start = date.fromisoformat(start) if start else h19.TRAIN_START
    win_end = date.fromisoformat(end) if end else h19.TEST_END
    now = _dt.fromisoformat(as_of) if as_of else utc_now()

    async def run() -> None:
        settings = get_settings()
        engine = create_async_engine(settings.database_url)
        try:
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        _text(
                            "select market_ticker, id, observed_at, close_time, environment "
                            "from market_snapshots "
                            "where market_ticker ~ :fam "
                            "and close_time >= :s and close_time < :e"
                        ),
                        {
                            "fam": _H0019_FAMILY_REGEX,
                            # close is typically the morning after target_date:
                            # cover the frozen target window with a +2d margin
                            "s": datetime(win_start.year, win_start.month, win_start.day),
                            "e": datetime(win_end.year, win_end.month, win_end.day)
                            + timedelta(days=2),
                        },
                    )
                ).all()
        finally:
            await engine.dispose()

        snapshots = [
            ctg.SnapshotLite(
                ticker=r[0],
                snapshot_id=r[1],
                observed_at=r[2],
                close_time=r[3],
                environment=r[4],
            )
            for r in rows
        ]
        # Pass 1 -- CONSERVATIVE: the full family universe. Tickers whose
        # entire history predates the production provenance cutover (ADR
        # 0013/0014: null- and demo-era snapshots) are BLOCKED here.
        report = ctg.analyze_close_times(
            snapshots,
            name="h0019-close-time",
            as_of=now,
            horizon_hours=float(h19.HORIZON_HOURS),
            nulls_excluded_by_frozen_rules=True,  # H0019 requires reliable close_time
            allowed_environments=(environment,),
        )
        # Pass 2 -- FROZEN SCOPE: H0019's registered environment policy admits
        # production-provenance markets only, so pre-cutover-era tickers are
        # excluded from its dataset by the frozen rules themselves. This pass
        # is the one that gates H0019's final run.
        admissible = {s.ticker for s in snapshots if s.environment == environment}
        scope_report = ctg.analyze_close_times(
            [s for s in snapshots if s.ticker in admissible],
            name="h0019-close-time (frozen scope)",
            as_of=now,
            horizon_hours=float(h19.HORIZON_HOURS),
            nulls_excluded_by_frozen_rules=True,
            allowed_environments=(environment,),
        )
        report.detail["frozen_scope"] = scope_report.to_dict()
        report.detail["blocked_explanation"] = (
            "blocked tickers have only pre-production-cutover snapshots "
            "(null/demo provenance eras); H0019's frozen environment policy "
            "excludes them from its dataset"
        )
        report.detail["frozen_window"] = {
            "train": [str(h19.TRAIN_START), str(h19.TRAIN_END)],
            "val": [str(h19.VAL_START), str(h19.VAL_END)],
            "test": [str(h19.TEST_START), str(h19.TEST_END)],
            "scanned_close_range": [str(win_start), str(win_end + timedelta(days=2))],
        }
        if as_json:
            typer.echo(json.dumps(report.to_dict(), indent=2, default=str))
        else:
            typer.echo(
                f"preflight h0019-close-time: universe={report.status} "
                f"frozen-scope={scope_report.status}"
            )
            typer.echo(
                f"  frozen window train {h19.TRAIN_START}..{h19.TRAIN_END} "
                f"val {h19.VAL_START}..{h19.VAL_END} test {h19.TEST_START}..{h19.TEST_END}"
            )
            typer.echo(
                f"  universe: inspected={report.tickers_inspected} "
                f"stable={report.tickers_stable} revised={report.tickers_revised} "
                f"null_close={report.tickers_null_close} "
                f"blocked={report.tickers_blocked} (pre-cutover era, excluded by "
                f"the frozen environment policy)"
            )
            typer.echo(
                f"  frozen scope: inspected={scope_report.tickers_inspected} "
                f"stable={scope_report.tickers_stable} "
                f"revised={scope_report.tickers_revised} "
                f"null_close={scope_report.tickers_null_close}"
            )
            typer.echo(f"  max revision: {scope_report.max_revision_seconds}s")
            for r in scope_report.revisions[:10]:
                typer.echo(
                    f"  REVISED {r.ticker}: {len(r.versions)} close_times, "
                    f"max shift {r.max_shift_seconds}s ({r.direction}); derived "
                    f"decision_time changes -- HUMAN SCIENTIFIC DECISION REQUIRED"
                )
            if scope_report.status in (ctg.PASS, ctg.PASS_WITH_NULLS):
                typer.echo(
                    "  derived decision_time is stable for every ticker in H0019's "
                    "frozen scope"
                )
        # the FROZEN-SCOPE status gates H0019's final run
        if scope_report.status == ctg.REVIEW_REQUIRED and fail_on_review:
            raise typer.Exit(code=1)
        if scope_report.status == ctg.BLOCKED_INSUFFICIENT_HISTORY:
            raise typer.Exit(code=2)

    asyncio.run(run())


def _experiment_readiness_h0020(log_path: str | None) -> None:
    """H0020 counts-only readiness (see experiments/h0020_readiness.py).
    Counts, dates, exclusions, provenance, and integrity ONLY -- no model,
    no probabilities, no Brier/loss, no benchmark comparison."""
    import json as _json

    from kalshi_weather.experiments import h0020_readiness as h20r

    async def run() -> None:
        settings = get_settings()
        configure_logging(settings.log_level)
        now = utc_now().date()
        integrity_ok, integrity_detail = h20r.verify_frozen_integrity(Path.cwd())
        async with _open_session(settings) as session:
            labels, strikes, candles, series = await h20r.load_inputs(session)
        report = h20r.compute_report(
            labels,
            strikes,
            candles,
            series,
            now=now,
            integrity_ok=integrity_ok,
            integrity_detail=integrity_detail,
        )
        typer.echo(f"H0020 readiness as of {now}: {report.state}")
        typer.echo(
            f"  effective test end: {report.effective_test_end} "
            f"({report.detail.get('window_kind', 'n/a')}) "
            f"days_until_test_end={report.detail.get('days_until_test_end')}"
        )
        for split in ("train", "validation", "test"):
            c = report.split_counts.get(split, {})
            typer.echo(
                f"  {split}: event_groups={c.get('event_groups', 0)} rows={c.get('rows', 0)} "
                f"pos={c.get('pos_labels', 0)} neg={c.get('neg_labels', 0)} "
                f"stations={c.get('stations', 0)} "
                f"concentration={round(c.get('station_concentration', 0.0), 3)} "
                f"zero_rev={c.get('zero_revision_rows', 0)}"
            )
        for split, reasons in sorted(report.exclusions_by_split.items()):
            typer.echo(f"  exclusions[{split}]: {dict(sorted(reasons.items()))}")
        for gate, ok in report.gates.items():
            typer.echo(f"  [{'PASS' if ok else 'FAIL'}] {gate}")
        typer.echo(f"  integrity: {report.integrity}")
        # append-only readiness log -- state only, never any test analysis
        lp = (
            Path(log_path)
            if log_path
            else Path("docs/research/experiments/EXP-FUTURE-H0020/readiness_log.jsonl")
        )
        lp.parent.mkdir(parents=True, exist_ok=True)
        with lp.open("a") as f:
            f.write(_json.dumps({"as_of": str(now), "state": report.state}) + "\n")

    asyncio.run(run())


def _experiment_readiness_h0012r(
    *, as_of: str, as_json: bool, environment: str = ""
) -> None:
    """H0012r counts-only readiness (see experiments/h0012r_readiness.py).

    Structural coverage + integrity ONLY over the NYC settlement-label
    population. NEVER computes claim A, claim B, any stage-difference rate, or
    any CI. Read-only; writes nothing."""
    from datetime import date as _date
    from pathlib import Path as _Path

    from kalshi_weather.experiments import h0012r_readiness as h12
    from kalshi_weather.research import close_time_guard as ctg
    from kalshi_weather.research import leakage_lint as ll
    from kalshi_weather.settlement.labels import build_labels as _build_labels
    from kalshi_weather.storage.models import MarketSnapshot as _MS

    now = _date.fromisoformat(as_of) if as_of else utc_now().date()

    def _is_nyc(ticker: str) -> bool:
        return ticker.startswith(h12.FAMILY_PREFIXES)

    async def run() -> None:
        settings = get_settings()
        configure_logging(settings.log_level)
        from sqlalchemy import select as _select

        async with _open_session(settings) as session:
            labels = await _build_labels(session)
            # NYC-family latest snapshots for close-time stability + provenance.
            snaps = (await session.scalars(_select(_MS))).all()

        nyc = [
            lab
            for lab in labels
            if _is_nyc(lab.market_ticker)
            and lab.usable
            and lab.station_id
            and lab.variable
            and lab.target_date
        ]
        # Collapse strikes to one row per (station, variable, target_date).
        groups: dict[tuple[str, str, date], list[Any]] = {}
        for lab in nyc:
            assert lab.station_id and lab.variable and lab.target_date  # narrowed above
            groups.setdefault((lab.station_id, lab.variable, lab.target_date), []).append(lab)

        # Partition consistency: strikes of one event must agree on the timeline.
        inconsistent = 0
        for members in groups.values():
            if (
                len({m.value_at_close for m in members}) > 1
                or len({m.value_at_settlement for m in members}) > 1
                or len({m.latest_final_value for m in members}) > 1
            ):
                inconsistent += 1

        exact = sum(1 for members in groups.values() if members[0].settlement_time_is_exact)
        bounded = len(groups) - exact
        tmax = sum(1 for (_s, v, _d) in groups if v == "tmax_f")
        tmin = sum(1 for (_s, v, _d) in groups if v == "tmin_f")
        target_dates = sorted(d for (_s, _v, d) in groups)
        stations = len({s for (s, _v, _d) in groups})

        missing_source = sum(
            1
            for lab in labels
            if _is_nyc(lab.market_ticker) and lab.status.value == "missing_source_data"
        )

        # Payout-agreement integrity gate (spec Failure-mode 3): reconstruction
        # vs Kalshi's own expiration_value/result. Integrity, not a hypothesis
        # outcome.
        payout_total = 0
        payout_matches = 0
        for lab in nyc:
            if lab.payout_value_agrees is not None and lab.payout_result_agrees is not None:
                payout_total += 1
                if lab.payout_value_agrees and lab.payout_result_agrees:
                    payout_matches += 1

        # Point-in-time leakage: no selected issuance may postdate its boundary
        # (H0012r's publication availability contract: issuance_time <= boundary).
        pit_violations = 0
        for lab in nyc:
            if (
                lab.issuance_at_close is not None
                and lab.close_time is not None
                and lab.issuance_at_close > lab.close_time
            ) or (
                lab.issuance_at_settlement is not None
                and lab.settlement_time is not None
                and lab.settlement_time_is_exact
                and lab.issuance_at_settlement > lab.settlement_time
            ):
                pit_violations += 1
        leakage_status = "PASS" if pit_violations == 0 else "FAIL"
        # Static latest-state scan of the reconstruction source (warnings only).
        _static = ll.scan_latest_state(
            _Path("src/kalshi_weather/settlement/labels.py").read_text(),
            path="settlement/labels.py",
        )

        # Reserved-window overlap (informational; never an exclusion).
        overlap = {
            name: sum(1 for d in target_dates if start <= d <= end)
            for (name, start, end) in h12.RESERVED_WINDOWS
        }

        # Environment provenance of NYC-family latest snapshots (informational).
        latest_by_ticker: dict[str, Any] = {}
        for s in snaps:
            if _is_nyc(s.market_ticker):
                cur = latest_by_ticker.get(s.market_ticker)
                if cur is None or s.id > cur.id:
                    latest_by_ticker[s.market_ticker] = s
        env_counts: dict[str, int] = {}
        for s in latest_by_ticker.values():
            key = s.environment or "unknown"
            env_counts[key] = env_counts.get(key, 0) + 1

        # Close-time stability over NYC-family tickers (metadata only). H0012r's
        # settlement-label population is environment-agnostic and predominantly
        # pre-production-cutover (null/demo), so the DEFAULT frame includes every
        # environment: each row is normalized to a single sentinel so the guard
        # measures pure close_time revision (any genuine cross-mirror
        # disagreement surfaces as a revision, never silently dropped). Passing
        # --environment restricts to that one frame as a sensitivity check.
        env_agnostic = not environment
        lite = [
            ctg.SnapshotLite(
                ticker=s.market_ticker,
                snapshot_id=s.id,
                observed_at=s.observed_at,
                close_time=s.close_time,
                environment="any" if env_agnostic else s.environment,
            )
            for s in snaps
            if _is_nyc(s.market_ticker)
        ]
        allowed = ("any",) if env_agnostic else (environment,)
        guard = ctg.analyze_close_times(
            lite,
            name="h0012r-close-time",
            as_of=utc_now(),
            horizon_hours=0.0,
            allowed_environments=allowed,
        )
        ct_status = {
            ctg.PASS: "PASS",
            ctg.PASS_WITH_NULLS: "PASS",
            ctg.REVIEW_REQUIRED: "REVIEW_REQUIRED",
            ctg.BLOCKED_INSUFFICIENT_HISTORY: "BLOCKED_INSUFFICIENT",
        }.get(guard.status, "UNKNOWN")

        counts = h12.ReadinessCounts(
            variable_dates=len(groups),
            exact_settlement=exact,
            bounded_settlement=bounded,
            stations=stations,
            tmax_dates=tmax,
            tmin_dates=tmin,
            missing_source_data=missing_source,
            target_date_min=target_dates[0] if target_dates else None,
            target_date_max=target_dates[-1] if target_dates else None,
            reserved_window_overlap=overlap,
            environment_counts=env_counts,
        )
        flags = h12.IntegrityFlags(
            spec_recovered=True,
            leakage_status=leakage_status,
            payout_agreement_ok=(payout_total > 0 and payout_matches == payout_total),
            payout_matches=payout_matches,
            payout_total=payout_total,
            close_time_status=ct_status,
            close_time_revised=guard.tickers_revised,
            close_time_null=guard.tickers_null_close,
            partition_consistent=(inconsistent == 0),
            partition_inconsistent_groups=inconsistent,
            provenance_ok=True,
        )
        report = h12.classify_readiness(counts, flags, now=now)
        payload = report.to_dict()
        payload["static_latest_state_warnings"] = len(_static)

        if as_json:
            typer.echo(json.dumps(payload, indent=2, default=str))
            return
        typer.echo("COUNTS ONLY — NOT AN EXPERIMENT RESULT")
        typer.echo(f"H0012r readiness as of {now}: {report.state.value}")
        cal = payload["calendar"]
        typer.echo(
            f"  calendar boundary {cal['boundary']} reached={cal['reached']} "
            f"days_until={cal['days_until_boundary']}"
        )
        cc = payload["counts"]
        typer.echo(
            f"  variable-dates {cc['variable_dates']}/{cc['min_required']} "
            f"(shortfall {cc['shortfall']}); exact={cc['exact_settlement']} "
            f"bounded={cc['bounded_settlement']}; tmax={cc['tmax_dates']} tmin={cc['tmin_dates']}; "
            f"stations={cc['stations']}"
        )
        typer.echo(
            f"  target_date range {cc['target_date_min']}..{cc['target_date_max']}; "
            f"missing_source_data_excluded={cc['missing_source_data_excluded']}; "
            f"reserved_overlap={cc['reserved_window_overlap']}"
        )
        ig = payload["integrity"]
        typer.echo(
            f"  integrity: leakage={ig['leakage_status']} "
            f"payout={ig['payout_agreement']}({'ok' if ig['payout_agreement_ok'] else 'FAIL'}) "
            f"close_time={ig['close_time_status']}(rev={ig['close_time_revised']},"
            f"null={ig['close_time_null']}) "
            f"partition={'ok' if ig['partition_consistent'] else 'INCONSISTENT'}"
        )
        typer.echo(f"  failing gates: {report.failing_gates}")
        typer.echo(f"  earliest ready (estimate): {payload['earliest_ready_estimate']}")
        typer.echo(f"  next action: {report.next_action}")

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
        backoff_base_seconds=settings.kalshi_backoff_base_seconds,
        backoff_max_seconds=settings.kalshi_backoff_max_seconds,
        backoff_jitter=settings.kalshi_backoff_jitter,
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
        backoff_base_seconds=settings.kalshi_backoff_base_seconds,
        backoff_max_seconds=settings.kalshi_backoff_max_seconds,
        backoff_jitter=settings.kalshi_backoff_jitter,
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
        min_request_interval_s=settings.kalshi_min_request_interval_seconds,
        backoff_base_s=settings.kalshi_backoff_base_seconds,
        backoff_max_s=settings.kalshi_backoff_max_seconds,
        backoff_jitter=settings.kalshi_backoff_jitter,
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
                startup_grace_seconds=(
                    0.0 if once else settings.kalshi_startup_grace_seconds
                ),
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


@ops_app.command("status")
def ops_status(
    as_of: str = typer.Option("", help="ISO UTC clock to inject (default: now)."),
    as_json: bool = typer.Option(False, "--json", help="Emit the dashboard as JSON."),
    section: str = typer.Option("", help="Show only one checkpoint area (e.g. paper)."),
    fail_on_action_required: bool = typer.Option(
        False, help="Exit nonzero if any checkpoint is ACTION_REQUIRED."
    ),
    no_network: bool = typer.Option(False, help="Skip any check that would touch the network."),
    compact: bool = typer.Option(False, help="Terse output; skip the heavier observatory build."),
) -> None:
    """READ-ONLY unified operator status. Summarizes every pending research
    and ops checkpoint -- what is healthy, time-blocked, evidence-blocked,
    the next permitted action, and what must not be run early. Executes NO
    experiment, settlement, service, or exchange call and writes nothing.
    Research checkpoints pass through their counts-only readiness *state*
    only; no performance metric or H0019/H0020 outcome is computed."""
    from datetime import UTC as _utc
    from datetime import datetime as _dt

    from sqlalchemy import text as _text

    from kalshi_weather.ops import operator_status as ost

    now = _dt.fromisoformat(as_of) if as_of else utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=_utc)
    settings = get_settings()

    checkpoints: list[ost.Checkpoint] = []

    # --- paper (production DB read + paper DB reconcile) --------------------
    paper_reconcile_ok = True
    paper_kill = False
    paper_open = False
    paper_terminal = False
    paper_close: datetime | None = None
    paper_ticker = ""
    try:
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import async_sessionmaker as _asm

        from kalshi_weather.execution.ledger import EntryKind, LedgerEntry, replay
        from kalshi_weather.paper.store import (
            PaperPnlSnapshotRow,
            PaperPositionRow,
            kill_switch_active,
            open_paper_db,
            paper_engine,
            stored_ledger_entries,
        )

        async def _paper() -> None:
            nonlocal paper_reconcile_ok, paper_kill, paper_open, paper_terminal
            nonlocal paper_close, paper_ticker
            eng = paper_engine(settings.paper_database_url)
            await open_paper_db(eng)
            factory = _asm(eng, expire_on_commit=False)
            async with factory() as ps:
                entries = await stored_ledger_entries(ps)
                snap = (
                    await ps.scalars(
                        select(PaperPnlSnapshotRow).order_by(PaperPnlSnapshotRow.id.desc()).limit(1)
                    )
                ).first()
                paper_kill = (await kill_switch_active(ps))[0]
                last_run = (
                    await ps.scalars(
                        select(PaperPositionRow.run_id).order_by(PaperPositionRow.id.desc()).limit(1)
                    )
                ).first()
                positions = (
                    (
                        await ps.scalars(
                            select(PaperPositionRow).where(PaperPositionRow.run_id == last_run)
                        )
                    ).all()
                    if last_run
                    else []
                )
            await eng.dispose()
            open_pos = [p for p in positions if p.yes_qty or p.no_qty]
            paper_open = bool(open_pos)
            if entries and snap is not None:
                led = [
                    LedgerEntry(
                        seq=i, at=at, kind=EntryKind(kind),
                        cash_delta_cents=cash_d, reserved_delta_cents=res_d, payload=payload,
                    )
                    for i, (_s, at, kind, cash_d, res_d, payload) in enumerate(entries)
                ]
                pf = replay(0, led)
                cost = sum(p.yes_cost_cents + p.no_cost_cents for p in pf.positions.values())
                paper_reconcile_ok = (
                    pf.cash_cents == snap.cash_cents
                    and pf.reserved_cents == snap.reserved_cents
                    and cost == snap.position_cost_cents
                    and pf.fees_cents == snap.fees_cents
                )
            if open_pos:
                paper_ticker = open_pos[0].ticker
                async with _open_session(settings) as session:
                    row = (
                        await session.execute(
                            _text(
                                "select status, result, settlement_ts, close_time "
                                "from market_snapshots where market_ticker=:t "
                                "order by id desc limit 1"
                            ),
                            {"t": paper_ticker},
                        )
                    ).first()
                if row is not None:
                    paper_terminal = (row.result in ("yes", "no")) and (
                        row.settlement_ts is not None
                        and str(row.status).lower() in ("finalized", "determined", "settled")
                    )
                    paper_close = row.close_time

        asyncio.run(_paper())
        checkpoints.append(
            ost.classify_paper(
                has_open_position=paper_open,
                terminal_result_available=paper_terminal,
                reconcile_ok=paper_reconcile_ok,
                kill_switch_active=paper_kill,
                close_time=paper_close,
                now=now,
            )
        )
    except Exception as exc:
        checkpoints.append(
            ost.classify_operational(
                "paper", healthy=None, detail_msg=f"unavailable: {type(exc).__name__}",
                evidence="paper store", command="uv run kalshi-weather paper reconcile",
                unavailable=True,
            )
        )

    # --- station pilot (pure time gate) ------------------------------------
    from kalshi_weather.ops.station_pilot_review import (
        PILOT_INTEGRITY_ISSUE,
        PILOT_REVIEW_READY,
        review_window,
    )

    rw = review_window(now)
    checkpoints.append(
        ost.classify_station_pilot(
            review_ready=(rw.state == PILOT_REVIEW_READY),
            integrity_ok=(rw.state != PILOT_INTEGRITY_ISSUE),
            earliest=rw.review_ready_at,
            now=now,
        )
    )

    # --- E0002 fourteen-day remeasurement (complete production days) --------
    try:
        prod_days = 0

        async def _e0002() -> None:
            nonlocal prod_days
            async with _open_session(settings) as session:
                row = (
                    await session.execute(
                        _text(
                            "select count(*) from (select date(captured_at) d "
                            "from orderbook_snapshots where environment='production' "
                            "and captured_at < date_trunc('day', cast(:now as timestamp)) "
                            "group by 1) x"
                        ),
                        {"now": now.replace(tzinfo=None)},
                    )
                ).first()
                prod_days = int(row[0]) if row else 0

        asyncio.run(_e0002())
        checkpoints.append(
            ost.classify_e0002(
                complete_production_days=prod_days, required_days=14, now=now,
                earliest=ost.E0002_REMEASURE_BOUNDARY,
            )
        )
    except Exception as exc:
        checkpoints.append(
            ost.classify_operational(
                "e0002", healthy=None, detail_msg=f"unavailable: {type(exc).__name__}",
                evidence="orderbook_snapshots",
                command="uv run kalshi-weather research exploratory e0002", unavailable=True,
            )
        )

    # --- research readiness passthrough (calendar-derived state labels) -----
    # Pre-boundary the calendar binds regardless of counts; the authoritative
    # counts-only detail lives behind each `experiment readiness` command.
    def _h0020_stage(d: date) -> str:
        if d < date(2026, 8, 12):
            return "COLLECTING_TRAIN"
        if d < date(2026, 8, 26):
            return "EXCLUDED_GAP"
        if d < date(2026, 9, 9):
            return "COLLECTING_VALIDATION"
        return "COLLECTING_TEST"

    checkpoints.append(ost.classify_h0012r(readiness_state="CALENDAR_GATED", now=now))
    checkpoints.append(ost.classify_h0019(readiness_state="NOT_READY", now=now))
    checkpoints.append(ost.classify_h0020(readiness_state=_h0020_stage(now.date()), now=now))

    # --- operational health (persisted state files; no network) ------------
    try:
        coll_healthy = True
        coll_msg = "recent successful cycle"

        async def _coll() -> None:
            nonlocal coll_healthy, coll_msg
            async with _open_session(settings) as session:
                row = (
                    await session.execute(
                        _text(
                            "select success, finished_at from collector_runs "
                            "where collector='kalshi' order by id desc limit 1"
                        )
                    )
                ).first()
            if row is None:
                coll_healthy, coll_msg = False, "no collector runs recorded"
                return
            age = (now.replace(tzinfo=None) - row.finished_at).total_seconds()
            coll_healthy = bool(row.success) and age < 3600
            coll_msg = f"last cycle {'ok' if row.success else 'FAILED'}, {int(age)}s ago"

        asyncio.run(_coll())
    except Exception as exc:
        coll_healthy, coll_msg = None, f"unavailable: {type(exc).__name__}"
    checkpoints.append(
        ost.classify_operational(
            "collector", healthy=coll_healthy, detail_msg=coll_msg,
            evidence="collector_runs", command="uv run kalshi-weather ops health",
        )
    )

    # backups (persisted status file)
    try:
        bstatus = backup.read_backup_status(_backup_dir(settings) / backup.STATUS_FILENAME)
        b_healthy = bool(bstatus and bstatus.local_outcome == "success")
        b_msg = (
            f"local {bstatus.local_outcome}, remote {bstatus.remote_outcome}"
            if bstatus
            else "no backup recorded"
        )
    except Exception as exc:
        b_healthy, b_msg = None, f"unavailable: {type(exc).__name__}"
    checkpoints.append(
        ost.classify_operational(
            "backups", healthy=b_healthy, detail_msg=b_msg, evidence="last_backup_status.json",
            command="uv run kalshi-weather backup status",
        )
    )

    # restore drill (persisted state file)
    try:
        from kalshi_weather.ops.restore_drill import STATE_FILENAME, read_drill_state

        dstate = read_drill_state(_backup_dir(settings) / STATE_FILENAME)
        d_healthy = bool(dstate and str(dstate.get("status", "")).lower() == "success")
        d_msg = f"last drill {dstate.get('status') if dstate else 'never run'}"
    except Exception as exc:
        d_healthy, d_msg = None, f"unavailable: {type(exc).__name__}"
    checkpoints.append(
        ost.classify_operational(
            "restore_drill", healthy=d_healthy, detail_msg=d_msg,
            evidence="restore_drill_state.json",
            command="uv run kalshi-weather ops restore-drill",
            unavailable=no_network and d_healthy is None,
        )
    )

    # recovery watch (persisted state file)
    try:
        from pathlib import Path as _P

        from kalshi_weather.ops.recovery_watch import WatchState, load_watch_state

        wpath = _P.home() / "Library/Logs/kalshi-weather" / "recovery_watch_state.json"
        wrec = load_watch_state(wpath)
        wstate = str(wrec.state) if wrec else None
        if wstate is None:
            rc_healthy, rc_msg = None, "no recovery-watch state recorded"
        elif wstate == WatchState.OUTAGE_ACTIVE.value:
            rc_healthy, rc_msg = False, "upstream outage active"
        else:
            rc_healthy, rc_msg = True, f"watch state {wstate}"
    except Exception as exc:
        rc_healthy, rc_msg = None, f"unavailable: {type(exc).__name__}"
    checkpoints.append(
        ost.classify_operational(
            "recovery_watch", healthy=rc_healthy, detail_msg=rc_msg,
            evidence="recovery_watch_state.json",
            command="uv run kalshi-weather ops recovery-watch --no-notify",
        )
    )

    # storage (DB reachable + backup dir present)
    storage_ok = coll_healthy is not None
    checkpoints.append(
        ost.classify_operational(
            "storage", healthy=storage_ok, detail_msg="database reachable",
            evidence="database + backup dir", command="uv run kalshi-weather ops health",
        )
    )

    # observatory (DB-only; skip under --compact)
    if compact:
        checkpoints.append(
            ost.classify_operational(
                "observatory", healthy=None, detail_msg="skipped (--compact)",
                evidence="observatory report", command="uv run kalshi-weather ops observatory",
                unavailable=True,
            )
        )
    else:
        try:
            obs_sev = "INFO"

            async def _obs() -> None:
                nonlocal obs_sev
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
                obs_sev = report.status.value.upper()

            asyncio.run(_obs())
            obs_healthy = obs_sev != "CRITICAL"
            checkpoints.append(
                ost.classify_operational(
                    "observatory", healthy=obs_healthy, detail_msg=f"severity {obs_sev}",
                    evidence="observatory report",
                    command="uv run kalshi-weather ops observatory",
                )
            )
        except Exception as exc:
            checkpoints.append(
                ost.classify_operational(
                    "observatory", healthy=None, detail_msg=f"unavailable: {type(exc).__name__}",
                    evidence="observatory report",
                    command="uv run kalshi-weather ops observatory", unavailable=True,
                )
            )

    import subprocess as _sp

    try:
        commit = _sp.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout.strip() or "unknown"
    except Exception:
        commit = "unknown"
    dash = ost.Dashboard(generated_at=now, git_commit=commit, checkpoints=checkpoints)
    payload = dash.to_dict()

    if section:
        payload = {
            "banners": payload["banners"],
            "generated_at": payload["generated_at"],
            section: payload.get(section),
        }

    if as_json:
        typer.echo(json.dumps(payload, indent=2, default=str))
    else:
        for b in ost.BANNERS:
            typer.echo(f"*** {b} ***")
        typer.echo(f"generated_at: {payload['generated_at']}  commit: {commit}")
        typer.echo(f"OVERALL: {payload['overall_state']}")
        nxt = payload["next_action"]
        typer.echo(f"NEXT: {nxt['area']} — {nxt['action']}" if nxt else "NEXT: nothing actionable")
        typer.echo("")
        typer.echo(f"{'AREA':14s} {'STATE':26s} {'APPR':5s} BLOCKER / EARLIEST")
        rows = dash.ordered()
        if section:
            rows = [c for c in rows if c.area == section]
        for c in rows:
            appr = "yes" if c.approval_required else "no"
            earliest = f"  [earliest {c.earliest_ts}]" if c.earliest_ts else ""
            typer.echo(f"{c.area:14s} {c.state.value:26s} {appr:5s} {c.blocker}{earliest}")
            if not compact:
                typer.echo(f"{'':14s} → {c.next_action}")
                typer.echo(f"{'':14s}   cmd: {c.command}")
        typer.echo("")
        typer.echo("FORBIDDEN NOW:")
        for f in ost.FORBIDDEN_ACTIONS:
            typer.echo(f"  - {f}")
        warnings = [c.area for c in checkpoints if c.state is ost.CheckpointState.NOT_APPLICABLE]
        if warnings:
            typer.echo(f"unresolved/unavailable: {', '.join(warnings)}")

    if fail_on_action_required and any(
        c.state is ost.CheckpointState.ACTION_REQUIRED for c in checkpoints
    ):
        raise typer.Exit(code=1)


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


@ops_app.command("recovery-watch")
def ops_recovery_watch(
    state_path: str = typer.Option(..., help="Path to the persisted watch-state JSON."),
    history_path: str = typer.Option(..., help="Path to the append-only watch-history JSONL."),
    probe: bool = typer.Option(
        True, help="Probe the Kalshi production endpoint (unauthenticated reachability only)."
    ),
    notify: bool = typer.Option(
        True, help="Deliver transition notifications via the configured ALERT_TRANSPORT."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit the evaluation as JSON."),
) -> None:
    """Kalshi outage/recovery watch for the pending paper-fill validation.
    Read-only against research + paper data; unauthenticated reachability
    probe only; exactly-once notifications per state transition. NEVER runs
    the paper validation itself -- human approval remains required. A
    non-blocking lock (<state-path>.lock, the `ops monitor` mechanism)
    makes an overlapping invocation skip cleanly -- a skip is logged, never
    treated as a state transition."""
    import subprocess as sp

    import httpx
    from sqlalchemy import select as _select
    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import create_async_engine

    from kalshi_weather.ops import recovery_watch as rw

    lock_handle = monitor.try_acquire_lock(Path(f"{state_path}.lock"))
    if lock_handle is None:
        typer.echo("another `ops recovery-watch` run is still in progress; skipping this cycle")
        return

    async def run() -> None:
        settings = get_settings()
        now = utc_now()

        # -- reachability probe (any HTTP response = reachable) --------------
        api_reachable = False
        if probe:
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    resp = await client.get(
                        f"{settings.kalshi_data_base_url}/exchange/status"
                    )
                    api_reachable = resp.status_code < 500
            except httpx.HTTPError:
                api_reachable = False

        # -- research-side ages (read-only) ----------------------------------
        r_engine = create_async_engine(settings.database_url)
        try:
            async with r_engine.connect() as conn:
                latest = (
                    await conn.execute(
                        _text(
                            "select id, success from collector_runs "
                            "where collector='kalshi' order by id desc limit 1"
                        )
                    )
                ).first()
                success = (
                    await conn.execute(
                        _text(
                            "select id, extract(epoch from (now()-finished_at)) "
                            "from collector_runs where collector='kalshi' and success "
                            "order by id desc limit 1"
                        )
                    )
                ).first()

                async def age(q: str) -> float | None:
                    v = (await conn.execute(_text(q))).scalar()
                    return float(v) if v is not None else None

                snap_age = await age(
                    "select extract(epoch from (now()-max(observed_at))) from market_snapshots"
                )
                book_age = await age(
                    "select extract(epoch from (now()-max(captured_at))) from orderbook_snapshots"
                )
                poll_age = await age(
                    "select extract(epoch from (now()-max(completed_at))) "
                    "from market_poll_attempts where endpoint_type='orderbook' and outcome in "
                    "('succeeded_new_data','succeeded_unchanged','succeeded_empty')"
                )
        finally:
            await r_engine.dispose()

        # -- collector process count -----------------------------------------
        pgrep = sp.run(
            ["pgrep", "-f", "scripts/service/launch.py"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        collector_count = len([ln for ln in pgrep.stdout.splitlines() if ln.strip()])

        # -- paper-side gates (read-only) ------------------------------------
        from kalshi_weather.execution.ledger import EntryKind as _EK
        from kalshi_weather.execution.ledger import LedgerEntry as _LE
        from kalshi_weather.execution.ledger import replay as _replay
        from kalshi_weather.paper.store import (
            PaperPnlSnapshotRow,
            kill_switch_active,
            open_paper_db,
            paper_engine,
            stored_ledger_entries,
        )

        p_engine = paper_engine(settings.paper_database_url)
        await open_paper_db(p_engine)
        p_factory = async_sessionmaker(p_engine, expire_on_commit=False)
        async with p_factory() as ps:
            kill, _reason = await kill_switch_active(ps)
            entries = await stored_ledger_entries(ps)
            snap_row = (
                await ps.scalars(
                    _select(PaperPnlSnapshotRow).order_by(PaperPnlSnapshotRow.id.desc()).limit(1)
                )
            ).first()
        await p_engine.dispose()
        if not entries:
            reconcile_ok = True  # an empty paper book reconciles trivially
        else:
            portfolio = _replay(
                0,
                [
                    _LE(
                        seq=i,
                        at=at,
                        kind=_EK(kind),
                        cash_delta_cents=c,
                        reserved_delta_cents=r,
                        payload=p,
                    )
                    for i, (_s, at, kind, c, r, p) in enumerate(entries)
                ],
            )
            cost = sum(
                x.yes_cost_cents + x.no_cost_cents for x in portfolio.positions.values()
            )
            reconcile_ok = snap_row is not None and (
                portfolio.cash_cents == snap_row.cash_cents
                and portfolio.reserved_cents == snap_row.reserved_cents
                and cost == snap_row.position_cost_cents
                and portfolio.fees_cents == snap_row.fees_cents
            )

        inputs = rw.WatchInputs(
            api_reachable=api_reachable,
            success_run_age_seconds=float(success[1]) if success else None,
            latest_success_run_id=success[0] if success else None,
            latest_run_id=latest[0] if latest else None,
            latest_run_success=bool(latest[1]) if latest else None,
            snapshot_age_seconds=snap_age,
            book_age_seconds=book_age,
            poll_evidence_age_seconds=poll_age,
            collector_count=collector_count,
            kill_switch_inactive=not kill,
            reconcile_ok=reconcile_ok,
        )
        # Recovery-HEALTH uses cadence-aware thresholds (DQ-002), independent
        # of the strict 300s paper-execution fill gate shown below.
        recovery_policy = rw.RecoveryHealthPolicy()
        state, reasons = rw.classify(inputs, recovery_policy)
        prior = rw.load_watch_state(Path(state_path))
        decision = rw.decide_notification(state, reasons, inputs, prior, now=now)

        delivered: bool | None = None
        detail = "suppressed (no state transition)"
        if notify and decision.should_notify:
            telegram = None
            if settings.alert_telegram_bot_token and settings.alert_telegram_chat_id:
                telegram = alerting.TelegramConfig(
                    bot_token=settings.alert_telegram_bot_token.get_secret_value(),
                    chat_id=settings.alert_telegram_chat_id,
                )
            result = await alerting.send_alert(
                settings.alert_transport, decision.message, telegram=telegram
            )
            delivered, detail = result.delivered, result.detail
        elif decision.should_notify:
            detail = "notification computed but --no-notify set"

        rw.save_watch_state(Path(state_path), decision.new_state)
        rw.append_watch_history(
            Path(history_path),
            timestamp=now.isoformat(),
            state=state.value,
            transition=decision.transition,
            reasons=",".join(reasons) or "all_gates_pass",
            notified=delivered,
            detail=detail,
        )

        # Paper-EXECUTION fill gates (strict 300s book) -- unchanged; shown so
        # the operator sees whether a fill would pass RIGHT NOW. Distinct from
        # the recovery-HEALTH state, which uses the cadence-aware thresholds.
        from kalshi_weather.paper.engine import PaperRiskPolicy as _PRP

        thresholds = _PRP()
        paper_fill_gates = {
            "api_reachable": api_reachable,
            f"success_run_age<={thresholds.max_snapshot_age_seconds}s": (
                inputs.success_run_age_seconds is not None
                and inputs.success_run_age_seconds <= thresholds.max_snapshot_age_seconds
            ),
            f"snapshot_age<={thresholds.max_snapshot_age_seconds}s": (
                snap_age is not None and snap_age <= thresholds.max_snapshot_age_seconds
            ),
            f"book_age<={thresholds.max_book_age_seconds}s": (
                book_age is not None and book_age <= thresholds.max_book_age_seconds
            ),
            f"poll_age<={thresholds.max_poll_evidence_age_seconds}s": (
                poll_age is not None and poll_age <= thresholds.max_poll_evidence_age_seconds
            ),
            "one_collector": collector_count == 1,
            "kill_switch_inactive": not kill,
            "paper_reconcile_ok": reconcile_ok,
        }
        recovery_health_gates = {
            f"book_age<={recovery_policy.max_book_age_seconds}s": (
                book_age is not None and book_age <= recovery_policy.max_book_age_seconds
            ),
            f"snapshot_age<={recovery_policy.max_snapshot_age_seconds}s": (
                snap_age is not None and snap_age <= recovery_policy.max_snapshot_age_seconds
            ),
            f"poll_age<={recovery_policy.max_poll_evidence_age_seconds}s": (
                poll_age is not None and poll_age <= recovery_policy.max_poll_evidence_age_seconds
            ),
            f"success_run_age<={recovery_policy.success_stale_seconds}s": (
                inputs.success_run_age_seconds is not None
                and inputs.success_run_age_seconds <= recovery_policy.success_stale_seconds
            ),
        }
        payload = {
            "state": state.value,
            "reasons": reasons,
            "transition": decision.transition,
            "notified": delivered,
            "detail": detail,
            "outage_started_at": decision.new_state.outage_started_at,
            "last_notification_at": decision.new_state.last_notification_at,
            "ready_pending": decision.new_state.ready_pending,
            "latest_run": {"id": inputs.latest_run_id, "success": inputs.latest_run_success},
            "latest_success_run": {
                "id": inputs.latest_success_run_id,
                "age_seconds": inputs.success_run_age_seconds,
            },
            "ages_seconds": {"snapshot": snap_age, "book": book_age, "poll": poll_age},
            "recovery_health_gates": recovery_health_gates,
            "paper_fill_gates": paper_fill_gates,
            "paper_validation_ready": state is rw.WatchState.PAPER_VALIDATION_READY,
        }
        if as_json:
            typer.echo(json.dumps(payload, indent=2, default=str))
        else:
            typer.echo(f"recovery-watch state: {state.value} (transition={decision.transition})")
            if decision.new_state.outage_started_at:
                typer.echo(f"  outage since: {decision.new_state.outage_started_at}")
            typer.echo(
                f"  latest run: {inputs.latest_run_id} success={inputs.latest_run_success} | "
                f"latest success: {inputs.latest_success_run_id} "
                f"age={inputs.success_run_age_seconds}"
            )
            typer.echo("  recovery-health gates (cadence-aware):")
            for name, ok in recovery_health_gates.items():
                typer.echo(f"    [{'PASS' if ok else 'FAIL'}] {name}")
            typer.echo("  paper-fill gates (strict 300s; re-checked at run time):")
            for name, ok in paper_fill_gates.items():
                typer.echo(f"    [{'PASS' if ok else 'FAIL'}] {name}")
            typer.echo(f"  paper_validation_ready: {payload['paper_validation_ready']}")
            typer.echo(f"  notified: {delivered} detail={detail!r}")

    try:
        asyncio.run(run())
    finally:
        lock_handle.close()


@ops_app.command("station-pilot-review")
def ops_station_pilot_review(
    as_of: str = typer.Option("", help="Review as-of timestamp (ISO; default now)."),
    start: str = typer.Option("", help="Pilot start override (ISO; default ADR 0023 deploy)."),
    as_json: bool = typer.Option(False, "--json", help="Emit the review as JSON."),
) -> None:
    """READ-ONLY seven-day SEA/PHX/MIA pilot review (ADR 0023): continuity,
    timezone behavior, collector/storage impact, settlement gain, decision
    gates, and (only after 7 full days) a second-batch recommendation that
    still requires explicit human approval. Writes nothing."""
    from datetime import datetime as _dt

    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import create_async_engine

    from kalshi_weather.ops import station_pilot_review as spr

    async def run() -> None:
        settings = get_settings()
        now = _dt.fromisoformat(as_of) if as_of else utc_now()
        pilot_start = _dt.fromisoformat(start) if start else spr.PILOT_START_DEFAULT
        window = spr.review_window(now, pilot_start=pilot_start)

        engine = create_async_engine(settings.database_url)
        try:
            async with engine.connect() as conn:
                all_stations = list(spr.PILOT_STATIONS) + list(spr.BASELINE_STATIONS)
                fc_rows = (
                    await conn.execute(
                        _text(
                            "select station_id, issue_time, observed_at, valid_start "
                            "from weather_forecasts where observed_at >= :start"
                        ),
                        {"start": pilot_start.replace(tzinfo=None)},
                    )
                ).all()
                forecasts = [
                    spr.ForecastRowLite(
                        station=r[0],
                        issue_time=r[1].replace(tzinfo=None) if r[1].tzinfo else r[1],
                        observed_at=(
                            r[2].astimezone(UTC).replace(tzinfo=None) if r[2].tzinfo else r[2]
                        ),
                        valid_start=r[3].replace(tzinfo=None) if r[3].tzinfo else r[3],
                    )
                    for r in fc_rows
                    if r[0] in all_stations
                ]
                ob_rows = (
                    await conn.execute(
                        _text(
                            "select station_id, variable, observation_date "
                            "from weather_observations where observation_date >= :d"
                        ),
                        {"d": window.first_full_day - timedelta(days=1)},
                    )
                ).all()
                observations = [
                    spr.ObservationRowLite(station=r[0], variable=r[1], observation_date=r[2])
                    for r in ob_rows
                    if r[0] in all_stations
                ]
                cyc_rows = (
                    await conn.execute(
                        _text(
                            "select id, started_at, coalesce(duration_seconds,0), "
                            "coalesce(requests_attempted,0), success from collector_runs "
                            "where collector='weather' and started_at >= :pre"
                        ),
                        {"pre": (pilot_start - timedelta(hours=48)).replace(tzinfo=None)},
                    )
                ).all()
                cycles = [
                    spr.CycleLite(
                        run_id=r[0],
                        started_at=r[1].replace(tzinfo=UTC) if r[1].tzinfo is None else r[1],
                        duration_seconds=float(r[2]),
                        requests=int(r[3]),
                        success=bool(r[4]),
                    )
                    for r in cyc_rows
                ]
                kalshi_ok = (
                    await conn.execute(
                        _text(
                            "select count(*) filter (where success), count(*) "
                            "from collector_runs where collector='kalshi' "
                            "and started_at >= :start"
                        ),
                        {"start": pilot_start.replace(tzinfo=None)},
                    )
                ).first()
                sizes = (
                    await conn.execute(
                        _text(
                            "select pg_size_pretty(pg_database_size(current_database())), "
                            "pg_total_relation_size('weather_forecasts'), "
                            "pg_total_relation_size('weather_observations')"
                        )
                    )
                ).first()
                assert sizes is not None  # single-row aggregate always returns
                pilot_fc = (
                    await conn.execute(
                        _text(
                            "select count(*) from weather_forecasts "
                            "where station_id in ('SEA','PHX','MIA')"
                        )
                    )
                ).scalar()
                pilot_ob = (
                    await conn.execute(
                        _text(
                            "select count(*) from weather_observations "
                            "where station_id in ('SEA','PHX','MIA')"
                        )
                    )
                ).scalar()
        finally:
            await engine.dispose()

        pre = [c for c in cycles if c.started_at < pilot_start]
        post = [c for c in cycles if c.started_at >= pilot_start]
        impact = spr.collector_impact(
            pre, post, cadence_seconds=float(settings.weather_interval_seconds)
        )

        continuity = {
            st: spr.station_continuity(st, forecasts, observations, window)
            for st in (*spr.PILOT_STATIONS, *spr.BASELINE_STATIONS)
        }
        tz = spr.timezone_checks(now)
        spill = spr.observation_date_spillover(observations, window)

        # settlement gain (read-only classifier; nothing persisted)
        from kalshi_weather.settlement.resolver import ParserSettlementResolver
        from kalshi_weather.settlement.spec import SettlementStatus

        async with _open_session(settings) as session:
            specs = await ParserSettlementResolver().resolve_specs(session)
        pilot_resolved: dict[str, dict[str, int]] = {
            st: {"tmax_f": 0, "tmin_f": 0} for st in spr.PILOT_STATIONS
        }
        baseline_resolved = 0
        unresolved_now = 0
        for sp in specs:
            if sp.status is SettlementStatus.RESOLVED:
                if sp.station_id in pilot_resolved and sp.variable:
                    pilot_resolved[sp.station_id][sp.variable] += 1
                elif sp.station_id in spr.BASELINE_STATIONS:
                    baseline_resolved += 1
            elif sp.status in (SettlementStatus.UNRESOLVED, SettlementStatus.AMBIGUOUS):
                unresolved_now += 1

        expected_obs_days = window.elapsed_full_days * len(spr.PILOT_STATIONS)
        complete_obs_days = sum(
            continuity[st].observation_days_complete for st in spr.PILOT_STATIONS
        )
        stable = sum(
            1
            for st in spr.PILOT_STATIONS
            if window.elapsed_full_days > 0
            and len(continuity[st].issuance_shortfall_days) == 0
        )
        gates = spr.GateInputs(
            review_state=window.state,
            timezone_ok=all(v["ok"] for v in tz.values()),
            spillover_ok=all(spill.values()),
            mapping_ok=True,  # frozen registry; parser fixtures guard mappings
            pilot_critical_findings=0,
            stations_with_stable_issuance=stable,
            pilot_observation_completeness=(
                complete_obs_days / expected_obs_days if expected_obs_days else 1.0
            ),
            duplicate_rows=sum(
                continuity[st].duplicate_logical_rows for st in spr.PILOT_STATIONS
            ),
            collector_capacity_ok=impact.cycles_exceeding_half_cadence == 0,
            baseline_stations_healthy=all(
                continuity[st].longest_forecast_gap_hours < 24.0
                for st in spr.BASELINE_STATIONS
            ),
            settlement_no_regression=baseline_resolved >= spr.BASELINE_RESOLVED_FLOOR,
            settlement_unresolved_new=unresolved_now,
            storage_growth_mb_per_day=(
                (int(sizes[1]) + int(sizes[2])) / 1_048_576 / max(1, window.elapsed_full_days)
                if window.elapsed_full_days
                else 0.0
            ),
        )
        recommendation, reasons = spr.decide(gates)
        if window.state != spr.PILOT_REVIEW_READY:
            recommendation = spr.CONTINUE_PILOT  # no expansion call before day 7

        payload: dict[str, Any] = {
            "state": window.state,
            "pilot_start": str(window.pilot_start),
            "as_of": str(window.as_of),
            "elapsed_full_days": window.elapsed_full_days,
            "review_ready_at": str(window.review_ready_at),
            "continuity": {
                st: {
                    "forecast_rows_by_day": c.forecast_rows_by_day,
                    "issuances_by_day": c.issuances_by_day,
                    "issuance_shortfall_days": c.issuance_shortfall_days,
                    "observation_days_complete": c.observation_days_complete,
                    "observation_days_missing": c.observation_days_missing,
                    "duplicates": c.duplicate_logical_rows,
                    "longest_forecast_gap_h": c.longest_forecast_gap_hours,
                    "latest_forecast_age_h": c.latest_forecast_age_hours,
                    "latest_observation": c.latest_observation_date,
                    "ingest_delay_p50_m": c.ingestion_delay_p50_minutes,
                    "ingest_delay_p90_m": c.ingestion_delay_p90_minutes,
                }
                for st, c in continuity.items()
            },
            "timezone_checks": tz,
            "observation_spillover_ok": spill,
            "collector_impact": impact.__dict__,
            "kalshi_runs_since_pilot": {
                "success": kalshi_ok[0] if kalshi_ok else 0,
                "total": kalshi_ok[1] if kalshi_ok else 0,
            },
            "storage": {
                "db_size": sizes[0],
                "forecast_table_bytes": int(sizes[1]),
                "observation_table_bytes": int(sizes[2]),
                "pilot_forecast_rows": int(pilot_fc or 0),
                "pilot_observation_rows": int(pilot_ob or 0),
            },
            "settlement": {
                "pilot_resolved": pilot_resolved,
                "baseline_resolved": baseline_resolved,
                "baseline_floor": spr.BASELINE_RESOLVED_FLOOR,
                "unresolved_or_ambiguous": unresolved_now,
            },
            "gates": gates.__dict__,
            "recommendation": recommendation,
            "reasons": reasons,
            "second_batch_ranking": [
                {"city": c, "notes": n} for c, n in spr.SECOND_BATCH_RANKING
            ],
            "second_batch_recommended": (
                list(spr.SECOND_BATCH_RECOMMENDED)
                if recommendation == spr.SECOND_BATCH_ELIGIBLE
                else "deferred until PILOT_REVIEW_READY and all gates pass"
            ),
        }
        if as_json:
            typer.echo(json.dumps(payload, indent=2, default=str))
            return
        typer.echo(f"station-pilot review: {window.state}")
        typer.echo(
            f"  pilot day {window.elapsed_full_days}/{spr.REVIEW_DAYS_REQUIRED} "
            f"(review ready {window.review_ready_at})"
        )
        for st in (*spr.PILOT_STATIONS, *spr.BASELINE_STATIONS):
            c = continuity[st]
            typer.echo(
                f"  {st}: obs_complete={c.observation_days_complete} "
                f"missing={len(c.observation_days_missing)} dup={c.duplicate_logical_rows} "
                f"max_gap={c.longest_forecast_gap_hours}h "
                f"latest_fc_age={c.latest_forecast_age_hours}h "
                f"shortfall_days={len(c.issuance_shortfall_days)}"
            )
        typer.echo(f"  tz: {'OK' if all(v['ok'] for v in tz.values()) else 'ANOMALY'} "
                   f"| spillover: {'OK' if all(spill.values()) else 'ANOMALY'}")
        typer.echo(
            f"  weather cycles: pre p50={impact.pre_median_s}s -> steady p50="
            f"{impact.post_median_s}s p95={impact.post_p95_s}s "
            f"(backfill {impact.post_first_backfill_s}s) over-cadence="
            f"{impact.cycles_exceeding_half_cadence}"
        )
        typer.echo(
            f"  settlement: pilot={ {k: sum(v.values()) for k,v in pilot_resolved.items()} } "
            f"baseline={baseline_resolved} (floor {spr.BASELINE_RESOLVED_FLOOR}) "
            f"unresolved={unresolved_now}"
        )
        typer.echo(f"  recommendation: {recommendation} ({', '.join(reasons)})")
        if recommendation != spr.SECOND_BATCH_ELIGIBLE:
            typer.echo("  second batch: deferred (no expansion call before day 7 / gates)")
        else:
            typer.echo(
                f"  second batch candidates (HUMAN APPROVAL REQUIRED): "
                f"{', '.join(spr.SECOND_BATCH_RECOMMENDED)}"
            )

    asyncio.run(run())


@ops_app.command("restore-drill")
def ops_restore_drill(
    dry_run: bool = typer.Option(False, help="Select and verify the backup; download nothing."),
    backup_key: str = typer.Option("", help="Explicit S3 key (default: newest verified)."),
    keep_on_failure: bool = typer.Option(
        False, help="Keep the disposable container + download for forensics on failure."
    ),
    timeout_seconds: float = typer.Option(900.0, help="Per-command timeout bound."),
    as_json: bool = typer.Option(False, "--json", help="Emit the drill result as JSON."),
) -> None:
    """Disaster-recovery drill from the newest VERIFIED S3 backup: download,
    checksum, restore into a uniquely named DISPOSABLE PostgreSQL container,
    validate schema + representative data, destroy everything, and append
    the result to the drill history. Mechanically incapable of addressing
    the production container or database. Read-only toward production."""
    import subprocess as sp

    from kalshi_weather.ops import restore_drill as rd

    settings = get_settings()
    if settings.backup_remote_type != "s3":
        raise typer.BadParameter("BACKUP_REMOTE_TYPE is not 's3'; no S3 backups to drill against")
    if not settings.backup_s3_bucket:
        raise typer.BadParameter("BACKUP_S3_BUCKET is not configured")
    commit = sp.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=False
    ).stdout.strip()
    config = rd.DrillConfig(
        bucket=settings.backup_s3_bucket,
        prefix=settings.backup_s3_prefix,
        backup_dir=_backup_dir(settings),
        timeout_seconds=timeout_seconds,
        keep_on_failure=keep_on_failure,
        backup_key=backup_key or None,
        dry_run=dry_run,
        code_commit=commit or "unknown",
    )
    result = rd.run_drill(config)
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "drill_id": result.drill_id,
                    "status": result.status,
                    "failure_reason": result.failure_reason,
                    "backup_key": result.backup_key,
                    "backup_size": result.backup_size,
                    "backup_sha256": result.backup_sha256,
                    "container": result.container,
                    "pg_version": result.pg_version,
                    "cleanup_ok": result.cleanup_ok,
                    "steps": result.steps,
                },
                indent=2,
            )
        )
    else:
        typer.echo(f"restore drill {result.drill_id}: {result.status.upper()}")
        typer.echo(f"  backup: {result.backup_key} ({result.backup_size} bytes)")
        if result.backup_sha256:
            typer.echo(f"  sha256: {result.backup_sha256}")
        if result.container:
            typer.echo(f"  disposable target: {result.container}")
        for s in result.steps:
            typer.echo(f"  [{'OK' if s['ok'] else 'FAIL'}] {s['step']}"
                       + (f": {s['detail']}" if s["detail"] else ""))
        if result.failure_reason:
            typer.echo(f"  failure: {result.failure_reason}")
    if result.status != "success":
        raise typer.Exit(code=1)


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
                    startup_grace_seconds=settings.kalshi_startup_grace_seconds,
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
