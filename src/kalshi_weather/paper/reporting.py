"""Daily paper-trading report and attribution.

Everything reported here is labelled synthetic/manual-signal OPERATIONAL
testing. Nothing in this module may describe results as strategy
performance -- there is no strategy.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.paper.engine import station_of
from kalshi_weather.paper.store import (
    PaperFillRow,
    PaperOrderIntentRow,
    PaperPnlSnapshotRow,
    PaperRiskDecisionRow,
    PaperRun,
    PaperSignalRow,
)

BANNER = "SYNTHETIC/MANUAL-SIGNAL OPERATIONAL TESTING — NOT STRATEGY PERFORMANCE"


def family_of(ticker: str) -> str:
    head = ticker.split("-", 1)[0]
    for prefix in ("KXHIGHT", "KXLOWT"):
        if head.startswith(prefix):
            return prefix
    return head or "UNKNOWN"


async def build_daily_report(session: AsyncSession, day: date) -> dict[str, Any]:
    """Aggregate one calendar day (UTC) of paper records into a report dict."""

    def on_day(col: Any) -> Any:
        start = datetime(day.year, day.month, day.day)
        return (col >= start) & (col < start + timedelta(days=1))

    runs = (await session.scalars(select(PaperRun).where(on_day(PaperRun.created_at)))).all()
    signals = (
        await session.scalars(select(PaperSignalRow).where(on_day(PaperSignalRow.created_at)))
    ).all()
    intents = (
        await session.scalars(
            select(PaperOrderIntentRow).where(on_day(PaperOrderIntentRow.created_at))
        )
    ).all()
    fills = (
        await session.scalars(select(PaperFillRow).where(on_day(PaperFillRow.created_at)))
    ).all()
    decisions = (
        await session.scalars(
            select(PaperRiskDecisionRow).where(on_day(PaperRiskDecisionRow.created_at))
        )
    ).all()
    pnl_rows = (
        await session.scalars(
            select(PaperPnlSnapshotRow)
            .where(on_day(PaperPnlSnapshotRow.at))
            .order_by(PaperPnlSnapshotRow.id)
        )
    ).all()

    filled_order_ids = {f.order_id for f in fills}
    accepted = [i for i in intents if i.final_state != "rejected"]
    unfilled = [i for i in accepted if i.order_id not in filled_order_ids]
    rejections: dict[str, int] = {}
    for d in decisions:
        if not d.approved and d.reason:
            rejections[d.reason] = rejections.get(d.reason, 0) + 1

    def bucket(rows: Sequence[Any], key: Any) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in rows:
            k = key(r)
            out[k] = out.get(k, 0) + 1
        return out

    exposure_by_station: dict[str, int] = {}
    exposure_by_family: dict[str, int] = {}
    for f in fills:
        cost = f.price_cents * f.quantity
        st, fam = station_of(f.ticker), family_of(f.ticker)
        exposure_by_station[st] = exposure_by_station.get(st, 0) + cost
        exposure_by_family[fam] = exposure_by_family.get(fam, 0) + cost

    last_pnl = pnl_rows[-1] if pnl_rows else None
    return {
        "banner": BANNER,
        "date": day.isoformat(),
        "runs": len(runs),
        "signals": len(signals),
        "signals_rejected": sum(1 for s in signals if not s.accepted),
        "accepted_intents": len(accepted),
        "rejected_intents": len(intents) - len(accepted),
        "fills": len(fills),
        "unfilled_intents": len(unfilled),
        "fees_cents": sum(f.fee_cents for f in fills),
        "realized_pnl_cents": last_pnl.realized_pnl_cents if last_pnl else 0,
        "unrealized_pnl_cents": last_pnl.unrealized_pnl_cents if last_pnl else None,
        "total_equity_at_cost_cents": last_pnl.equity_at_cost_cents if last_pnl else None,
        "drawdown_cents": last_pnl.drawdown_cents if last_pnl else 0,
        "exposure_by_station_cents": exposure_by_station,
        "exposure_by_family_cents": exposure_by_family,
        "attribution_by_signal_source": bucket(signals, lambda s: s.source_type),
        "attribution_by_execution_confidence": bucket(accepted, lambda i: i.execution_confidence),
        "rejection_reasons": rejections,
        "stale_data_incidents": sum(
            v for k, v in rejections.items() if k.startswith("stale_") or k == "no_market_snapshot"
        ),
        "coverage_failures": sum(
            v
            for k, v in rejections.items()
            if k in ("missing_poll_evidence", "no_book_evidence", "stale_book_evidence")
        ),
    }


def render_report(report: dict[str, Any]) -> str:
    lines = [f"=== PAPER REPORT {report['date']} ===", report["banner"], ""]
    for k, v in report.items():
        if k in ("banner", "date"):
            continue
        lines.append(f"  {k}: {v}")
    return "\n".join(lines)
