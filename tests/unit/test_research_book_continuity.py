"""Book-continuity classifier: synthetic-fixture coverage of every
classification, fail-closed behavior, deterministic ordering, duration
accounting, invariants, and isolation. No database, no network."""

import itertools
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from kalshi_weather.research.book_continuity import (
    BookMeta,
    Confidence,
    ContinuityConfig,
    IntervalClass,
    PollMeta,
    RunMeta,
    classify_intervals,
)

T0 = datetime(2026, 7, 27, 12, 0, 0)
LEDGER_START = datetime(2026, 7, 27, 2, 52, 20)
AS_OF = datetime(2026, 7, 28, 0, 0, 0)
CONFIG = ContinuityConfig(as_of=AS_OF, poll_ledger_start=LEDGER_START)


def book(
    ticker: str = "KXHIGHTPHX-26JUL27-B85.5",
    sid: int = 1,
    at: datetime = T0,
    chash: str | None = "aaa",
    env: str | None = "production",
) -> BookMeta:
    return BookMeta(
        ticker=ticker, snapshot_id=sid, captured_at=at, content_hash=chash, environment=env
    )


def poll(
    ticker: str = "KXHIGHTPHX-26JUL27-B85.5",
    at: datetime | None = None,
    outcome: str = "succeeded_unchanged",
    env: str | None = "production",
    run_id: int = 100,
    dedup: bool = True,
    persisted: int = 0,
    raw_id: int | None = 5000,
) -> PollMeta:
    at = at or (T0 + timedelta(minutes=5))
    return PollMeta(
        ticker=ticker,
        requested_at=at - timedelta(seconds=1),
        completed_at=at,
        outcome=outcome,
        environment=env,
        collector_run_id=run_id,
        deduplicated=dedup,
        persisted_row_count=persisted,
        raw_payload_id=raw_id,
    )


def run(rid: int = 100, start: datetime | None = None, ok: bool = True) -> RunMeta:
    start = start or (T0 + timedelta(minutes=4))
    return RunMeta(
        run_id=rid, started_at=start, finished_at=start + timedelta(minutes=1), success=ok
    )


def pair(chash2: str = "bbb") -> list[BookMeta]:
    return [book(sid=1), book(sid=2, at=T0 + timedelta(minutes=20), chash=chash2)]


def closed(report):  # first closed interval helper
    return next(iv for iv in report.intervals if iv.next_snapshot_id is not None)


def test_changed_after_successful_poll() -> None:
    polls = [
        poll(at=T0 + timedelta(minutes=20), outcome="succeeded_new_data", dedup=False, persisted=1)
    ]
    iv = closed(classify_intervals(pair(), polls, [run()], CONFIG))
    assert iv.classification is IntervalClass.CHANGED_AFTER_SUCCESSFUL_POLL
    assert iv.confidence is Confidence.HIGH
    assert iv.content_hash_changed is True


def test_unchanged_confirmed_by_successful_poll() -> None:
    polls = [
        poll(at=T0 + timedelta(minutes=5)),
        poll(at=T0 + timedelta(minutes=20), outcome="succeeded_new_data", dedup=False, persisted=1),
    ]
    iv = closed(classify_intervals(pair(), polls, [run()], CONFIG))
    assert iv.classification is IntervalClass.UNCHANGED_CONFIRMED_BY_SUCCESSFUL_POLL
    assert iv.unchanged_confirmations == 1
    assert iv.reason_code == "unchanged_confirmed_in_interval"


def test_failed_poll_interval() -> None:
    polls = [poll(at=T0 + timedelta(minutes=5), outcome="api_failure", dedup=False)]
    iv = closed(classify_intervals(pair(), polls, [run()], CONFIG))
    assert iv.classification is IntervalClass.FAILED_POLL_INTERVAL
    assert iv.confidence is Confidence.HIGH
    assert iv.failed_polls == 1


def test_retry_after_failure_counts_both_and_prefers_success() -> None:
    polls = [
        poll(at=T0 + timedelta(minutes=5), outcome="api_failure", dedup=False),
        poll(at=T0 + timedelta(minutes=6)),
    ]
    iv = closed(classify_intervals(pair(), polls, [run()], CONFIG))
    assert iv.classification is IntervalClass.UNCHANGED_CONFIRMED_BY_SUCCESSFUL_POLL
    assert iv.failed_polls == 1 and iv.successful_polls == 1


