"""Deterministic historical/synthetic replay runner + immutable run artifacts.

Event ordering (Phase 10, documented): events are processed in strict
(timestamp, kind_rank, tiebreak_id) order so a run is fully deterministic and no
future event can influence an earlier fill. kind_rank follows the suggested
pipeline: book/price (1) < trade (2) < order intent (3) < settlement (4) <
mark (5). For each order intent we use only the latest book at-or-before its
submission and only trades strictly after it (passive evidence) -- never future
books, never pre-submission trades.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from kalshi_weather.execution import SIMULATION_ONLY_BANNER
from kalshi_weather.execution.engine import Simulator
from kalshi_weather.execution.market import MarketData, OrderBook
from kalshi_weather.execution.metrics import compute_metrics
from kalshi_weather.execution.models import OrderIntent, Side
from kalshi_weather.execution.policy import ExecutionPolicy, RiskConfig

KNOWN_LIMITATIONS = [
    "Fill models are conservative approximations of public historical data; true "
    "queue position and hidden depth are not reconstructable.",
    "Fee model is a configurable placeholder, ZERO by default -- not verified "
    "against Kalshi's live fee schedule.",
    "Passive fills use realized subsequent volume as a proxy; they cannot prove a "
    "real order would have filled.",
    "This is SIMULATION ONLY -- not demo execution and not real execution; do not "
    "read results as tradable-edge evidence.",
]


@dataclass(frozen=True)
class SettlementEvent:
    ticker: str
    result: Side
    available_at: datetime  # settlement is never applied before this instant


@dataclass(frozen=True)
class MarketStatusEvent:
    """A market-state transition. When a ticker has any status events, an order
    submitted while its current state is not ``"open"`` is rejected
    (``market_not_open``) -- orders may not be placed outside valid windows."""

    ticker: str
    state: str  # "open" | "closed" | "determined" | "finalized" | ...
    at: datetime
    source_ref: str = "synthetic"


@dataclass
class ReplayConfig:
    run_id: str
    initial_cash_cents: int
    policy: ExecutionPolicy
    risk: RiskConfig
    market_data: MarketData
    order_intents: list[OrderIntent] = field(default_factory=list)
    settlements: list[SettlementEvent] = field(default_factory=list)
    marks: dict[str, int] = field(default_factory=dict)  # ticker -> yes mark cents
    #: Optional market-state transitions. When present for a ticker, orders are
    #: gated to its OPEN window.
    market_status: list[MarketStatusEvent] = field(default_factory=list)


def _fill_row(f: Any) -> dict[str, Any]:
    """Serialize a fill. The per-fill fee decomposition is emitted only when the
    fee model supplies one (a market schedule other than 'n/a'), so runs using a
    flat fee model (zero / configurable placeholder) keep their original shape."""
    row: dict[str, Any] = {
        "fill_id": f.fill_id,
        "order_id": f.order_id,
        "ticker": f.ticker,
        "side": f.side.value,
        "action": f.action.value,
        "quantity": f.quantity,
        "price_cents": f.price_cents,
        "fee_cents": f.fee_cents,
        "liquidity": f.liquidity.value,
        "at": f.filled_at.isoformat(),
        "source_ref": f.source_ref,
        "policy_version": f.execution_policy_version,
    }
    if f.fee_market_schedule is not None and f.fee_market_schedule != "n/a":
        row["fee_model_version"] = f.fee_model_version
        row["fee_market_schedule"] = f.fee_market_schedule
        row["fee_trade_centicents"] = f.fee_trade_centicents
        row["fee_rounding_centicents"] = f.fee_rounding_centicents
        row["fee_rebate_centicents"] = f.fee_rebate_centicents
    return row


def _latest_book_before(books: list[OrderBook], ticker: str, at: datetime) -> OrderBook | None:
    candidates = [b for b in books if b.ticker == ticker and b.captured_at <= at]
    return max(candidates, key=lambda b: b.captured_at) if candidates else None


def run_replay(config: ReplayConfig) -> Simulator:
    sim = Simulator(
        policy=config.policy, risk=config.risk, initial_cash_cents=config.initial_cash_cents
    )
    books = list(config.market_data.order_books)
    trades = list(config.market_data.trades)

    # unified, deterministically-ordered event stream. kind_rank encodes the
    # documented ordering (see module docstring / ADR 0017): market-status (1) <
    # order intent (3) < settlement (4). Books/trades are looked up per intent
    # (never future-dated), so they are not streamed.
    events: list[tuple[datetime, int, str, Any]] = []
    for k, ms in enumerate(config.market_status):
        events.append((ms.at, 1, f"{ms.at.isoformat()}|status|{ms.ticker}|{k}", ms))
    for oi in config.order_intents:
        events.append((oi.submitted_at, 3, f"{oi.submitted_at.isoformat()}|{oi.order_id}", oi))
    for j, se in enumerate(config.settlements):
        events.append((se.available_at, 4, f"{se.available_at.isoformat()}|{se.ticker}|{j}", se))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    tickers_with_status = {ms.ticker for ms in config.market_status}
    current_state: dict[str, str] = {}

    equity_curve: list[int] = [sim.initial_cash_cents]
    for at, _rank, _tie, item in events:
        if isinstance(item, MarketStatusEvent):
            current_state[item.ticker] = item.state
        elif isinstance(item, OrderIntent):
            # Gate to the OPEN window only when we actually have status data for
            # this ticker (otherwise the window is simply unknown, not closed).
            if (
                item.ticker in tickers_with_status
                and current_state.get(item.ticker, "unknown") != "open"
            ):
                sim.reject(item, "market_not_open")
            else:
                book = _latest_book_before(books, item.ticker, item.submitted_at)
                subsequent = [
                    t
                    for t in trades
                    if t.ticker == item.ticker and t.executed_at > item.submitted_at
                ]
                sim.submit(item, book=book, subsequent_trades=subsequent, now=at)
        elif isinstance(item, SettlementEvent):
            sim.settle(item.ticker, item.result, at)
        equity_curve.append(sim.portfolio.total_equity_cents(config.marks))

    # expire any still-open orders at the end (deterministic sweep)
    end = events[-1][0] if events else datetime(1970, 1, 1)
    for o in sim.orders:
        if o.is_open():
            sim.cancel(o, end, expired=True)

    sim._equity_curve = equity_curve  # type: ignore[attr-defined]
    return sim


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return "unknown"


def _dataset_hash(md: MarketData) -> str:
    payload = json.dumps(
        {
            "books": [
                [b.ticker, b.captured_at.isoformat(), list(b.yes_asks), list(b.yes_bids)]
                for b in md.order_books
            ],
            "trades": [
                [t.ticker, t.executed_at.isoformat(), t.yes_price_cents, t.quantity]
                for t in md.trades
            ],
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def write_artifacts(config: ReplayConfig, sim: Simulator, out_dir: Path) -> dict[str, Any]:
    """Write an immutable run directory. Refuses to overwrite an existing run."""
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"run directory {out_dir} already exists; refusing to overwrite")
    out_dir.mkdir(parents=True, exist_ok=True)

    equity_curve = getattr(sim, "_equity_curve", [sim.initial_cash_cents])
    metrics = compute_metrics(sim, marks=config.marks, equity_curve=equity_curve)

    def dump(name: str, obj: Any) -> str:
        text = json.dumps(obj, indent=2, sort_keys=True, default=str)
        (out_dir / name).write_text(text)
        return hashlib.sha256(text.encode()).hexdigest()

    hashes = {}
    hashes["orders.json"] = dump(
        "orders.json",
        [
            {
                "order_id": o.intent.order_id,
                "ticker": o.intent.ticker,
                "side": o.intent.side.value,
                "action": o.intent.action.value,
                "type": o.intent.order_type.value,
                "limit_cents": o.intent.limit_price_cents,
                "quantity": o.intent.quantity,
                "filled": o.filled_quantity,
                "state": o.state.value,
                "reject_reason": o.reject_reason,
            }
            for o in sim.orders
        ],
    )
    hashes["fills.json"] = dump("fills.json", [_fill_row(f) for f in sim.fills])
    hashes["ledger.json"] = dump(
        "ledger.json",
        [
            {
                "seq": e.seq,
                "at": e.at.isoformat(),
                "kind": e.kind.value,
                "cash_delta_cents": e.cash_delta_cents,
                "reserved_delta_cents": e.reserved_delta_cents,
                "payload": e.payload,
            }
            for e in sim.ledger.entries
        ],
    )
    hashes["rejections.json"] = dump("rejections.json", sim.rejections)
    hashes["positions.json"] = dump(
        "positions.json",
        {
            t: {
                "yes_qty": p.yes_qty,
                "no_qty": p.no_qty,
                "yes_cost_cents": p.yes_cost_cents,
                "no_cost_cents": p.no_cost_cents,
                "realized_pnl_cents": p.realized_pnl_cents,
                "fees_cents": p.fees_cents,
                "settled": p.settled,
            }
            for t, p in sim.portfolio.positions.items()
        },
    )
    hashes["equity_curve.json"] = dump("equity_curve.json", equity_curve)
    hashes["metrics.json"] = dump("metrics.json", metrics)

    manifest: dict[str, Any] = {
        "banner": SIMULATION_ONLY_BANNER,
        "run_id": config.run_id,
        "git_commit": _git_commit(),
        "dataset_sha256": _dataset_hash(config.market_data),
        "initial_cash_cents": config.initial_cash_cents,
        "execution_policy": config.policy.to_manifest(),
        "fee_model_version": config.policy.fee_model.version,
        "risk_config": config.risk.to_manifest(),
        "known_limitations": KNOWN_LIMITATIONS,
        "output_hashes": hashes,
        "h0019_isolation": "no H0019 artifact is read or written by this run",
    }
    dump("run_manifest.json", manifest)

    acct = metrics["accounting"]
    exe = metrics["execution"]
    summary = (
        f"{SIMULATION_ONLY_BANNER}\nrun {config.run_id} (git {manifest['git_commit'][:10]})\n"
        f"start ${acct['starting_cash_cents'] / 100:.2f}  "
        f"equity ${acct['total_equity_cents'] / 100:.2f}  "
        f"realized ${acct['realized_pnl_cents'] / 100:.2f}  "
        f"fees ${acct['total_fees_cents'] / 100:.2f}\n"
        f"orders {exe['orders_submitted']} (filled {exe['orders_filled']}, "
        f"rejected {exe['orders_rejected']}, canceled {exe['orders_canceled_or_expired']})  "
        f"fill_rate {exe['fill_rate']}\n"
    )
    (out_dir / "summary.txt").write_text(summary)
    return manifest
