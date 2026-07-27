"""Immutable export of historical replay data, and running a history replay.

The export writes Parquet for the (potentially large) event tables and JSON for
manifests, hashing the CANONICAL content (sorted JSON) of each event list so
determinism is verified independently of Parquet's internal byte layout. An
export directory is immutable -- it is never overwritten.

Nothing here writes to the source database, submits orders, or reads H0019.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from kalshi_weather.execution.coverage import (
    CoverageResults,
    FillConfidence,
    ReplayConfidence,
    ReplayCoveragePolicy,
    fill_confidence,
)
from kalshi_weather.execution.engine import Simulator
from kalshi_weather.execution.history import HistoricalReplayData, HistoryQuery
from kalshi_weather.execution.market import MarketData, OrderBook, Trade
from kalshi_weather.execution.models import OrderIntent, Side
from kalshi_weather.execution.policy import ExecutionPolicy, RiskConfig
from kalshi_weather.execution.replay import (
    MarketStatusEvent,
    ReplayConfig,
    SettlementEvent,
    _git_commit,
    _latest_book_before,
    run_replay,
    write_artifacts,
)

HISTORICAL_BANNER = "SIMULATION ONLY -- HISTORICAL REPLAY -- NO EXCHANGE ORDERS WILL BE SUBMITTED"


class InsufficientCoverageError(RuntimeError):
    """Raised when a required coverage gate fails and no unsafe override is set."""


# --- canonical (deterministic) serialization -------------------------------


def _books_rows(books: list[OrderBook]) -> list[dict[str, Any]]:
    return [
        {
            "ticker": b.ticker,
            "captured_at": b.captured_at.isoformat(),
            "yes_bids": json.dumps([list(x) for x in b.yes_bids]),
            "yes_asks": json.dumps([list(x) for x in b.yes_asks]),
            "source_ref": b.source_ref,
        }
        for b in sorted(books, key=lambda b: (b.captured_at, b.ticker, b.source_ref))
    ]


def _trades_rows(trades: list[Trade]) -> list[dict[str, Any]]:
    return [
        {
            "ticker": t.ticker,
            "executed_at": t.executed_at.isoformat(),
            "yes_price_cents": t.yes_price_cents,
            "quantity": t.quantity,
            "taker_side": t.taker_side.value if t.taker_side is not None else None,
            "source_ref": t.source_ref,
        }
        for t in sorted(trades, key=lambda t: (t.executed_at, t.source_ref))
    ]


def _status_rows(status: list[MarketStatusEvent]) -> list[dict[str, Any]]:
    return [
        {"ticker": s.ticker, "state": s.state, "at": s.at.isoformat(), "source_ref": s.source_ref}
        for s in sorted(status, key=lambda s: (s.at, s.ticker, s.source_ref))
    ]


def _settlement_rows(settlements: list[SettlementEvent]) -> list[dict[str, Any]]:
    return [
        {"ticker": s.ticker, "result": s.result.value, "available_at": s.available_at.isoformat()}
        for s in sorted(settlements, key=lambda s: (s.available_at, s.ticker))
    ]


def _content_hash(rows: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


# --- export ----------------------------------------------------------------


def export_replay_data(
    query: HistoryQuery,
    data: HistoricalReplayData,
    out_dir: Path,
    *,
    source_db_revision: str | None = None,
) -> dict[str, Any]:
    """Write an immutable export directory. Refuses to overwrite an existing one.
    Event tables are Parquet; manifests/quality/settlements are JSON. Hashes are
    over canonical content, so re-exporting identical data yields identical
    hashes regardless of Parquet byte layout."""
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"export directory {out_dir} already exists; refusing to overwrite")
    out_dir.mkdir(parents=True, exist_ok=True)

    books = _books_rows(data.market_data.order_books)
    trades = _trades_rows(data.market_data.trades)
    status = _status_rows(data.market_status)
    settlements = _settlement_rows(data.settlements)

    # Parquet for the event tables (empty-safe: write a schema'd empty frame).
    _write_parquet(out_dir / "order_books.parquet", books, _BOOK_SCHEMA)
    _write_parquet(out_dir / "trades.parquet", trades, _TRADE_SCHEMA)
    _write_parquet(out_dir / "market_status.parquet", status, _STATUS_SCHEMA)

    hashes = {
        "order_books": _content_hash(books),
        "trades": _content_hash(trades),
        "market_status": _content_hash(status),
        "settlements": _content_hash(settlements),
    }

    def dump(name: str, obj: Any) -> None:
        (out_dir / name).write_text(json.dumps(obj, indent=2, sort_keys=True, default=str))

    dump("settlements.json", settlements)
    dump("quality_report.json", data.quality.to_manifest())
    manifest: dict[str, Any] = {
        "banner": HISTORICAL_BANNER,
        "export_id": f"hist-{query.start.isoformat()}_{query.end.isoformat()}",
        "git_commit": _git_commit(),
        "source_db_revision": source_db_revision,
        "query": query.to_manifest(),
        "counts": data.counts.to_manifest(),
        "content_hashes": hashes,
        "h0019_isolation": "no H0019 artifact is read or written by this export",
    }
    dump("export_manifest.json", manifest)
    return manifest


_BOOK_SCHEMA = {
    "ticker": pl.Utf8,
    "captured_at": pl.Utf8,
    "yes_bids": pl.Utf8,
    "yes_asks": pl.Utf8,
    "source_ref": pl.Utf8,
}
_TRADE_SCHEMA = {
    "ticker": pl.Utf8,
    "executed_at": pl.Utf8,
    "yes_price_cents": pl.Int64,
    "quantity": pl.Int64,
    "taker_side": pl.Utf8,
    "source_ref": pl.Utf8,
}
_STATUS_SCHEMA = {"ticker": pl.Utf8, "state": pl.Utf8, "at": pl.Utf8, "source_ref": pl.Utf8}


def _write_parquet(path: Path, rows: list[dict[str, Any]], schema: dict[str, Any]) -> None:
    frame = pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)
    frame.write_parquet(path)


# --- import ----------------------------------------------------------------


@dataclass
class LoadedExport:
    market_data: MarketData
    settlements: list[SettlementEvent]
    market_status: list[MarketStatusEvent]
    manifest: dict[str, Any]
    quality_report: dict[str, Any] = field(default_factory=dict)

    def tickers(self) -> tuple[str, ...]:
        return tuple(self.manifest.get("query", {}).get("tickers", []))

    def as_historical_data(self) -> HistoricalReplayData:
        """Rebuild a HistoricalReplayData (for coverage evaluation) from the export,
        carrying the exported per-market quality counters."""
        from kalshi_weather.execution.history import ExclusionCounts, QualityReport

        q = QualityReport()
        q.per_market = self.quality_report.get("per_market", {})
        return HistoricalReplayData(
            market_data=self.market_data,
            settlements=self.settlements,
            market_status=self.market_status,
            counts=ExclusionCounts(),
            quality=q,
        )


def _dt(v: str) -> datetime:
    return datetime.fromisoformat(v)


def load_exported_data(export_dir: Path) -> LoadedExport:
    """Reconstruct replay events from an export directory (read-only)."""
    manifest = json.loads((export_dir / "export_manifest.json").read_text())
    books_df = pl.read_parquet(export_dir / "order_books.parquet")
    trades_df = pl.read_parquet(export_dir / "trades.parquet")
    status_df = pl.read_parquet(export_dir / "market_status.parquet")

    books = [
        OrderBook(
            ticker=r["ticker"],
            captured_at=_dt(r["captured_at"]),
            yes_bids=tuple((int(p), int(q)) for p, q in json.loads(r["yes_bids"])),
            yes_asks=tuple((int(p), int(q)) for p, q in json.loads(r["yes_asks"])),
            source_ref=r["source_ref"],
        )
        for r in books_df.iter_rows(named=True)
    ]
    trades = [
        Trade(
            ticker=r["ticker"],
            executed_at=_dt(r["executed_at"]),
            yes_price_cents=int(r["yes_price_cents"]),
            quantity=int(r["quantity"]),
            taker_side=Side(r["taker_side"]) if r["taker_side"] else None,
            source_ref=r["source_ref"],
        )
        for r in trades_df.iter_rows(named=True)
    ]
    status = [
        MarketStatusEvent(
            ticker=r["ticker"], state=r["state"], at=_dt(r["at"]), source_ref=r["source_ref"]
        )
        for r in status_df.iter_rows(named=True)
    ]
    settlements = [
        SettlementEvent(
            ticker=s["ticker"], result=Side(s["result"]), available_at=_dt(s["available_at"])
        )
        for s in json.loads((export_dir / "settlements.json").read_text())
    ]
    quality_path = export_dir / "quality_report.json"
    quality_report = json.loads(quality_path.read_text()) if quality_path.exists() else {}
    return LoadedExport(
        market_data=MarketData(order_books=books, trades=trades),
        settlements=settlements,
        market_status=status,
        manifest=manifest,
        quality_report=quality_report,
    )


# --- run a history replay --------------------------------------------------


def load_order_intents(path: Path) -> list[OrderIntent]:
    """Parse a JSON file of explicit external/synthetic order intents."""
    from kalshi_weather.execution.models import Action, OrderType, TimeInForce

    raw = json.loads(path.read_text())
    intents: list[OrderIntent] = []
    for o in raw:
        intents.append(
            OrderIntent(
                order_id=o["order_id"],
                strategy_id=o.get("strategy_id", "external"),
                ticker=o["ticker"],
                side=Side(o["side"]),
                action=Action(o["action"]),
                order_type=OrderType(o.get("order_type", "marketable_limit")),
                limit_price_cents=int(o["limit_price_cents"]),
                quantity=int(o["quantity"]),
                submitted_at=_dt(o["submitted_at"]),
                time_in_force=TimeInForce(o.get("time_in_force", "ioc")),
                expires_at=_dt(o["expires_at"]) if o.get("expires_at") else None,
                model_version=o.get("model_version"),
            )
        )
    return intents


def load_exec_config(path: Path) -> tuple[int, ExecutionPolicy, RiskConfig, dict[str, int]]:
    """Parse initial cash, execution policy, and risk config from a JSON file
    (reusing the standard run-config parsing; market_data/orders ignored)."""
    from kalshi_weather.execution.loader import _build_fee_model
    from kalshi_weather.execution.policy import FillMode

    raw = json.loads(path.read_text())
    pol_raw = raw.get("policy", {})
    policy = ExecutionPolicy(
        version=pol_raw.get("version", "exec-policy-v1"),
        fill_mode=FillMode(pol_raw.get("fill_mode", "marketable")),
        fee_model=_build_fee_model(pol_raw.get("fee_model", {})),
        queue_ahead_contracts=int(pol_raw.get("queue_ahead_contracts", 0)),
        max_book_age_seconds=float(pol_raw.get("max_book_age_seconds", 300.0)),
        max_spread_cents=int(pol_raw.get("max_spread_cents", 100)),
        random_seed=int(pol_raw.get("random_seed", 0)),
    )
    risk = RiskConfig(
        **{k: v for k, v in raw.get("risk", {}).items() if k in RiskConfig.__dataclass_fields__}
    )
    marks = {k: int(v) for k, v in raw.get("marks", {}).items()}
    return int(raw.get("initial_cash_cents", 0)), policy, risk, marks


def build_history_replay_config(
    *,
    run_id: str,
    initial_cash_cents: int,
    policy: ExecutionPolicy,
    risk: RiskConfig,
    loaded: LoadedExport,
    order_intents: list[OrderIntent],
    marks: dict[str, int] | None = None,
) -> ReplayConfig:
    return ReplayConfig(
        run_id=run_id,
        initial_cash_cents=initial_cash_cents,
        policy=policy,
        risk=risk,
        market_data=loaded.market_data,
        order_intents=order_intents,
        settlements=loaded.settlements,
        marks=marks or {},
        market_status=loaded.market_status,
    )


def load_coverage_policy(path: Path | None) -> ReplayCoveragePolicy:
    """Load a coverage policy from JSON, or the default policy when no path."""
    if path is None:
        return ReplayCoveragePolicy()
    raw = json.loads(path.read_text())
    fields = ReplayCoveragePolicy.__dataclass_fields__
    return ReplayCoveragePolicy(**{k: v for k, v in raw.items() if k in fields})


def enforce_coverage(coverage: CoverageResults, *, allow_insufficient: bool) -> None:
    """Pre-run gate: refuse to proceed on INSUFFICIENT_DATA unless the caller
    explicitly overrides. Never downgrades a FAIL into a WARNING."""
    if coverage.verdict is ReplayConfidence.INSUFFICIENT_DATA and not allow_insufficient:
        failed = ", ".join(g.name for g in coverage.failed_gates())
        raise InsufficientCoverageError(
            f"coverage verdict INSUFFICIENT_DATA (failed required gates: {failed}); "
            f"pass an explicit unsafe override to run anyway"
        )


def _is_passive(config: ReplayConfig, order_type_value: str) -> bool:
    return order_type_value == "passive_limit" or config.policy.fill_mode.value == "passive"


def compute_order_confidence(
    sim: Simulator,
    config: ReplayConfig,
    coverage: CoverageResults,
    *,
    unsafe_override: bool,
) -> list[dict[str, Any]]:
    """Per-order/per-fill execution confidence (Phase 7). Not derived from ledger
    reconciliation -- only from execution-data quality."""
    by_ticker = {m.ticker: m for m in coverage.markets}
    books = list(config.market_data.order_books)
    trades = list(config.market_data.trades)
    rows: list[dict[str, Any]] = []
    filled_orders = {f.order_id for f in sim.fills}
    for o in sim.orders:
        oi = o.intent
        book = _latest_book_before(books, oi.ticker, oi.submitted_at)
        book_age = (oi.submitted_at - book.captured_at).total_seconds() if book else None
        trades_after = sum(
            1 for t in trades if t.ticker == oi.ticker and t.executed_at > oi.submitted_at
        )
        passive = _is_passive(config, oi.order_type.value)
        mq = by_ticker.get(oi.ticker)
        if o.intent.order_id in filled_orders:
            conf = fill_confidence(
                fill_mode=config.policy.fill_mode.value,
                is_passive=passive,
                book_age_seconds=book_age,
                market=mq,
                policy=coverage.policy,
                unsafe_override=unsafe_override,
            )
        else:
            conf = FillConfidence.UNSUPPORTED  # no fill produced
        warnings: list[str] = list(mq.reasons) if mq else []
        rows.append(
            {
                "order_id": oi.order_id,
                "ticker": oi.ticker,
                "state": o.state.value,
                "filled_quantity": o.filled_quantity,
                "reject_reason": o.reject_reason,
                "coverage_policy_version": coverage.policy.version,
                "market_confidence": mq.confidence.value if mq else "unknown",
                "book_age_seconds": round(book_age, 2) if book_age is not None else None,
                "trades_after_submission": trades_after,
                "fill_confidence": conf.value,
                "warnings": "; ".join(warnings),
                "unsafe_coverage_override": unsafe_override,
            }
        )
    return rows


def run_history_replay(config: ReplayConfig) -> Simulator:
    """Run a replay over historical data with market-status gating (delegates to
    the single deterministic engine)."""
    return run_replay(config)


def _coverage_summary_md(
    coverage: CoverageResults, order_conf: list[dict[str, Any]], unsafe_override: bool
) -> str:
    excluded = [
        m.ticker for m in coverage.markets if m.confidence is ReplayConfidence.INSUFFICIENT_DATA
    ]
    rejected = [r["order_id"] for r in order_conf if r["reject_reason"]]
    low = [r["order_id"] for r in order_conf if r["fill_confidence"] in ("LOW", "UNSUPPORTED")]

    def _gate_lines(gs: list[Any]) -> list[str]:
        return [
            f"- {g.name}: actual {g.actual} vs threshold {g.threshold} ({g.reason})" for g in gs
        ] or ["- none"]

    lines = [
        f"# {HISTORICAL_BANNER}",
        "",
        f"## Overall quality verdict: **{coverage.verdict.value}**",
    ]
    if unsafe_override:
        lines += [
            "",
            "> **UNSAFE_COVERAGE_OVERRIDE ACTIVE** -- a required gate failed; results",
            "> are NOT a high-fidelity execution estimate.",
        ]
    lines += [
        "",
        "### Failed gates",
        *_gate_lines(coverage.failed_gates()),
        "",
        "### Warning gates",
        *_gate_lines(coverage.warning_gates()),
        "",
        f"### Markets excluded (insufficient data): {excluded or 'none'}",
        f"### Orders rejected due to coverage/state: {rejected or 'none'}",
        f"### Orders/fills carrying low or unsupported confidence: {low or 'none'}",
        f"### Unsafe override used: {unsafe_override}",
        "",
        "Note: a deterministic, reconciled replay can still be a poor representation of",
        "real execution when source market data is sparse. Accounting correctness and",
        "execution-data quality are separate.",
        "",
    ]
    return "\n".join(lines)


def write_history_artifacts(
    config: ReplayConfig,
    sim: Simulator,
    loaded: LoadedExport,
    out_dir: Path,
    *,
    coverage: CoverageResults | None = None,
    order_confidence: list[dict[str, Any]] | None = None,
    unsafe_override: bool = False,
) -> dict[str, Any]:
    """Write the standard run artifacts plus historical provenance and (when a
    coverage policy was applied) the coverage/confidence artifacts, hashing each."""
    manifest = write_artifacts(config, sim, out_dir)
    (out_dir / "history_source.json").write_text(
        json.dumps(
            {"banner": HISTORICAL_BANNER, "export_manifest": loaded.manifest},
            indent=2,
            sort_keys=True,
            default=str,
        )
    )
    if coverage is None:
        return manifest
    order_confidence = order_confidence or []
    extra_hashes: dict[str, str] = {}

    def _dump_json(name: str, obj: Any) -> None:
        text = json.dumps(obj, indent=2, sort_keys=True, default=str)
        (out_dir / name).write_text(text)
        extra_hashes[name] = hashlib.sha256(text.encode()).hexdigest()

    _dump_json("coverage_policy.json", coverage.policy.to_manifest())
    _dump_json("coverage_results.json", coverage.to_manifest())
    _write_parquet(
        out_dir / "market_quality.parquet",
        [m.to_dict() | {"reasons": "; ".join(m.reasons)} for m in coverage.markets],
        _MARKET_QUALITY_SCHEMA,
    )
    _write_parquet(out_dir / "order_confidence.parquet", order_confidence, _ORDER_CONF_SCHEMA)
    summary = _coverage_summary_md(coverage, order_confidence, unsafe_override)
    (out_dir / "coverage_summary.md").write_text(summary)
    extra_hashes["coverage_summary.md"] = hashlib.sha256(summary.encode()).hexdigest()
    # market_quality/order_confidence content hashes (canonical JSON)
    extra_hashes["market_quality"] = hashlib.sha256(
        json.dumps([m.to_dict() for m in coverage.markets], sort_keys=True).encode()
    ).hexdigest()
    extra_hashes["order_confidence"] = hashlib.sha256(
        json.dumps(order_confidence, sort_keys=True, default=str).encode()
    ).hexdigest()

    manifest["coverage_verdict"] = coverage.verdict.value
    manifest["coverage_policy_version"] = coverage.policy.version
    manifest["unsafe_coverage_override"] = unsafe_override
    manifest["coverage_hashes"] = extra_hashes
    (out_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str)
    )
    return manifest


_MARKET_QUALITY_SCHEMA = {
    "ticker": pl.Utf8,
    "book_count": pl.Int64,
    "trade_count": pl.Int64,
    "has_settlement": pl.Boolean,
    "median_book_age_seconds": pl.Float64,
    "max_book_age_seconds": pl.Float64,
    "max_book_gap_seconds": pl.Float64,
    "max_trade_gap_seconds": pl.Float64,
    "malformed_rows": pl.Int64,
    "conflicts": pl.Int64,
    "production_pct": pl.Float64,
    "marketable_eligible": pl.Boolean,
    "passive_eligible": pl.Boolean,
    "confidence": pl.Utf8,
    "reasons": pl.Utf8,
}
_ORDER_CONF_SCHEMA = {
    "order_id": pl.Utf8,
    "ticker": pl.Utf8,
    "state": pl.Utf8,
    "filled_quantity": pl.Int64,
    "reject_reason": pl.Utf8,
    "coverage_policy_version": pl.Utf8,
    "market_confidence": pl.Utf8,
    "book_age_seconds": pl.Float64,
    "trades_after_submission": pl.Int64,
    "fill_confidence": pl.Utf8,
    "warnings": pl.Utf8,
    "unsafe_coverage_override": pl.Boolean,
}
