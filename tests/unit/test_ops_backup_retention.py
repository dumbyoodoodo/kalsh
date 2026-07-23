from datetime import datetime, timedelta
from pathlib import Path

from kalshi_weather.ops.backup_retention import (
    BackupFile,
    apply_partial_cleanup,
    apply_retention_plan,
    compute_retention_plan,
    find_stale_partial_files,
    list_backup_files,
    parse_backup_filename,
)

# Naive UTC throughout -- matches this codebase's established convention
# (domain.time.to_naive_utc) and what parse_backup_filename/list_backup_files
# actually produce in production; a prior version of this module returned
# aware datetimes here, which caused a real "can't subtract offset-naive
# and offset-aware datetimes" crash the first time this code ran live
# against the real backup directory.
NOW = datetime(2026, 7, 23, 0, 0, 0)


def _bf(days_ago: int, size: int = 100) -> BackupFile:
    ts = NOW - timedelta(days=days_ago)
    name = f"kalshi_weather-{ts.strftime('%Y%m%dT%H%M%SZ')}.dump"
    return BackupFile(path=Path(f"/backups/{name}"), timestamp=ts, size_bytes=size)


def test_parse_backup_filename_valid() -> None:
    ts = parse_backup_filename("kalshi_weather-20260722T034116Z.dump")
    assert ts == datetime(2026, 7, 22, 3, 41, 16)
    assert ts.tzinfo is None


def test_parse_backup_filename_rejects_unrelated_names() -> None:
    assert parse_backup_filename("readme.txt") is None
    assert parse_backup_filename("kalshi_weather-20260722T034116Z.dump.partial") is None
    assert parse_backup_filename("._kalshi_weather-20260722T034116Z.dump") is None


def test_list_backup_files_filters_and_sorts(tmp_path: Path) -> None:
    (tmp_path / "kalshi_weather-20260722T000000Z.dump").write_bytes(b"a")
    (tmp_path / "kalshi_weather-20260721T000000Z.dump").write_bytes(b"bb")
    (tmp_path / "unrelated.txt").write_text("x")
    (tmp_path / "kalshi_weather-20260722T000000Z.dump.partial").write_bytes(b"partial")
    files = list_backup_files(tmp_path)
    assert [f.path.name for f in files] == [
        "kalshi_weather-20260721T000000Z.dump",
        "kalshi_weather-20260722T000000Z.dump",
    ]


def test_list_backup_files_missing_directory() -> None:
    assert list_backup_files(Path("/does/not/exist")) == []


def test_find_stale_partial_files(tmp_path: Path) -> None:
    stale = tmp_path / "kalshi_weather-20260722T000000Z.dump.partial"
    fresh = tmp_path / "kalshi_weather-20260723T000000Z.dump.partial"
    stale.write_bytes(b"x")
    fresh.write_bytes(b"x")
    import os
    from datetime import UTC

    # NOW is naive-UTC (matches production); os.utime needs a real epoch
    # timestamp, so this one conversion must explicitly attach UTC rather
    # than let .timestamp() assume the test machine's local timezone.
    old_time = (NOW - timedelta(hours=5)).replace(tzinfo=UTC).timestamp()
    os.utime(stale, (old_time, old_time))
    result = find_stale_partial_files(tmp_path, now=NOW, grace_hours=2.0)
    assert result == [stale]


# --- compute_retention_plan -----------------------------------------------------


def test_empty_input_aborts() -> None:
    plan = compute_retention_plan([], now=NOW)
    assert plan.aborted is True
    assert plan.decisions == ()


def test_single_file_is_always_kept_as_newest() -> None:
    f = _bf(days_ago=1000)  # far older than any tier
    plan = compute_retention_plan([f], now=NOW)
    assert len(plan.decisions) == 1
    assert plan.decisions[0].action == "keep"
    assert "newest" in plan.decisions[0].reason


def test_newest_file_never_deleted_even_if_very_old() -> None:
    files = [_bf(2000), _bf(1000)]  # both ancient; the newest of the two must survive
    plan = compute_retention_plan(files, now=NOW)
    newest_decision = next(d for d in plan.decisions if d.file.timestamp == files[1].timestamp)
    assert newest_decision.action == "keep"
    assert "newest" in newest_decision.reason


def test_within_daily_window_all_kept() -> None:
    files = [_bf(0), _bf(5), _bf(13)]
    plan = compute_retention_plan(files, now=NOW, daily_days=14)
    assert all(d.action == "keep" for d in plan.decisions)


