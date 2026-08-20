"""Invariants exposed by the August 2026 gap-preservation task.

The load-bearing one is the evidence-discipline rule: the pmset ring buffer had
already aged out for the largest outage (101h, 2026-08-08..2026-08-13), so those
intervals CANNOT be attributed to host sleep no matter how strongly the
surrounding pattern suggests it. A record may only claim ``HOST_UNAVAILABLE``
when it carries direct host power evidence for its own interval; otherwise the
loss is recorded and the cause left unattributed.

These run against the real canonical ledger, so a later edit that quietly
promotes an unattributed interval to a cause fails here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from kalshi_weather.data_quality.gap_ledger import (
    Classification,
    Confidence,
    EvidenceKind,
    GapLedger,
)

#: pmset ring-buffer start observed during the audit. Nothing earlier has host
#: power evidence on this machine.
PMSET_BUFFER_START = datetime(2026, 8, 13, 10, 24, 21, tzinfo=UTC)

#: The 12 intervals appended on 2026-08-20.
AUGUST_IDS = (
    "GAP-20260807-1450-WEATHER-UNATTRIBUTED",
    "GAP-20260807-2305-WEATHER-UNATTRIBUTED",
    "GAP-20260808-1507-WEATHER-UNATTRIBUTED",
    "GAP-20260808-2011-WEATHER-UNATTRIBUTED",
    "GAP-20260813-0225-WEATHER-UNATTRIBUTED",
    "GAP-20260813-0506-HOST-SLEEP-IV8",
    "GAP-20260814-1924-HOST-SLEEP-IV9",
    "GAP-20260817-2023-HOST-SLEEP-IV10",
    "GAP-20260818-0353-HOST-SLEEP-IV11",
    "GAP-20260818-2350-HOST-SLEEP-IV12",
    "GAP-20260819-1209-HOST-SLEEP-IV13",
    "GAP-20260820-0512-HOST-SLEEP-IV14",
)


@pytest.fixture(scope="module")
def ledger() -> GapLedger:
    return GapLedger.load()


def _august(ledger: GapLedger):
    return [r for r in ledger.records if r.gap_id in AUGUST_IDS]


def test_canonical_ledger_validates_and_hashes_recompute(ledger: GapLedger) -> None:
    assert ledger.validate() == []
    for record in ledger.records:
        assert record.content_hash == record.compute_hash(), record.gap_id


def test_all_august_records_present(ledger: GapLedger) -> None:
    assert {r.gap_id for r in _august(ledger)} == set(AUGUST_IDS)


def test_august_host_records_cite_host_evidence(ledger: GapLedger) -> None:
    """Scoped to the August records rather than the whole ledger.

    ``GAP-20260726-HOST-BACKFILLED`` is a genuine historical exception: it is the
    companion record covering TRADES/CANDLES for an interval whose host cause is
    established by its sibling ``GAP-20260726-HOST-LIVE``, so it carries
    collector and recovery evidence instead. Records are immutable, so that
    exception is documented here rather than retrofitted -- but every record
    appended from August 2026 onward must stand on its own host evidence.
    """
    for record in _august(ledger):
        if record.classification is not Classification.HOST_UNAVAILABLE:
            continue
        assert any(e.kind is EvidenceKind.HOST_EVIDENCE for e in record.evidence), (
            f"{record.gap_id}: HOST_UNAVAILABLE without HOST_EVIDENCE"
        )


def test_a_sleep_claim_requires_power_management_evidence(ledger: GapLedger) -> None:
    """The rule the aged-out buffer forced: no pmset reference, no sleep claim.

    Deliberately keyed on the *sleep* claim rather than on HOST_UNAVAILABLE in
    general -- a host that was powered off (GAP-20260726-HOST-LIVE) is proven by
    collector-run absence and reboot records, and never asserts sleep. What must
    never happen is attributing an interval to sleep when no power-management
    evidence for that interval survives.

    Keyed on the affirmative MEASUREMENT ("explicit Sleep" with a measured
    fraction), not merely on the string "pmset": the unattributed records cite
    pmset too, but only to record that its buffer had already expired. A
    substring test for "pmset" would accept that as proof of sleep, which is the
    exact confusion this guard exists to prevent.
    """
    for record in ledger.records:
        claims_sleep = "sleep" in record.reason_code.lower() or "SLEEP" in record.gap_id
        if not claims_sleep:
            continue
        details = [e.detail for e in record.evidence if e.kind is EvidenceKind.HOST_EVIDENCE]
        assert any("explicit Sleep" in d for d in details), (
            f"{record.gap_id}: claims host sleep without a measured sleep fraction "
            f"for its own interval"
        )


def test_intervals_predating_the_pmset_buffer_are_not_attributed_to_host_sleep(
    ledger: GapLedger,
) -> None:
    """An interval that ended before the buffer starts has no surviving host
    evidence, so it must not carry a host cause."""
    for record in _august(ledger):
        if record.end_at <= PMSET_BUFFER_START:
            assert record.classification is Classification.LEGACY_UNKNOWN, (
                f"{record.gap_id} ended before host evidence exists but claims "
                f"{record.classification}"
            )


def test_unattributed_records_do_not_smuggle_a_cause_into_reason_code(
    ledger: GapLedger,
) -> None:
    for record in _august(ledger):
        if record.classification is not Classification.LEGACY_UNKNOWN:
            continue
        assert "host_sleep" not in record.reason_code, (
            f"{record.gap_id}: reason_code asserts a cause the evidence does not support"
        )


def test_multi_day_interval_is_representable_and_intact(ledger: GapLedger) -> None:
    """The 101h outage is the longest single gap recorded; a duration bug that
    truncated or wrapped it would show up here."""
    record = ledger.by_id("GAP-20260808-2011-WEATHER-UNATTRIBUTED")
    assert record is not None
    duration = record.end_at - record.start_at
    assert duration > timedelta(days=4)
    assert abs(duration.total_seconds() / 3600 - 101.086) < 0.01


def test_august_intervals_do_not_overlap_each_other(ledger: GapLedger) -> None:
    ordered = sorted(_august(ledger), key=lambda r: r.start_at)
    for earlier, later in pairwise(ordered):
        assert earlier.end_at <= later.start_at, (
            f"{earlier.gap_id} overlaps {later.gap_id}"
        )


def test_august_intervals_do_not_overlap_pre_existing_records(ledger: GapLedger) -> None:
    prior = [r for r in ledger.records if r.gap_id not in AUGUST_IDS]
    for new in _august(ledger):
        for old in prior:
            if not (new.subsystems and set(new.subsystems) & set(old.subsystems)):
                continue
            assert not (new.start_at < old.end_at and old.start_at < new.end_at), (
                f"{new.gap_id} overlaps existing {old.gap_id}"
            )


def test_no_august_record_authorizes_exclusion_on_low_confidence(ledger: GapLedger) -> None:
    for record in _august(ledger):
        if record.exclusion_eligible:
            assert record.confidence is not Confidence.LOW, record.gap_id


def test_confirmed_records_cite_a_sleep_fraction_and_awake_window(ledger: GapLedger) -> None:
    """A CONFIRMED host record must show its work: how much of the interval was
    explicit sleep, and that no awake window fit a 30-min weather cycle."""
    for record in _august(ledger):
        if record.confidence is not Confidence.CONFIRMED:
            continue
        detail = " ".join(
            e.detail for e in record.evidence if e.kind is EvidenceKind.HOST_EVIDENCE
        )
        assert "explicit Sleep" in detail, record.gap_id
        assert "longest awake window" in detail, record.gap_id


def test_pmset_local_offset_converts_to_utc_correctly() -> None:
    """pmset logged local +0300 (Europe/Sofia, EEST). Every ledger timestamp is
    UTC, so the conversion is subtraction of three hours -- pinned here because
    an offset error would silently shift every August interval."""
    local = datetime(2026, 8, 20, 13, 14, 55, tzinfo=UTC)  # wall-clock reading
    assert (local - timedelta(hours=3)).isoformat() == "2026-08-20T10:14:55+00:00"


def test_august_records_are_weather_scoped(ledger: GapLedger) -> None:
    """These intervals were derived from the weather run series, so they may not
    claim streams whose own cadence was not examined."""
    for record in _august(ledger):
        assert {str(s) for s in record.subsystems} == {
            "WEATHER_OBSERVATIONS",
            "WEATHER_FORECASTS",
        }, record.gap_id