def test_no_poll_evidence_within_cadence_vs_omitted() -> None:
    # 20-minute interval, cadence 900s -> omitted; cadence 3600s -> expected
    iv = closed(classify_intervals(pair(), [], [run()], CONFIG))
    assert iv.classification is IntervalClass.NO_DIRECT_POLL_EVIDENCE
    assert iv.reason_code == "ticker_omitted_from_successful_cycles"
    wide = ContinuityConfig(
        as_of=AS_OF, poll_ledger_start=LEDGER_START, expected_cadence_seconds=3600
    )
    iv2 = closed(classify_intervals(pair(), [], [run()], wide))
    assert iv2.reason_code == "within_expected_cadence"
    assert iv2.confidence is Confidence.MEDIUM


def test_collection_gap_no_runs_and_only_failed_runs() -> None:
    iv = closed(classify_intervals(pair(), [], [], CONFIG))
    assert iv.classification is IntervalClass.COLLECTION_GAP
    assert iv.reason_code == "no_collector_runs_in_interval"
    iv2 = closed(classify_intervals(pair(), [], [run(ok=False)], CONFIG))
    assert iv2.classification is IntervalClass.COLLECTION_GAP
    assert iv2.reason_code == "only_failed_collector_runs"


def test_open_interval_never_terminal() -> None:
    report = classify_intervals([book()], [poll()], [run()], CONFIG)
    (iv,) = report.intervals
    assert iv.classification is IntervalClass.OPEN_INTERVAL_NOT_YET_CLASSIFIABLE
    assert iv.confidence is Confidence.UNKNOWN
    assert iv.next_snapshot_id is None
    assert iv.end == AS_OF


def test_legacy_pre_ledger_and_straddle() -> None:
    old = [
        book(sid=1, at=LEDGER_START - timedelta(hours=2)),
        book(sid=2, at=LEDGER_START - timedelta(hours=1), chash="bbb"),
        book(sid=3, at=LEDGER_START + timedelta(hours=1), chash="ccc"),
    ]
    report = classify_intervals(old, [], [run()], CONFIG)
    closed_ivs = [iv for iv in report.intervals if iv.next_snapshot_id is not None]
    assert closed_ivs[0].classification is IntervalClass.LEGACY_PRE_POLL_LEDGER
    assert closed_ivs[0].reason_code == "pre_poll_ledger"
    assert closed_ivs[1].classification is IntervalClass.LEGACY_PRE_POLL_LEDGER
    assert closed_ivs[1].reason_code == "straddles_poll_ledger_start"
    assert closed_ivs[1].confidence is Confidence.LOW


def test_ambiguous_environment() -> None:
    books = [
        book(sid=1, env="demo"),
        book(sid=2, at=T0 + timedelta(minutes=20), chash="bbb", env="demo"),
    ]
    iv = closed(classify_intervals(books, [], [run()], CONFIG))
    assert iv.classification is IntervalClass.AMBIGUOUS_PROVENANCE
    polls = [poll(env="unknown")]
    iv2 = closed(classify_intervals(pair(), polls, [run()], CONFIG))
    assert iv2.classification is IntervalClass.AMBIGUOUS_PROVENANCE
    assert iv2.reason_code == "poll_environment_mismatch"


def test_exact_ticker_isolation() -> None:
    other = poll(ticker="KXHIGHTSEA-26JUL27-B75.5", at=T0 + timedelta(minutes=5))
    iv = closed(classify_intervals(pair(), [other], [run()], CONFIG))
    assert iv.classification is not IntervalClass.UNCHANGED_CONFIRMED_BY_SUCCESSFUL_POLL
    assert iv.poll_attempts == 0


def test_same_second_deterministic_ordering() -> None:
    books = [
        book(sid=2, at=T0, chash="bbb"),
        book(sid=1, at=T0, chash="aaa"),
        book(sid=3, at=T0 + timedelta(minutes=10), chash="ccc"),
    ]
    r1 = classify_intervals(books, [], [run()], CONFIG)
    r2 = classify_intervals(list(reversed(books)), [], [run()], CONFIG)
    assert [iv.prev_snapshot_id for iv in r1.intervals] == [
        iv.prev_snapshot_id for iv in r2.intervals
    ] == [1, 2, 3]


