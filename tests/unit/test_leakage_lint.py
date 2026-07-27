"""Leakage linter: every rule's positive and negative case, fail-closed
input handling, and module isolation. Synthetic fixtures only."""

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from kalshi_weather.research.leakage_lint import (
    LintError,
    LintFinding,
    LintReport,
    Severity,
    check_backfill_flagged,
    check_contract_declared,
    check_excluded_window,
    check_feature_after_decision,
    check_group_partition_isolation,
    check_label_after_cutoff,
    check_local_date_derivation,
    check_no_terminal_columns,
    check_provenance,
    check_publication_after_decision,
    check_universe_source,
    scan_latest_state,
)

T0 = datetime(2026, 7, 28, 12, 0)


def test_feature_after_decision_flags_lookahead() -> None:
    rows = [
        {"key": "ok", "decision_time": T0, "observed_at": T0 - timedelta(hours=1)},
        {"key": "bad", "decision_time": T0, "observed_at": T0 + timedelta(minutes=1)},
    ]
    f = check_feature_after_decision(rows, availability_field="observed_at")
    assert f is not None and f.severity is Severity.ERROR and f.count == 1
    assert "bad" in f.samples
    ok = check_feature_after_decision(rows[:1], availability_field="observed_at")
    assert ok is None


def test_publication_after_decision_rule() -> None:
    rows = [{"key": "x", "decision_time": T0, "issue_time": T0 + timedelta(hours=2)}]
    f = check_publication_after_decision(rows)
    assert f is not None and f.rule_id == "R002"


def test_missing_field_fails_closed() -> None:
    with pytest.raises(LintError):
        check_feature_after_decision([{"decision_time": T0}], availability_field="observed_at")


def test_contract_mixing_detected() -> None:
    assert check_contract_declared("ingestion", ["observed_at"]) is None
    f = check_contract_declared("ingestion", ["observed_at", "issue_time"])
    assert f is not None and f.rule_id == "R003"
    f2 = check_contract_declared("publication", ["observed_at"])
    assert f2 is not None
    with pytest.raises(LintError):
        check_contract_declared("vibes", [])


def test_label_after_cutoff_rule() -> None:
    ok = [{"key": "a", "feature_cutoff": T0, "label_available_at": T0 + timedelta(days=1)}]
    assert check_label_after_cutoff(ok) is None
    bad = [{"key": "b", "feature_cutoff": T0, "label_available_at": T0}]
    f = check_label_after_cutoff(bad)
    assert f is not None and f.severity is Severity.ERROR


def test_terminal_columns_in_feature_frame() -> None:
    assert check_no_terminal_columns(["fc_point", "mkt_prob"]) is None
    f = check_no_terminal_columns(["fc_point", "kalshi_result"])
    assert f is not None and "kalshi_result" in f.samples


def test_group_partition_isolation() -> None:
    ok = [
        {"event_group": "CHI|tmax|2026-07-01", "partition": "train"},
        {"event_group": "CHI|tmax|2026-07-01", "partition": "train"},
        {"event_group": "CHI|tmax|2026-07-20", "partition": "test"},
    ]
    assert check_group_partition_isolation(ok) is None
    bad = [*ok, {"event_group": "CHI|tmax|2026-07-01", "partition": "test"}]
    f = check_group_partition_isolation(bad)
    assert f is not None and f.count == 1


def test_excluded_window_rows_flagged() -> None:
    rows = [{"target_date": date(2026, 8, 15)}, {"target_date": date(2026, 9, 10)}]
    f = check_excluded_window(
        rows, start=date(2026, 8, 12), end=date(2026, 8, 25), window_name="H0019 test gap"
    )
    assert f is not None and f.count == 1 and "H0019" in f.message


def test_backfill_flagging_publication_contract_only() -> None:
    rows = [
        {"key": "late", "issue_time": T0, "observed_at": T0 + timedelta(days=3)},
        {
            "key": "marked",
            "issue_time": T0,
            "observed_at": T0 + timedelta(days=3),
            "backfilled": True,
        },
        {"key": "prompt", "issue_time": T0, "observed_at": T0 + timedelta(minutes=40)},
    ]
    f = check_backfill_flagged(rows, max_ingest_delay_hours=24.0, contract="publication")
    assert f is not None and f.count == 1 and "late" in f.samples
    assert check_backfill_flagged(rows, max_ingest_delay_hours=24.0, contract="ingestion") is None


def test_provenance_rule() -> None:
    ok = [{"key": "a", "raw_payload_id": 5}]
    assert check_provenance(ok) is None
    f = check_provenance([{"key": "b", "raw_payload_id": None}])
    assert f is not None and f.rule_id == "R009"


def test_local_date_derivation_catches_utc_truncation() -> None:
    # 2026-07-28 03:00Z is 2026-07-27 in Phoenix (UTC-7): storing the UTC
    # date is the classic truncation-before-conversion bug
    instant = datetime(2026, 7, 28, 3, 0, tzinfo=UTC)
    good = [{"key": "ok", "instant_utc": instant, "timezone": "America/Phoenix",
             "local_date": date(2026, 7, 27)}]
    assert check_local_date_derivation(good) is None
    bad = [{"key": "bad", "instant_utc": instant, "timezone": "America/Phoenix",
            "local_date": date(2026, 7, 28)}]
    f = check_local_date_derivation(bad)
    assert f is not None and f.severity is Severity.ERROR
    with pytest.raises(LintError):
        check_local_date_derivation(
            [{"key": "naive", "instant_utc": instant.replace(tzinfo=None),
              "timezone": "America/Phoenix", "local_date": date(2026, 7, 27)}]
        )


def test_universe_source_rule() -> None:
    assert check_universe_source("append_only_snapshots") is None
    f = check_universe_source("current_active_markets")
    assert f is not None and f.rule_id == "R011" and f.severity is Severity.ERROR


def test_latest_state_scan_and_allowlist() -> None:
    src = (
        "latest = q.order_by(Model.observed_at.desc()).limit(1)\n"
        "strike = m['floor_strike'].drop_nulls().first()\n"
    )
    found = scan_latest_state(src, path="x.py")
    assert len(found) == 2
    allowed = scan_latest_state(src, path="x.py", allowlist=("drop_nulls().first()",))
    assert len(allowed) == 1 and "x.py:1" in allowed[0].samples


def test_report_verdicts() -> None:
    r = LintReport()
    assert r.verdict() == "PASS"
    r.add(LintFinding("RX", Severity.WARNING, "w", 1))
    assert r.verdict() == "PASS_WITH_WARNINGS"
    assert r.verdict(fail_on=Severity.WARNING) == "FAIL"
    r.add(LintFinding("RY", Severity.ERROR, "e", 1))
    assert r.verdict() == "FAIL"
    assert [f["rule_id"] for f in r.to_dict()["findings"]] == ["RX", "RY"]


def test_module_isolation() -> None:
    src = (
        Path(__file__).resolve().parents[2]
        / "src/kalshi_weather/research/leakage_lint.py"
    ).read_text()
    banned = re.compile(
        r"kalshi_weather\.experiments|kalshi_weather\.paper|kalshi_weather\.kalshi"
        r"|brier|log_loss|fit_|sqlalchemy|create_async_engine|datetime\.now|utcnow",
        re.IGNORECASE,
    )
    m = banned.search(src)
    assert m is None, f"leakage_lint references banned symbol: {m.group(0)!r}"
