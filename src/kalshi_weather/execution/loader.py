"""Parse a JSON run configuration into a ReplayConfig. Explicit inputs only --
no defaults that connect anywhere; the config fully specifies the synthetic or
historical market data, the orders, and the settlements. Never reads H0019."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from kalshi_weather.execution.market import MarketData, OrderBook, Trade
from kalshi_weather.execution.models import Action, OrderIntent, OrderType, Side, TimeInForce
from kalshi_weather.execution.policy import ExecutionPolicy, FeeModel, FillMode, RiskConfig
from kalshi_weather.execution.replay import ReplayConfig, SettlementEvent


def _dt(v: str) -> datetime:
    return datetime.fromisoformat(v)


def load_run_config(path: Path) -> ReplayConfig:
    raw: dict[str, Any] = json.loads(path.read_text())
    pol_raw = raw.get("policy", {})
    fee_raw = pol_raw.get("fee_model", {})
    fee = FeeModel(
        version=fee_raw.get("version", "zero-v1"),
        rate_bps=int(fee_raw.get("rate_bps", 0)),
        per_contract_min_cents=int(fee_raw.get("per_contract_min_cents", 0)),
        charge_on_maker=bool(fee_raw.get("charge_on_maker", True)),
    )
    policy = ExecutionPolicy(
        version=pol_raw.get("version", "exec-policy-v1"),
        fill_mode=FillMode(pol_raw.get("fill_mode", "synthetic")),
        fee_model=fee,
        queue_ahead_contracts=int(pol_raw.get("queue_ahead_contracts", 0)),
        max_book_age_seconds=float(pol_raw.get("max_book_age_seconds", 300.0)),
        max_spread_cents=int(pol_raw.get("max_spread_cents", 100)),
        random_seed=int(pol_raw.get("random_seed", 0)),
    )
    risk = RiskConfig(
        **{k: v for k, v in raw.get("risk", {}).items() if k in RiskConfig.__dataclass_fields__}
    )

    md_raw = raw.get("market_data", {})
    books = [
        OrderBook(
            ticker=b["ticker"],
            captured_at=_dt(b["captured_at"]),
            yes_asks=tuple((int(p), int(q)) for p, q in b.get("yes_asks", [])),
            yes_bids=tuple((int(p), int(q)) for p, q in b.get("yes_bids", [])),
            source_ref=b.get("source_ref", "config"),
        )
        for b in md_raw.get("order_books", [])
    ]
    trades = [
        Trade(
            ticker=t["ticker"],
            executed_at=_dt(t["executed_at"]),
            yes_price_cents=int(t["yes_price_cents"]),
            quantity=int(t["quantity"]),
            source_ref=t.get("source_ref", "config"),
        )
        for t in md_raw.get("trades", [])
    ]
    intents = [
        OrderIntent(
            order_id=o["order_id"],
            strategy_id=o.get("strategy_id", "config"),
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
        for o in raw.get("order_intents", [])
    ]
    settlements = [
        SettlementEvent(
            ticker=s["ticker"], result=Side(s["result"]), available_at=_dt(s["available_at"])
        )
        for s in raw.get("settlements", [])
    ]
    marks = {k: int(v) for k, v in raw.get("marks", {}).items()}
    return ReplayConfig(
        run_id=raw["run_id"],
        initial_cash_cents=int(raw["initial_cash_cents"]),
        policy=policy,
        risk=risk,
        market_data=MarketData(order_books=books, trades=trades),
        order_intents=intents,
        settlements=settlements,
        marks=marks,
    )
