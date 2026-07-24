"""Settlement-queue fairness (ADR 0012).

These tests exist because of a measured production starvation. On 2026-07-23
the queue reported `settle_checks=25, settled_captured=0, settle_errors=0` for
20+ consecutive cycles: 80 pending `KXRAIN` markets were the oldest by
`close_time`, they legitimately return HTTP 200 with an empty `result` while
awaiting settlement, and nothing recorded that they had already been checked.
With `ORDER BY close_time ASC` and a bounded batch they held every slot
forever, while 1,476 other pending markets -- 10 of 10 sampled being
`finalized` with real results -- were never reached.

All payloads here are synthetic.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.config import Environment
from kalshi_weather.ingestion.settlement_sync import (
    RETRY_POLICY,
    SettlementOutcome,
    capture_settled_transitions,
    classify_market,
    find_tickers_pending_settlement_check,
    record_settlement_attempt,
)
from kalshi_weather.kalshi.client import KalshiClient
from kalshi_weather.kalshi.models import Market
from kalshi_weather.storage.database import session_scope
from kalshi_weather.storage.models import Base, MarketSnapshot, SettlementAttempt
from kalshi_weather.storage.repositories import save_market_snapshot

NOW = datetime(2026, 7, 23, 23, 0, tzinfo=UTC)
OLD_CLOSE = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)  # KXRAIN blockers: oldest
NEW_CLOSE = datetime(2026, 7, 23, 16, 0, tzinfo=UTC)  # actionable markets: newer


@pytest.fixture
async def session_factory():  # type: ignore[no-untyped-def]
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed(
    session: AsyncSession,
    ticker: str,
    *,
    close_time: datetime,
    result: str | None = None,
    expiration_time: datetime | None = None,
) -> None:
    await save_market_snapshot(
        session,
        market_ticker=ticker,
        event_ticker=ticker.rsplit("-", 1)[0],
        market_type="binary",
        title="t",
        subtitle=None,
        status="closed",
        yes_bid_cents=0,
        yes_ask_cents=1,
        last_price_cents=0,
        volume=0,
        open_interest=0,
        close_time=close_time,
        rules_primary="r",
        rules_secondary=None,
        raw_payload_id=None,
        result=result,
        expiration_value=None,
        expiration_time=expiration_time,
    )


def _payload(ticker: str, *, result: str = "", status: str = "closed") -> dict:
    return {
        "market": {
            "ticker": ticker,
            "event_ticker": ticker.rsplit("-", 1)[0],
            "market_type": "binary",
            "title": "t",
            "status": status,
            "yes_bid_dollars": "0.0000",
            "yes_ask_dollars": "0.0100",
            "last_price_dollars": "0.0000",
            "volume_fp": "0.00",
            "open_interest_fp": "0.00",
            "close_time": NEW_CLOSE.isoformat().replace("+00:00", "Z"),
            "rules_primary": "r",
            "result": result,
        }
    }


def _client(handler) -> KalshiClient:  # type: ignore[no-untyped-def]
    return KalshiClient(
        base_url="https://example.invalid/trade-api/v2",
        environment=Environment.DEVELOPMENT,
        transport=httpx.MockTransport(handler),
        min_request_interval_seconds=0,
        max_retries=1,
    )


def _handler(spec: dict):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        for ticker, value in spec.items():
            if ticker in request.url.path:
                if isinstance(value, int):
                    return httpx.Response(value, json={"error": {"code": "x"}})
                return httpx.Response(200, json=value)
        # Default: an unsettled market (the KXRAIN shape).
        tk = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=_payload(tk))

    return handler


async def _run(session_factory, spec, *, limit=25, now=NOW):  # type: ignore[no-untyped-def]
    async with _client(_handler(spec)) as client, session_scope(session_factory) as session:
        return await capture_settled_transitions(
            client,
            session,
            open_tickers=set(),
            limit=limit,
            recent_days=7,
            session_factory=session_factory,
            now=now,
        )


# --- Classification ----------------------------------------------------------


def test_classify_settled_and_unsettled() -> None:
    assert classify_market(Market(ticker="T", result="yes"), now=NOW) is (
        SettlementOutcome.SETTLED_AND_SAVED
    )
    assert classify_market(Market(ticker="T", result="no"), now=NOW) is (
        SettlementOutcome.SETTLED_AND_SAVED
    )


def test_classify_not_final_yet_vs_result_missing() -> None:
    """The KXRAIN shape: 200, empty result. expiration_time decides which."""
    future = Market(ticker="T", result="", expiration_time=NOW + timedelta(days=1))
    past = Market(ticker="T", result="", expiration_time=NOW - timedelta(days=1))
    assert classify_market(future, now=NOW) is SettlementOutcome.NOT_FINAL_YET
    assert classify_market(past, now=NOW) is SettlementOutcome.RESULT_MISSING_RETRYABLE


def test_classify_malformed_result_is_not_a_settlement() -> None:
    """A result we do not understand must never become a valid settlement."""
    m = Market(ticker="T", result="maybe")
    assert classify_market(m, now=NOW) is SettlementOutcome.MALFORMED_PAYLOAD


def test_every_outcome_has_an_explicit_retry_policy() -> None:
    for outcome in SettlementOutcome:
        assert outcome in RETRY_POLICY
        retryable, cooldown = RETRY_POLICY[outcome]
        assert (cooldown is None) is (not retryable), outcome


# --- The starvation regression ----------------------------------------------


async def test_blockers_do_not_hide_actionable_markets(session_factory) -> None:  # type: ignore[no-untyped-def]
    """THE regression test. 25 older never-settling markets + 5 newer
    settleable ones, batch limit 25: the old queue returned only the 25
    blockers, forever."""
    async with session_scope(session_factory) as s:
        for i in range(25):
            await _seed(s, f"KXRAIN-26JUL21-B{i:02d}", close_time=OLD_CLOSE)
        for i in range(5):
            await _seed(s, f"KXTEMPNYCH-26JUL2316-T{i}", close_time=NEW_CLOSE)

    spec = {
        f"KXTEMPNYCH-26JUL2316-T{i}": _payload(f"KXTEMPNYCH-26JUL2316-T{i}", result="yes")
        for i in range(5)
    }

    first = await _run(session_factory, spec, limit=25)
    assert first.checked == 25
    assert first.settled_captured == 0  # all blockers, exactly as before the fix

    # Second cycle: blockers are in cooldown, so the actionable markets surface.
    second = await _run(session_factory, spec, limit=25, now=NOW + timedelta(minutes=5))
    assert second.settled_captured == 5, "actionable markets must be reachable"
    assert second.checked == 5
    assert second.deferred_cooldown == 25


async def test_cooldown_expires_and_blockers_retry(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        await _seed(s, "KXRAIN-26JUL21-A", close_time=OLD_CLOSE)
    await _run(session_factory, {}, limit=5)
    mid = await _run(session_factory, {}, limit=5, now=NOW + timedelta(minutes=30))
    assert mid.checked == 0, "still cooling down"
    later = await _run(session_factory, {}, limit=5, now=NOW + timedelta(hours=2))
    assert later.checked == 1, "retried after cooldown -- never dropped"


async def test_settled_market_never_re_enters(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        await _seed(s, "KXTEMPNYCH-26JUL2316-T1", close_time=NEW_CLOSE)
    spec = {"KXTEMPNYCH-26JUL2316-T1": _payload("KXTEMPNYCH-26JUL2316-T1", result="yes")}
    first = await _run(session_factory, spec)
    assert first.settled_captured == 1
    again = await _run(session_factory, spec, now=NOW + timedelta(days=1))
    assert again.checked == 0, "latest snapshot now has a result"


async def test_removed_market_is_terminal_and_never_re_enters(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        await _seed(s, "KXGONE-26JUL21-A", close_time=OLD_CLOSE)
    first = await _run(session_factory, {"KXGONE": 404})
    assert first.outcomes[SettlementOutcome.MARKET_REMOVED.value] == 1
    assert first.errors == 0
    # Deliberately inside the 7-day recency window, so the market is still
    # *scanned* and we prove terminality is what excludes it -- past the
    # window it would drop out for an unrelated reason and prove nothing.
    later = await _run(session_factory, {"KXGONE": 404}, now=NOW + timedelta(days=2))
    assert later.checked == 0, "terminal outcomes never re-enter"
    assert later.terminal_excluded == 1


async def test_api_failure_is_retryable_after_cooldown(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        await _seed(s, "KXFLAKY-26JUL21-A", close_time=OLD_CLOSE)
    first = await _run(session_factory, {"KXFLAKY": 500})
    assert first.outcomes[SettlementOutcome.API_FAILURE_RETRYABLE.value] == 1
    assert first.errors == 1
    soon = await _run(session_factory, {"KXFLAKY": 500}, now=NOW + timedelta(minutes=5))
    assert soon.checked == 0, "cooling down"
    ok = await _run(
        session_factory,
        {"KXFLAKY-26JUL21-A": _payload("KXFLAKY-26JUL21-A", result="no")},
        now=NOW + timedelta(minutes=20),
    )
    assert ok.settled_captured == 1


async def test_one_failure_does_not_roll_back_another_success(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Per-market transactions: a 500 on one ticker must not undo another."""
    async with session_scope(session_factory) as s:
        await _seed(s, "KXAAA-26JUL21-A", close_time=OLD_CLOSE)
        await _seed(s, "KXBBB-26JUL21-B", close_time=OLD_CLOSE + timedelta(minutes=1))
    stats = await _run(
        session_factory,
        {"KXAAA": 500, "KXBBB-26JUL21-B": _payload("KXBBB-26JUL21-B", result="yes")},
    )
    assert stats.settled_captured == 1
    assert stats.errors == 1
    async with session_scope(session_factory) as s:
        rows = (
            await s.scalars(
                select(MarketSnapshot).where(MarketSnapshot.market_ticker == "KXBBB-26JUL21-B")
            )
        ).all()
        attempts = (await s.scalars(select(SettlementAttempt))).all()
    assert len(rows) == 2, "the successful market's snapshot survived"
    assert {a.market_ticker for a in attempts} == {"KXAAA-26JUL21-A", "KXBBB-26JUL21-B"}


