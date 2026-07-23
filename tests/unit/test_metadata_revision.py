"""Post-expiration settled-metadata revision capture (ADR 0011).

These tests exist because of a measured integrity failure: on 2026-07-23 the
venue reported `volume=0` three hours after close for markets that had traded
hundreds of contracts, and published one `result` that contradicted its own
strike arithmetic -- both later corrected venue-side. Our archive captured the
provisional values faithfully and then never looked again, because the
settlement queue keys on "no result yet".

Every payload here is synthetic.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from kalshi_weather.config import Environment
from kalshi_weather.ingestion.metadata_revision import (
    FALLBACK_FINALITY_DAYS,
    classify_missing_market,
    finality_time,
    run_metadata_revision,
    run_metadata_revision_loop,
    select_revision_candidates,
)
from kalshi_weather.kalshi.client import KalshiClient
from kalshi_weather.storage.database import session_scope
from kalshi_weather.storage.models import (
    Base,
    CollectorRun,
    MarketMetadataVerification,
    MarketSnapshot,
)
from kalshi_weather.storage.repositories import save_market_snapshot

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
CLOSE = datetime(2026, 7, 23, 14, 0, tzinfo=UTC)
PAST_FINALITY = datetime(2026, 7, 30, 14, 0, tzinfo=UTC)  # close + 7d, already elapsed at NOW
FUTURE_FINALITY = datetime(2026, 8, 30, 14, 0, tzinfo=UTC)  # not yet elapsed at NOW


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
    result: str = "no",
    volume: int = 0,
    open_interest: int = 0,
    close_time: datetime | None = CLOSE,
    expiration_time: datetime | None = PAST_FINALITY,
) -> None:
    await save_market_snapshot(
        session,
        market_ticker=ticker,
        event_ticker=ticker.rsplit("-", 1)[0],
        market_type="binary",
        title="t",
        subtitle="s",
        status="finalized",
        yes_bid_cents=0,
        yes_ask_cents=1,
        last_price_cents=0,
        volume=volume,
        open_interest=open_interest,
        close_time=close_time,
        rules_primary="r",
        rules_secondary=None,
        raw_payload_id=None,
        result=result,
        expiration_value=None,
        expiration_time=expiration_time,
    )


def _market_payload(
    ticker: str,
    *,
    volume: int,
    oi: int,
    result: str,
    close: datetime = CLOSE,
    expiration: datetime = PAST_FINALITY,
) -> dict[str, Any]:
    return {
        "market": {
            "ticker": ticker,
            "event_ticker": ticker.rsplit("-", 1)[0],
            "market_type": "binary",
            "title": "t",
            "subtitle": "s",
            "status": "finalized",
            "yes_bid_dollars": "0.0000",
            "yes_ask_dollars": "0.0100",
            "last_price_dollars": "0.0000",
            "volume_fp": f"{volume}.00",
            "open_interest_fp": f"{oi}.00",
            "close_time": close.isoformat().replace("+00:00", "Z"),
            "expiration_time": expiration.isoformat().replace("+00:00", "Z"),
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


def _handler(responses: dict[str, Any]):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        for ticker, spec in responses.items():
            if ticker in request.url.path:
                if isinstance(spec, int):
                    return httpx.Response(spec, json={"error": {"code": "x"}})
                return httpx.Response(200, json=spec)
        raise AssertionError(f"unexpected path {request.url.path}")

    return handler


# --- Finality model (Phase 3) ------------------------------------------------


def test_finality_prefers_venue_expiration_time() -> None:
    assert finality_time(PAST_FINALITY, CLOSE) == PAST_FINALITY.replace(tzinfo=None)


def test_finality_falls_back_to_close_plus_seven_days() -> None:
    """Historical rows written before migration 0008 have no expiration_time."""
    expected = CLOSE.replace(tzinfo=None) + timedelta(days=FALLBACK_FINALITY_DAYS)
    assert finality_time(None, CLOSE) == expected


def test_finality_unknown_when_neither_is_known() -> None:
    """A market we cannot prove is final must never be treated as final."""
    assert finality_time(None, None) is None


def test_classify_missing_market_distinguishes_removed_from_retention_expired() -> None:
    now = datetime(2026, 8, 1, 12, 0)
    recent = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    ancient = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert classify_missing_market(recent, now=now, retention_days=67) == "market_removed"
    assert classify_missing_market(ancient, now=now, retention_days=67) == "retention_expired"


# --- Candidate selection (Phase 4) -------------------------------------------


async def test_pre_expiration_market_is_ineligible(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Result-bearing is NOT metadata-final; this is the whole point."""
    async with session_scope(session_factory) as s:
        await _seed(s, "T-EARLY", expiration_time=FUTURE_FINALITY)
    async with session_scope(session_factory) as s:
        assert await select_revision_candidates(s, now=NOW) == []


