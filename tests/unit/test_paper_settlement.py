"""Paper settlement: exact payouts, idempotency, fail-closed evidence,
corrections, cash conservation. Synthetic fixtures and disposable DBs only.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kalshi_weather.execution.fees import ZERO_FEE_MODEL
from kalshi_weather.execution.policy import ExecutionPolicy, FillMode, RiskConfig
from kalshi_weather.paper.settlement import (
    REASON_ALREADY_SETTLED,
    REASON_AMBIGUOUS,
    REASON_UNRESOLVED,
    REASON_UNSUPPORTED,
    SettlementEvidence,
    load_settlement_evidence,
    run_settlement_session,
)
from kalshi_weather.storage.models import Base, MarketSnapshot

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
T = "KXHIGHTCHI-X-B90"

POLICY = ExecutionPolicy(fill_mode=FillMode.MARKETABLE, fee_model=ZERO_FEE_MODEL)
RISK = RiskConfig()


def entries_with_position(
    *, yes_qty: int = 0, yes_cost: int = 0, no_qty: int = 0, no_cost: int = 0, cash: int = 10_000
) -> list[tuple[int, datetime, str, int, int, dict[str, Any]]]:
    """Prior ledger: a deposit plus one fill establishing the position."""
    deposit_payload = {"amount_cents": cash, "reason": "initial_cash"}
    out: list[tuple[int, datetime, str, int, int, dict[str, Any]]] = [
        (0, NOW - timedelta(days=1), "deposit", cash, 0, deposit_payload)
    ]
    seq = 1
    if yes_qty:
        price = yes_cost // yes_qty
        out.append(
            (
                seq,
                NOW - timedelta(hours=20),
                "fill",
                -yes_cost,
                0,
                {
                    "ticker": T,
                    "side": "yes",
                    "action": "buy",
                    "quantity": yes_qty,
                    "price_cents": price,
                    "fee_cents": 0,
                },
            )
        )
        seq += 1
    if no_qty:
        price = no_cost // no_qty
        out.append(
            (
                seq,
                NOW - timedelta(hours=19),
                "fill",
                -no_cost,
                0,
                {
                    "ticker": T,
                    "side": "no",
                    "action": "buy",
                    "quantity": no_qty,
                    "price_cents": price,
                    "fee_cents": 0,
                },
            )
        )
    return out


def ev(result: str | None, reason: str | None = None, **kw: Any) -> dict[str, SettlementEvidence]:
    return {
        T: SettlementEvidence(
            ticker=T,
            result=result,
            settlement_ts=NOW - timedelta(hours=1),
            snapshot_source_id=99,
            observed_at=NOW - timedelta(minutes=30),
            reason=reason,
            **kw,
        )
    }


def settle(  # type: ignore[no-untyped-def]
    prior: list[Any],
    evidence: dict[str, SettlementEvidence],
    settled: dict[str, tuple[int, str]] | None = None,
):
    return run_settlement_session(
        now=NOW,
        prior_entries=prior,
        evidence=evidence,
        prior_settled=settled or {},
        exec_policy=POLICY,
        sim_risk=RISK,
    )


def test_winning_yes_settlement_exact_payout() -> None:
    # 2 YES @ 55c (cost 110); result YES -> payout 200, realized +90
    res = settle(entries_with_position(yes_qty=2, yes_cost=110), ev("yes"))
    d = res.decisions[0]
    assert d.status == "settled" and d.result == "yes"
    assert d.gross_payout_cents == 200
    assert d.position_cost_cents == 110
    assert d.realized_pnl_delta_cents == 90
    assert d.fee_cents == 0
    assert res.pnl["cash_cents"] == 10_000 - 110 + 200
    assert res.pnl["equity_at_cost_cents"] == 10_090
    assert res.pnl["position_cost_cents"] == 0  # position closed


def test_winning_no_settlement_exact_payout() -> None:
    # 3 NO @ 45c (cost 135); result NO -> payout 300, realized +165
    res = settle(entries_with_position(no_qty=3, no_cost=135), ev("no"))
    d = res.decisions[0]
    assert d.status == "settled" and d.gross_payout_cents == 300
    assert d.realized_pnl_delta_cents == 165
    assert res.pnl["equity_at_cost_cents"] == 10_165


def test_losing_position_pays_zero_and_realizes_loss() -> None:
    # 2 YES @ 55c; result NO -> payout 0, realized -110
    res = settle(entries_with_position(yes_qty=2, yes_cost=110), ev("no"))
    d = res.decisions[0]
    assert d.status == "settled" and d.gross_payout_cents == 0
    assert d.realized_pnl_delta_cents == -110
    assert res.pnl["cash_cents"] == 10_000 - 110
    assert res.pnl["equity_at_cost_cents"] == 9_890


def test_cash_conservation_bankroll_identity() -> None:
    res = settle(entries_with_position(yes_qty=2, yes_cost=110), ev("yes"))
    # bankroll identity: cash + reserved + open cost == deposit + total realized
    assert (
        res.pnl["cash_cents"] + res.pnl["reserved_cents"] + res.pnl["position_cost_cents"]
        == 10_000 + res.pnl["realized_pnl_cents"]
    )
    # every new ledger entry is a settlement entry with exact deltas
    kinds = [e.kind for e in res.new_ledger_entries]
    assert kinds == ["settlement"]
    assert res.new_ledger_entries[0].cash_delta_cents == 200


def test_idempotent_rerun_skips_already_settled() -> None:
    prior = entries_with_position(yes_qty=2, yes_cost=110)
    first = settle(prior, ev("yes"))
    # append the settlement entry, mark ticker settled (as the store would)
    prior2 = prior + [
        (e.global_seq, e.at, e.kind, e.cash_delta_cents, e.reserved_delta_cents, e.payload)
        for e in first.new_ledger_entries
    ]
    second = settle(prior2, ev("yes"), settled={T: (1, "yes")})
    assert all(d.status != "settled" for d in second.decisions)
    assert not second.new_ledger_entries  # no duplicate money movement
    assert second.pnl["equity_at_cost_cents"] == first.pnl["equity_at_cost_cents"]


def test_duplicate_settlement_rejected_even_if_position_looks_open() -> None:
    # prior_settled says settled, but ledger has only the open position (a
    # partial-persist scenario): the run must skip, not double-pay.
    prior = entries_with_position(yes_qty=2, yes_cost=110)
    res = settle(prior, ev("yes"), settled={T: (1, "yes")})
    statuses = {d.status for d in res.decisions}
    assert f"skipped_{REASON_ALREADY_SETTLED}" in statuses
    assert not any(d.status == "settled" for d in res.decisions)


def test_unresolved_and_unsupported_and_ambiguous_fail_closed() -> None:
    prior = entries_with_position(yes_qty=1, yes_cost=55)
    for reason in (REASON_UNRESOLVED, REASON_UNSUPPORTED, REASON_AMBIGUOUS):
        res = settle(prior, ev(None, reason=reason))
        assert res.decisions[0].status == f"skipped_{reason}"
        assert not res.new_ledger_entries
        assert res.pnl["position_cost_cents"] == 55  # still open
    # missing evidence entirely -> unresolved
    res2 = settle(prior, {})
    assert res2.decisions[0].status == f"skipped_{REASON_UNRESOLVED}"


def test_correction_appends_successor_without_money_movement() -> None:
    prior = entries_with_position(yes_qty=2, yes_cost=110)
    first = settle(prior, ev("yes"))
    prior2 = prior + [
        (e.global_seq, e.at, e.kind, e.cash_delta_cents, e.reserved_delta_cents, e.payload)
        for e in first.new_ledger_entries
    ]
    # evidence now says NO where we paid YES -> correction record, no reversal
    res = settle(prior2, ev("no"), settled={T: (7, "yes")})
    corr = [d for d in res.decisions if d.status == "correction_detected"]
    assert len(corr) == 1
    assert corr[0].supersedes_row_id == 7 and corr[0].result == "no"
    assert not res.new_ledger_entries  # money is never silently re-moved


def test_position_closure_recorded() -> None:
    res = settle(entries_with_position(yes_qty=2, yes_cost=110), ev("yes"))
    pos = res.positions[0]
    assert pos["yes_qty"] == 0 and pos["yes_cost_cents"] == 0
    assert pos["realized_pnl_cents"] == 90


# --- evidence loader against a disposable DB --------------------------------


async def make_db(url: str, rows: list[MarketSnapshot]) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add_all(rows)
        await s.commit()
    await engine.dispose()


def snap(result: str, status: str = "finalized", minutes_ago: int = 60) -> MarketSnapshot:
    return MarketSnapshot(
        market_ticker=T,
        status=status,
        result=result,
        settlement_ts=NOW - timedelta(hours=2),
        observed_at=NOW - timedelta(minutes=minutes_ago),
        environment="production",
    )


@pytest.mark.parametrize(
    ("rows", "expected_result", "expected_reason"),
    [
        ([snap("yes")], "yes", None),
        ([snap("no", status="determined")], "no", None),
        ([], None, REASON_UNRESOLVED),
        ([snap("yes"), snap("no", minutes_ago=30)], None, REASON_AMBIGUOUS),
        ([snap("void")], None, REASON_UNSUPPORTED),
        ([snap("yes", status="active")], None, REASON_UNSUPPORTED),
    ],
)
async def test_evidence_loader_semantics(
    tmp_path: Any,
    rows: list[MarketSnapshot],
    expected_result: str | None,
    expected_reason: str | None,
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path}/research.db"
    await make_db(url, rows)
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        evidence = await load_settlement_evidence(s, [T])
    await engine.dispose()
    e = evidence[T]
    assert e.result == expected_result
    assert e.reason == expected_reason
    if expected_result:
        assert e.snapshot_source_id is not None  # provenance recorded


async def test_evidence_loader_is_select_only(tmp_path: Any) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path}/research.db"
    await make_db(url, [snap("yes")])
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        before = (await s.scalars(select(MarketSnapshot))).all()
        await load_settlement_evidence(s, [T])
        after = (await s.scalars(select(MarketSnapshot))).all()
    await engine.dispose()
    assert len(before) == len(after) == 1  # no production mutation