async def test_malformed_result_does_not_become_a_settlement(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        await _seed(s, "KXODD-26JUL21-A", close_time=OLD_CLOSE)
    stats = await _run(
        session_factory, {"KXODD-26JUL21-A": _payload("KXODD-26JUL21-A", result="partial")}
    )
    assert stats.settled_captured == 0
    assert stats.outcomes[SettlementOutcome.MALFORMED_PAYLOAD.value] == 1
    async with session_scope(session_factory) as s:
        latest = (
            await s.scalars(
                select(MarketSnapshot)
                .where(MarketSnapshot.market_ticker == "KXODD-26JUL21-A")
                .order_by(MarketSnapshot.id.desc())
            )
        ).first()
    assert latest is not None and latest.result not in ("yes", "no")


async def test_ordering_is_deterministic_among_actionable(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        await _seed(s, "KXC-26JUL21-C", close_time=OLD_CLOSE + timedelta(hours=2))
        await _seed(s, "KXA-26JUL21-A", close_time=OLD_CLOSE)
        await _seed(s, "KXB-26JUL21-B", close_time=OLD_CLOSE + timedelta(hours=1))
    async with session_scope(session_factory) as s:
        got = await find_tickers_pending_settlement_check(
            s, exclude_tickers=set(), recent_days=7, limit=10, now=NOW
        )
    assert got == ["KXA-26JUL21-A", "KXB-26JUL21-B", "KXC-26JUL21-C"]


async def test_filtering_happens_before_limit(session_factory) -> None:  # type: ignore[no-untyped-def]
    """With 5 cooled-down blockers ahead of 1 actionable market and limit=2,
    the actionable market must still be returned."""
    async with session_scope(session_factory) as s:
        for i in range(5):
            await _seed(s, f"KXRAIN-26JUL21-B{i}", close_time=OLD_CLOSE)
        await _seed(s, "KXTEMPNYCH-26JUL2316-T1", close_time=NEW_CLOSE)
        for i in range(5):
            await record_settlement_attempt(
                s,
                market_ticker=f"KXRAIN-26JUL21-B{i}",
                outcome=SettlementOutcome.NOT_FINAL_YET,
                now=NOW,
            )
    async with session_scope(session_factory) as s:
        got = await find_tickers_pending_settlement_check(
            s, exclude_tickers=set(), recent_days=7, limit=2, now=NOW + timedelta(minutes=1)
        )
    assert got == ["KXTEMPNYCH-26JUL2316-T1"]


async def test_forward_progress_across_repeated_cycles(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Ten blockers, ten actionable markets, limit 5: every actionable market
    is captured within a bounded number of cycles."""
    async with session_scope(session_factory) as s:
        for i in range(10):
            await _seed(s, f"KXRAIN-26JUL21-B{i:02d}", close_time=OLD_CLOSE)
        for i in range(10):
            await _seed(s, f"KXTEMPNYCH-26JUL2316-T{i:02d}", close_time=NEW_CLOSE)
    spec = {
        f"KXTEMPNYCH-26JUL2316-T{i:02d}": _payload(f"KXTEMPNYCH-26JUL2316-T{i:02d}", result="yes")
        for i in range(10)
    }
    captured = 0
    for cycle in range(6):
        st = await _run(session_factory, spec, limit=5, now=NOW + timedelta(minutes=cycle))
        captured += st.settled_captured
    assert captured == 10, "all actionable markets reached despite the blocker head"


async def test_metrics_classify_outcomes_and_expose_fairness(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        for i in range(3):
            await _seed(s, f"KXRAIN-26JUL21-B{i}", close_time=OLD_CLOSE)
        await _seed(s, "KXTEMPNYCH-26JUL2316-T1", close_time=NEW_CLOSE)
        await _seed(s, "KXGONE-26JUL21-G", close_time=OLD_CLOSE)
    spec = {
        "KXTEMPNYCH-26JUL2316-T1": _payload("KXTEMPNYCH-26JUL2316-T1", result="no"),
        "KXGONE": 404,
    }
    st = await _run(session_factory, spec, limit=25)
    d = st.as_stats()
    assert d["settle_checks"] == 5
    assert d["settle_outcome_settled_and_saved"] == 1
    assert d["settle_outcome_not_final_yet"] == 3
    assert d["settle_outcome_market_removed"] == 1
    assert d["settle_unique_series"] == 3
    assert d["settle_max_from_one_series"] == 3
    assert d["settle_oldest_actionable_age_seconds"] > 0
    assert sum(st.outcomes.values()) == st.checked, "every attempt is classified"


async def test_attempt_and_snapshot_commit_atomically(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        await _seed(s, "KXTEMPNYCH-26JUL2316-T1", close_time=NEW_CLOSE)
    await _run(
        session_factory,
        {"KXTEMPNYCH-26JUL2316-T1": _payload("KXTEMPNYCH-26JUL2316-T1", result="yes")},
    )
    async with session_scope(session_factory) as s:
        attempt = (await s.scalars(select(SettlementAttempt))).one()
        snap = (
            await s.scalars(
                select(MarketSnapshot)
                .where(MarketSnapshot.market_ticker == "KXTEMPNYCH-26JUL2316-T1")
                .order_by(MarketSnapshot.id.desc())
            )
        ).first()
    assert attempt.outcome == SettlementOutcome.SETTLED_AND_SAVED.value
    assert snap is not None and attempt.snapshot_id == snap.id, "provenance linked"
    assert attempt.retryable is False


async def test_retry_state_survives_restart(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Cooldown lives in the database, not process memory: a fresh call with
    no shared state still honours it."""
    async with session_scope(session_factory) as s:
        await _seed(s, "KXRAIN-26JUL21-A", close_time=OLD_CLOSE)
    await _run(session_factory, {}, limit=5)
    async with session_scope(session_factory) as s:  # simulates a new process
        got = await find_tickers_pending_settlement_check(
            s, exclude_tickers=set(), recent_days=7, limit=5, now=NOW + timedelta(minutes=10)
        )
    assert got == []


async def test_existing_snapshots_are_never_mutated(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        await _seed(s, "KXTEMPNYCH-26JUL2316-T1", close_time=NEW_CLOSE)
    async with session_scope(session_factory) as s:
        before = (await s.scalars(select(MarketSnapshot))).all()
        snapshot = [(r.id, r.result, r.content_hash) for r in before]
    await _run(
        session_factory,
        {"KXTEMPNYCH-26JUL2316-T1": _payload("KXTEMPNYCH-26JUL2316-T1", result="yes")},
    )
    async with session_scope(session_factory) as s:
        after = (await s.scalars(select(MarketSnapshot).order_by(MarketSnapshot.id))).all()
    assert [(r.id, r.result, r.content_hash) for r in after][: len(snapshot)] == snapshot


async def test_snapshot_persists_when_raw_payload_is_in_the_open_transaction(  # type: ignore[no-untyped-def]
    session_factory,
) -> None:
    """End-to-end persistence with an uncommitted raw payload (ADR 0012).

    Wires a real payload sink writing into the same open session, so
    `last_raw_payload_id` points at an uncommitted `raw_api_payloads` row --
    production's shape -- and asserts the snapshot still persists with full
    provenance.

    **Honest limitation:** this does NOT reproduce the production failure it
    was written for. On 2026-07-23 the separate-session approach made that row
    invisible and 10 of 10 newly reached markets hit
    `market_snapshots_raw_payload_id_fkey`. This suite runs on SQLite with a
    `StaticPool`, where every session shares one connection and foreign keys
    are not enforced by default, so the separate-session variant passes here
    too (verified by reverting the fix and re-running). That is exactly why the
    bug escaped the unit suite and only appeared against PostgreSQL.

    The real proof is the production evidence plus the post-fix cycles. Catching
    this class of defect in tests would need a PostgreSQL-backed integration
    test with FK enforcement -- recorded as follow-up, not faked here.
    """
    from kalshi_weather.storage.repositories import save_raw_payload

    async with session_scope(session_factory) as s:
        await _seed(s, "KXTEMPNYCH-26JUL2316-T1", close_time=NEW_CLOSE)

    spec = {"KXTEMPNYCH-26JUL2316-T1": _payload("KXTEMPNYCH-26JUL2316-T1", result="yes")}

    async with session_scope(session_factory) as session:

        async def sink(source, endpoint, request_key, status, payload):  # type: ignore[no-untyped-def]
            raw = await save_raw_payload(
                session,
                source=source,
                endpoint_or_channel=endpoint,
                request_key=request_key,
                http_status=status,
                payload_json=payload,
            )
            return raw.id

        client = KalshiClient(
            base_url="https://example.invalid/trade-api/v2",
            environment=Environment.DEVELOPMENT,
            transport=httpx.MockTransport(_handler(spec)),
            min_request_interval_seconds=0,
            max_retries=1,
            raw_payload_sink=sink,
        )
        async with client:
            stats = await capture_settled_transitions(
                client,
                session,
                open_tickers=set(),
                limit=5,
                recent_days=7,
                session_factory=session_factory,
                now=NOW,
            )
        assert client.last_raw_payload_id is not None, "sink must have run"

    assert stats.settled_captured == 1, "must persist despite the uncommitted raw payload"
    assert stats.errors == 0
    async with session_scope(session_factory) as s:
        attempt = (await s.scalars(select(SettlementAttempt))).one()
    assert attempt.outcome == SettlementOutcome.SETTLED_AND_SAVED.value
    assert attempt.snapshot_id is not None
    assert attempt.raw_payload_id is not None, "provenance retained"