async def test_post_expiration_market_is_eligible(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        await _seed(s, "T-READY", expiration_time=PAST_FINALITY)
    async with session_scope(session_factory) as s:
        got = await select_revision_candidates(s, now=NOW)
    assert [t for t, _ in got] == ["T-READY"]


async def test_unsettled_market_is_never_eligible(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        await _seed(s, "T-OPEN", result="")
    async with session_scope(session_factory) as s:
        assert await select_revision_candidates(s, now=NOW) == []


async def test_null_expiration_time_uses_documented_fallback(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Pre-0008 rows: eligible once close_time + 7d has elapsed, not before."""
    async with session_scope(session_factory) as s:
        await _seed(s, "T-OLD-NULL", close_time=CLOSE, expiration_time=None)
        await _seed(
            s,
            "T-NEW-NULL",
            close_time=datetime(2026, 7, 31, 14, 0, tzinfo=UTC),
            expiration_time=None,
        )
    async with session_scope(session_factory) as s:
        got = await select_revision_candidates(s, now=NOW)
    assert [t for t, _ in got] == ["T-OLD-NULL"]


async def test_eligibility_filters_before_limit(session_factory) -> None:  # type: ignore[no-untyped-def]
    """THE regression test, mirroring ADR 0010: ineligible markets must not
    consume the per-cycle budget. Five not-yet-final markets sort ahead of two
    eligible ones by ticker; with limit=2 the eligible pair must still win."""
    async with session_scope(session_factory) as s:
        for i in range(5):
            await _seed(s, f"T-AAA{i}", expiration_time=FUTURE_FINALITY)
        await _seed(s, "T-ZZZ1", expiration_time=PAST_FINALITY)
        await _seed(s, "T-ZZZ2", expiration_time=PAST_FINALITY)
    async with session_scope(session_factory) as s:
        got = await select_revision_candidates(s, now=NOW, limit=2)
    assert [t for t, _ in got] == ["T-ZZZ1", "T-ZZZ2"]


async def test_already_verified_markets_do_not_starve_the_queue(session_factory) -> None:  # type: ignore[no-untyped-def]
    """A verified market leaves the queue for good, so later markets are
    reachable -- the price-sync starvation lesson applied to this queue."""
    async with session_scope(session_factory) as s:
        for i in range(3):
            await _seed(s, f"T-VERIFIED{i}")
        await _seed(s, "T-PENDING")
        for i in range(3):
            s.add(
                MarketMetadataVerification(
                    market_ticker=f"T-VERIFIED{i}",
                    verified_at=NOW,
                    finality_at=PAST_FINALITY,
                    outcome="unchanged",
                    snapshot_appended=False,
                )
            )
    async with session_scope(session_factory) as s:
        got = await select_revision_candidates(s, now=NOW, limit=2)
    assert [t for t, _ in got] == ["T-PENDING"]


async def test_ordering_is_deterministic_oldest_finality_first(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        await _seed(s, "T-B", expiration_time=datetime(2026, 7, 30, 14, 0, tzinfo=UTC))
        await _seed(s, "T-A", expiration_time=datetime(2026, 7, 30, 14, 0, tzinfo=UTC))
        await _seed(s, "T-EARLIEST", expiration_time=datetime(2026, 7, 25, 14, 0, tzinfo=UTC))
    async with session_scope(session_factory) as s:
        got = await select_revision_candidates(s, now=NOW)
    assert [t for t, _ in got] == ["T-EARLIEST", "T-A", "T-B"]


# --- Persistence semantics (Phase 5) -----------------------------------------


async def _revise(session_factory, responses, **kw):  # type: ignore[no-untyped-def]
    async with _client(_handler(responses)) as c:
        return await run_metadata_revision(session_factory=session_factory, client=c, now=NOW, **kw)


async def _counts(session_factory, ticker):  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        snaps = (
            await s.scalars(select(MarketSnapshot).where(MarketSnapshot.market_ticker == ticker))
        ).all()
        vers = (
            await s.scalars(
                select(MarketMetadataVerification).where(
                    MarketMetadataVerification.market_ticker == ticker
                )
            )
        ).all()
    return list(snaps), list(vers)


async def test_changed_volume_appends_exactly_one_snapshot(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        await _seed(s, "T-VOL", volume=0, open_interest=0)
    report = await _revise(
        session_factory, {"T-VOL": _market_payload("T-VOL", volume=646, oi=0, result="no")}
    )
    snaps, vers = await _counts(session_factory, "T-VOL")
    assert len(snaps) == 2 and snaps[-1].volume == 646
    assert report.totals()["volume_revisions"] == 1
    assert report.totals()["snapshots_appended"] == 1
    assert vers[0].outcome == "changed" and vers[0].snapshot_appended is True


async def test_changed_open_interest_appends_exactly_one_snapshot(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        await _seed(s, "T-OI", volume=5, open_interest=0)
    report = await _revise(
        session_factory, {"T-OI": _market_payload("T-OI", volume=5, oi=91, result="no")}
    )
    snaps, _ = await _counts(session_factory, "T-OI")
    assert len(snaps) == 2 and snaps[-1].open_interest == 91
    assert report.totals()["open_interest_revisions"] == 1


async def test_changed_result_appends_exactly_one_snapshot(session_factory) -> None:  # type: ignore[no-untyped-def]
    """The 2026-07-23 case: the venue corrected a wrong settlement label."""
    async with session_scope(session_factory) as s:
        await _seed(s, "T-RES", result="no", volume=4)
    report = await _revise(
        session_factory, {"T-RES": _market_payload("T-RES", volume=4, oi=0, result="yes")}
    )
    snaps, _ = await _counts(session_factory, "T-RES")
    assert len(snaps) == 2
    assert snaps[0].result == "no" and snaps[-1].result == "yes"
    assert report.totals()["result_revisions"] == 1


async def test_multiple_changed_fields_append_one_snapshot(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        await _seed(s, "T-MULTI", result="no", volume=4, open_interest=4)
    report = await _revise(
        session_factory, {"T-MULTI": _market_payload("T-MULTI", volume=150, oi=150, result="yes")}
    )
    snaps, _ = await _counts(session_factory, "T-MULTI")
    assert len(snaps) == 2  # one row, not three
    t = report.totals()
    assert t["snapshots_appended"] == 1
    assert t["volume_revisions"] == t["open_interest_revisions"] == t["result_revisions"] == 1


async def test_unchanged_response_appends_no_snapshot(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Records the verification without polluting the snapshot history."""
    async with session_scope(session_factory) as s:
        await _seed(s, "T-SAME", volume=42, open_interest=42, result="no")
    report = await _revise(
        session_factory, {"T-SAME": _market_payload("T-SAME", volume=42, oi=42, result="no")}
    )
    snaps, vers = await _counts(session_factory, "T-SAME")
    assert len(snaps) == 1  # unchanged: nothing appended
    assert len(vers) == 1 and vers[0].outcome == "unchanged"
    assert report.totals()["unchanged"] == 1
    assert report.totals()["snapshots_appended"] == 0


async def test_repeat_run_is_idempotent(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        await _seed(s, "T-IDEM", volume=0)
    payload = {"T-IDEM": _market_payload("T-IDEM", volume=646, oi=0, result="no")}
    await _revise(session_factory, payload)
    second = await _revise(session_factory, payload)
    snaps, vers = await _counts(session_factory, "T-IDEM")
    assert len(snaps) == 2  # still two: the market left the queue after verification
    assert len(vers) == 1
    assert second.totals()["candidates_selected"] == 0
    assert second.totals()["attempted"] == 0


async def test_historical_snapshot_is_never_mutated(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        await _seed(s, "T-KEEP", volume=0, result="no")
    async with session_scope(session_factory) as s:
        before = (
            await s.scalars(select(MarketSnapshot).where(MarketSnapshot.market_ticker == "T-KEEP"))
        ).all()
        original = (before[0].id, before[0].volume, before[0].result, before[0].content_hash)
    await _revise(
        session_factory, {"T-KEEP": _market_payload("T-KEEP", volume=999, oi=0, result="yes")}
    )
    snaps, _ = await _counts(session_factory, "T-KEEP")
    assert (snaps[0].id, snaps[0].volume, snaps[0].result, snaps[0].content_hash) == original


# --- Terminal and failure handling -------------------------------------------


async def test_transient_api_failure_is_retried_next_cycle(session_factory) -> None:  # type: ignore[no-untyped-def]
    """No verification row on a 500, so the market stays in the queue."""
    async with session_scope(session_factory) as s:
        await _seed(s, "T-FLAKY", volume=0)
    report = await _revise(session_factory, {"T-FLAKY": 500})
    snaps, vers = await _counts(session_factory, "T-FLAKY")
    assert report.totals()["api_failure"] == 1
    assert len(snaps) == 1 and vers == []
    async with session_scope(session_factory) as s:
        still = await select_revision_candidates(s, now=NOW)
    assert [t for t, _ in still] == ["T-FLAKY"]

    recovered = await _revise(
        session_factory, {"T-FLAKY": _market_payload("T-FLAKY", volume=7, oi=0, result="no")}
    )
    assert recovered.totals()["changed"] == 1


async def test_recent_removed_market_is_terminal(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        await _seed(s, "T-GONE", close_time=datetime(2026, 7, 30, 12, 0, tzinfo=UTC))
    report = await _revise(session_factory, {"T-GONE": 404}, retention_days=67)
    _, vers = await _counts(session_factory, "T-GONE")
    assert report.totals()["market_removed"] == 1
    assert vers[0].outcome == "market_removed"
    async with session_scope(session_factory) as s:
        assert await select_revision_candidates(s, now=NOW) == []


async def test_old_market_404_is_classified_retention_expired(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        await _seed(
            s,
            "T-AGED",
            close_time=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            expiration_time=datetime(2026, 1, 8, 12, 0, tzinfo=UTC),
        )
    report = await _revise(session_factory, {"T-AGED": 404}, retention_days=67)
    _, vers = await _counts(session_factory, "T-AGED")
    assert report.totals()["retention_expired"] == 1
    assert vers[0].outcome == "retention_expired"


# --- Metrics and loop ---------------------------------------------------------


async def test_metrics_are_complete_and_correct(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        await _seed(s, "M-CHANGED", volume=0)
        await _seed(s, "M-SAME", volume=9, open_interest=9)
        await _seed(s, "M-GONE", close_time=datetime(2026, 7, 30, 12, 0, tzinfo=UTC))
        await _seed(s, "M-FLAKY", volume=1)
    report = await _revise(
        session_factory,
        {
            "M-CHANGED": _market_payload("M-CHANGED", volume=5, oi=0, result="no"),
            "M-SAME": _market_payload("M-SAME", volume=9, oi=9, result="no"),
            "M-GONE": 404,
            "M-FLAKY": 500,
        },
    )
    t = report.totals()
    assert t["candidates_selected"] == 4
    assert t["attempted"] == 4
    assert t["changed"] == 1
    assert t["unchanged"] == 1
    assert t["snapshots_appended"] == 1
    assert t["volume_revisions"] == 1
    assert t["market_removed"] == 1
    assert t["api_failure"] == 1
    assert set(t) >= {
        "candidates_selected",
        "attempted",
        "changed",
        "unchanged",
        "snapshots_appended",
        "result_revisions",
        "volume_revisions",
        "open_interest_revisions",
        "market_removed",
        "retention_expired",
        "api_failure",
        "errors",
    }


async def test_dry_run_writes_nothing(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as s:
        await _seed(s, "T-DRY", volume=0)
    report = await _revise(
        session_factory,
        {"T-DRY": _market_payload("T-DRY", volume=646, oi=0, result="no")},
        dry_run=True,
    )
    snaps, vers = await _counts(session_factory, "T-DRY")
    assert report.totals()["changed"] == 1
    assert len(snaps) == 1 and vers == []


async def test_loop_records_collector_run_and_makes_progress(session_factory) -> None:  # type: ignore[no-untyped-def]
    # The loop uses its own utc_now(), so these must be final in real time.
    past_close = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    past_final = datetime(2026, 1, 8, 12, 0, tzinfo=UTC)
    async with session_scope(session_factory) as s:
        await _seed(s, "L-ONE", volume=0, close_time=past_close, expiration_time=past_final)
        await _seed(s, "L-TWO", volume=0, close_time=past_close, expiration_time=past_final)
    responses = {
        "L-ONE": _market_payload(
            "L-ONE", volume=11, oi=0, result="no", close=past_close, expiration=past_final
        ),
        "L-TWO": _market_payload(
            "L-TWO", volume=22, oi=0, result="no", close=past_close, expiration=past_final
        ),
    }
    await run_metadata_revision_loop(
        session_factory=session_factory,
        client_factory=lambda: _client(_handler(responses)),
        limit_per_cycle=1,
        interval_seconds=0,
        stop_event=asyncio.Event(),
        max_cycles=2,
    )
    async with session_scope(session_factory) as s:
        runs = (await s.scalars(select(CollectorRun).order_by(CollectorRun.id))).all()
        vers = (await s.scalars(select(MarketMetadataVerification))).all()
    assert [r.collector for r in runs] == ["revision", "revision"]
    assert runs[0].stats_json["attempted"] == 1
    # cycle 2 reached the *other* market rather than re-selecting the first
    assert {v.market_ticker for v in vers} == {"L-ONE", "L-TWO"}


async def test_unrelated_field_change_appends_but_is_not_reported_as_a_revision(  # type: ignore[no-untyped-def]
    session_factory,
) -> None:
    """Regression for a bug found by bounded live validation.

    `expiration_time` is part of the content hash and is NULL on every row
    written before migration 0008, so the first verification of a historical
    market legitimately appends a snapshot even when nothing venue-mutable
    moved. Deriving `outcome` from "did we append" reported that as `changed`
    with no `changed_fields`, which would overstate result/volume revision
    counts across essentially the whole backfill. `outcome` must track the
    mutable fields; `snapshot_appended` separately tracks the write.
    """
    async with session_scope(session_factory) as s:
        await _seed(s, "T-NULLEXP", volume=42, open_interest=42, result="no", expiration_time=None)
    report = await _revise(
        session_factory,
        {"T-NULLEXP": _market_payload("T-NULLEXP", volume=42, oi=42, result="no")},
    )
    snaps, vers = await _counts(session_factory, "T-NULLEXP")

    assert len(snaps) == 2  # appended: expiration_time is now populated
    assert snaps[-1].expiration_time is not None
    t = report.totals()
    assert t["snapshots_appended"] == 1
    assert t["changed"] == 0 and t["unchanged"] == 1
    assert t["result_revisions"] == t["volume_revisions"] == 0
    assert vers[0].outcome == "unchanged" and vers[0].snapshot_appended is True