def test_weekly_tier_keeps_one_representative_per_week() -> None:
    # Two backups in the same ISO week, both beyond the 14-day daily window
    # but within the weekly tier -- only the earlier one should survive.
    earlier = _bf(20)
    later = _bf(19)
    # Ensure they land in the same ISO week for a deterministic test.
    if earlier.timestamp.isocalendar()[:2] != later.timestamp.isocalendar()[:2]:
        later = BackupFile(
            path=later.path, timestamp=earlier.timestamp + timedelta(days=1), size_bytes=100
        )
    files = [earlier, later, _bf(0)]  # _bf(0) is the newest, always kept
    plan = compute_retention_plan(files, now=NOW, daily_days=14, weekly_weeks=8)
    earlier_decision = next(d for d in plan.decisions if d.file is earlier)
    later_decision = next(d for d in plan.decisions if d.file is later)
    assert earlier_decision.action == "keep"
    assert later_decision.action == "delete"


def test_monthly_tier_keeps_one_representative_per_month() -> None:
    earlier = _bf(80)
    later = _bf(79)
    if (earlier.timestamp.year, earlier.timestamp.month) != (
        later.timestamp.year,
        later.timestamp.month,
    ):
        later = BackupFile(
            path=later.path, timestamp=earlier.timestamp + timedelta(days=1), size_bytes=100
        )
    files = [earlier, later, _bf(0)]
    plan = compute_retention_plan(files, now=NOW, daily_days=14, weekly_weeks=8, monthly_months=12)
    earlier_decision = next(d for d in plan.decisions if d.file is earlier)
    later_decision = next(d for d in plan.decisions if d.file is later)
    assert earlier_decision.action == "keep"
    assert later_decision.action == "delete"


def test_beyond_monthly_window_is_deleted() -> None:
    ancient = _bf(500)  # well beyond 14d + 8w + 12mo = 430d
    files = [ancient, _bf(0)]
    plan = compute_retention_plan(files, now=NOW)
    ancient_decision = next(d for d in plan.decisions if d.file is ancient)
    assert ancient_decision.action == "delete"
    assert "monthly retention window" in ancient_decision.reason


def test_compute_retention_plan_is_deterministic() -> None:
    files = [_bf(0), _bf(5), _bf(20), _bf(80), _bf(400)]
    plan1 = compute_retention_plan(files, now=NOW)
    plan2 = compute_retention_plan(files, now=NOW)
    assert [(d.file.path, d.action, d.reason) for d in plan1.decisions] == [
        (d.file.path, d.action, d.reason) for d in plan2.decisions
    ]


def test_to_delete_and_to_keep_properties() -> None:
    files = [_bf(0), _bf(500)]
    plan = compute_retention_plan(files, now=NOW)
    assert len(plan.to_keep) == 1
    assert len(plan.to_delete) == 1


# --- apply_retention_plan / apply_partial_cleanup ------------------------------


def test_apply_retention_plan_dry_run_does_not_delete(tmp_path: Path) -> None:
    old_file = tmp_path / "kalshi_weather-20250101T000000Z.dump"
    old_file.write_bytes(b"x")
    files = [
        BackupFile(path=old_file, timestamp=datetime(2025, 1, 1), size_bytes=1),
        _bf(0),
    ]
    plan = compute_retention_plan(files, now=NOW)
    log = apply_retention_plan(plan, dry_run=True)
    assert old_file.exists()
    delete_entries = [e for e in log if e.action == "delete"]
    assert delete_entries
    assert all(e.dry_run and not e.deleted for e in delete_entries)


def test_apply_retention_plan_real_run_deletes(tmp_path: Path) -> None:
    old_file = tmp_path / "kalshi_weather-20250101T000000Z.dump"
    old_file.write_bytes(b"x")
    files = [
        BackupFile(path=old_file, timestamp=datetime(2025, 1, 1), size_bytes=1),
        _bf(0),
    ]
    plan = compute_retention_plan(files, now=NOW)
    log = apply_retention_plan(plan, dry_run=False)
    assert not old_file.exists()
    delete_entries = [e for e in log if e.action == "delete"]
    assert all(not e.dry_run and e.deleted for e in delete_entries)


def test_apply_retention_plan_aborted_returns_empty_log() -> None:
    plan = compute_retention_plan([], now=NOW)
    assert apply_retention_plan(plan, dry_run=False) == []


def test_apply_partial_cleanup_dry_run(tmp_path: Path) -> None:
    partial = tmp_path / "kalshi_weather-20260722T000000Z.dump.partial"
    partial.write_bytes(b"x")
    log = apply_partial_cleanup([partial], dry_run=True)
    assert partial.exists()
    assert log[0].dry_run is True
    assert log[0].deleted is False


def test_apply_partial_cleanup_real_run(tmp_path: Path) -> None:
    partial = tmp_path / "kalshi_weather-20260722T000000Z.dump.partial"
    partial.write_bytes(b"x")
    log = apply_partial_cleanup([partial], dry_run=False)
    assert not partial.exists()
    assert log[0].deleted is True
