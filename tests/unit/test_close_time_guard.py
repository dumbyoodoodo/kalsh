"""Close-time revision guard: revision semantics, deterministic ordering,
environment provenance, decision policy, and isolation. Pure synthetic
fixtures -- no database, no outcomes, no metrics."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kalshi_weather.research.close_time_guard import (
    BLOCKED_INSUFFICIENT_HISTORY,
    PASS,
    PASS_WITH_NULLS,
    REVIEW_REQUIRED,
    GuardInputError,
    SnapshotLite,
    analyze_close_times,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
CLOSE_A = datetime(2026, 8, 13, 7, 0, tzinfo=UTC)
CLOSE_B = CLOSE_A + timedelta(hours=2)


def snap(
    ticker: str,
    sid: int,
    obs_minutes: int,
    close: datetime | None,
    env: str | None = "production",
) -> SnapshotLite:
    return SnapshotLite(
        ticker=ticker,
        snapshot_id=sid,
        observed_at=NOW + timedelta(minutes=obs_minutes),
        close_time=close,
        environment=env,
    )


def run(snaps, **kw):  # type: ignore[no-untyped-def]
    defaults = {"name": "test", "as_of": NOW, "horizon_hours": 24.0}
    defaults.update(kw)
    return analyze_close_times(snaps, **defaults)  # type: ignore[arg-type]


# --- stability ---------------------------------------------------------------


def test_single_stable_close_time_passes() -> None:
    r = run([snap("T1", 1, 0, CLOSE_A)])
    assert r.status == PASS and r.tickers_stable == 1 and r.tickers_revised == 0


def test_repeated_identical_close_times_are_not_revisions() -> None:
    r = run([snap("T1", 1, 0, CLOSE_A), snap("T1", 2, 10, CLOSE_A), snap("T1", 3, 20, CLOSE_A)])
    assert r.status == PASS and r.tickers_stable == 1
    assert r.max_revision_seconds == 0.0


def test_duplicate_snapshots_same_second_deterministic_ordering() -> None:
    # same observed_at: primary key breaks the tie deterministically
    a = SnapshotLite("T1", 2, NOW, CLOSE_B, "production")
    b = SnapshotLite("T1", 1, NOW, CLOSE_A, "production")
    r1 = run([a, b])
    r2 = run([b, a])
    assert r1.revisions[0].versions[0].close_time == CLOSE_A  # id 1 first
    assert r1.to_dict()["revisions"] == r2.to_dict()["revisions"]  # order-independent


# --- revisions ---------------------------------------------------------------


def test_single_forward_revision_requires_review() -> None:
    r = run([snap("T1", 1, 0, CLOSE_A), snap("T1", 2, 60, CLOSE_B)])
    assert r.status == REVIEW_REQUIRED and r.tickers_revised == 1
    rev = r.revisions[0]
    assert rev.direction == "forward"
    assert rev.max_shift_seconds == 7200.0
    assert r.max_revision_seconds == 7200.0
    # provenance of each version preserved
    assert rev.versions[0].first_snapshot_id == 1
    assert rev.versions[1].first_snapshot_id == 2


def test_backward_and_mixed_revisions() -> None:
    r = run([snap("T1", 1, 0, CLOSE_B), snap("T1", 2, 60, CLOSE_A)])
    assert r.revisions[0].direction == "backward"
    r2 = run(
        [
            snap("T1", 1, 0, CLOSE_A),
            snap("T1", 2, 60, CLOSE_B),
            snap("T1", 3, 120, CLOSE_A - timedelta(hours=1)),
        ]
    )
    assert r2.revisions[0].direction == "mixed"
    assert len(r2.revisions[0].versions) == 3


def test_revision_changes_derived_decision_time() -> None:
    r = run([snap("T1", 1, 0, CLOSE_A), snap("T1", 2, 60, CLOSE_B)], horizon_hours=24.0)
    (old, new), = r.revisions[0].decision_time_changes
    assert old == (CLOSE_A - timedelta(hours=24)).isoformat()
    assert new == (CLOSE_B - timedelta(hours=24)).isoformat()


def test_ticker_isolation() -> None:
    # different tickers with different close_times are NOT revisions
    r = run([snap("T1", 1, 0, CLOSE_A), snap("T2", 2, 10, CLOSE_B)])
    assert r.status == PASS and r.tickers_stable == 2


# --- nulls -------------------------------------------------------------------


def test_null_close_time_with_frozen_exclusion_passes_with_nulls() -> None:
    r = run([snap("T1", 1, 0, CLOSE_A), snap("T2", 2, 0, None)])
    assert r.status == PASS_WITH_NULLS
    assert r.tickers_null_close == 1 and "T2" in r.null_tickers


def test_null_without_frozen_exclusion_requires_review() -> None:
    r = run([snap("T2", 1, 0, None)], nulls_excluded_by_frozen_rules=False)
    assert r.status == REVIEW_REQUIRED


def test_revision_outranks_nulls() -> None:
    r = run([snap("T1", 1, 0, CLOSE_A), snap("T1", 2, 5, CLOSE_B), snap("T2", 3, 0, None)])
    assert r.status == REVIEW_REQUIRED


# --- provenance / blocking ---------------------------------------------------


def test_ambiguous_environment_blocks() -> None:
    r = run([snap("T1", 1, 0, CLOSE_A, env="unknown")])
    assert r.status == BLOCKED_INSUFFICIENT_HISTORY
    assert "T1" in r.blocked_tickers
    # a ticker with at least one unambiguous row is analyzable
    r2 = run([snap("T1", 1, 0, CLOSE_A, env="unknown"), snap("T1", 2, 5, CLOSE_A)])
    assert r2.status == PASS


def test_expected_ticker_absent_from_history_blocks() -> None:
    r = run([snap("T1", 1, 0, CLOSE_A)], expected_tickers=["T1", "TMISSING"])
    assert r.status == BLOCKED_INSUFFICIENT_HISTORY
    assert "TMISSING" in r.blocked_tickers


def test_eligibility_predicate_scopes_tickers() -> None:
    r = run(
        [snap("KXHIGHNY-X", 1, 0, CLOSE_A), snap("KXRAIN-X", 2, 0, CLOSE_B)],
        eligible=lambda t: t.startswith("KXHIGHNY"),
    )
    assert r.tickers_inspected == 1 and r.status == PASS


def test_empty_ticker_fails_closed() -> None:
    with pytest.raises(GuardInputError):
        run([SnapshotLite("", 1, NOW, CLOSE_A, "production")])


def test_naive_timestamps_normalized_as_utc() -> None:
    naive = SnapshotLite(
        "T1", 1, NOW.replace(tzinfo=None), CLOSE_A.replace(tzinfo=None), "production"
    )
    aware = SnapshotLite("T1", 2, NOW, CLOSE_A, "production")
    r = run([naive, aware])
    assert r.status == PASS and r.tickers_stable == 1  # same instant, one version


# --- machine-readable output -------------------------------------------------


def test_report_dict_shape() -> None:
    r = run([snap("T1", 1, 0, CLOSE_A), snap("T1", 2, 60, CLOSE_B)])
    d = r.to_dict()
    assert d["rule_id"] == "R013-close-time-stability"
    assert d["status"] == REVIEW_REQUIRED
    assert d["revisions"][0]["ticker"] == "T1"
    assert d["revisions"][0]["versions"][0]["first_snapshot_id"] == 1
    assert "policy" in d["detail"]


# --- isolation ---------------------------------------------------------------


def test_module_never_touches_outcomes_or_metrics() -> None:
    src = (
        Path(__file__).resolve().parents[2]
        / "src/kalshi_weather/research/close_time_guard.py"
    ).read_text()
    banned = re.compile(
        r"result|settlement_ts|price|brier|log_loss|fit_|prediction|kalshi_weather\.paper"
        r"|kalshi_weather\.kalshi|kalshi_weather\.experiments|sqlalchemy|datetime\.now|utcnow",
        re.IGNORECASE,
    )
    m = banned.search(src)
    assert m is None, f"guard references banned symbol: {m.group(0)!r}"
    assert "SnapshotLite" in src  # metadata-only input type is the contract