def test_duplicate_content_hash_flagged_not_altered() -> None:
    books = [book(sid=1), book(sid=2, at=T0 + timedelta(minutes=20), chash="aaa")]
    report = classify_intervals(books, [], [run()], CONFIG)
    assert any(v.check == "duplicate_logical_snapshot" for v in report.violations)
    assert len(report.intervals) == 2  # still classified, never dropped


def test_invariants_poll_order_and_future_books() -> None:
    bad_poll = PollMeta(
        ticker="X",
        requested_at=T0,
        completed_at=T0 - timedelta(seconds=5),
        outcome="succeeded_unchanged",
        environment="production",
        collector_run_id=1,
        deduplicated=True,
        persisted_row_count=0,
        raw_payload_id=None,
    )
    future = book(sid=9, at=AS_OF + timedelta(minutes=1))
    report = classify_intervals([future], [bad_poll], [], CONFIG)
    checks = {v.check for v in report.violations}
    assert "poll_completed_before_requested" in checks
    assert "book_captured_after_as_of" in checks


def test_duration_accounting_and_nonoverlap() -> None:
    books = [
        book(sid=1, at=T0),
        book(sid=2, at=T0 + timedelta(minutes=10), chash="bbb"),
        book(sid=3, at=T0 + timedelta(minutes=30), chash="ccc"),
    ]
    report = classify_intervals(books, [], [], CONFIG)
    s = report.summary()
    closed_secs = 10 * 60 + 20 * 60
    open_secs = (AS_OF - (T0 + timedelta(minutes=30))).total_seconds()
    assert s["total_seconds"] == closed_secs + open_secs
    assert s["open_seconds"] == open_secs
    assert s["classified_seconds"] == closed_secs
    # intervals for one ticker never overlap: each start == previous end
    ivs = sorted(report.intervals, key=lambda iv: iv.start)
    for a, b in itertools.pairwise(ivs):
        assert a.end == b.start


def test_summary_json_deterministic() -> None:
    books = pair()
    polls = [poll()]
    a = json.dumps(classify_intervals(books, polls, [run()], CONFIG).summary(), sort_keys=True)
    b = json.dumps(classify_intervals(books, polls, [run()], CONFIG).summary(), sort_keys=True)
    assert a == b


def test_reason_codes_stable_vocabulary() -> None:
    assert {c.value for c in IntervalClass} == {
        "CHANGED_AFTER_SUCCESSFUL_POLL",
        "UNCHANGED_CONFIRMED_BY_SUCCESSFUL_POLL",
        "FAILED_POLL_INTERVAL",
        "NO_DIRECT_POLL_EVIDENCE",
        "COLLECTION_GAP",
        "AMBIGUOUS_PROVENANCE",
        "LEGACY_PRE_POLL_LEDGER",
        "OPEN_INTERVAL_NOT_YET_CLASSIFIABLE",
    }


def test_module_isolation() -> None:
    src = (
        Path(__file__).resolve().parents[2]
        / "src/kalshi_weather/research/book_continuity.py"
    ).read_text()
    banned_imports = re.compile(
        r"from kalshi_weather\.(experiments|paper|execution|kalshi|storage|models)"
        r"|import kalshi_weather\.(experiments|paper|execution|kalshi|storage|models)"
    )
    assert banned_imports.search(src) is None
    # no outcome/metric/price fields; no ledger writes
    banned_terms = re.compile(
        r"kalshi_result|expiration_value|yes_bid|price_close|brier|append_record",
        re.IGNORECASE,
    )
    assert banned_terms.search(src) is None


def test_cli_block_read_only() -> None:
    src = (Path(__file__).resolve().parents[2] / "src/kalshi_weather/cli.py").read_text()
    block = src.split("def research_book_continuity")[1].split("\n\n\n")[0]
    assert re.search(r"insert |update |delete |create table|append_record", block, re.I) is None
    assert "READ-ONLY" in block
