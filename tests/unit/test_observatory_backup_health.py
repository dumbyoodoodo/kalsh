from datetime import datetime, timedelta
from pathlib import Path

from kalshi_weather.observatory.backup_health import (
    BackupHealthConfig,
    DiskUsage,
    build_backup_findings,
    check_backup_command,
    check_backup_freshness,
    check_disk_space,
    check_lock_contention,
    check_remote_copy,
    check_remote_staleness,
    load_disk_usage,
    load_skip_count,
)
from kalshi_weather.observatory.severity import Severity
from kalshi_weather.ops.backup import STATUS_FILENAME, BackupStatus, write_backup_status
from kalshi_weather.ops.backup_retention import BackupFile

NOW = datetime(2026, 7, 23, 0, 0, 0)


def _bf(hours_ago: float) -> BackupFile:
    return BackupFile(
        path=Path(f"/x-{hours_ago}.dump"), timestamp=NOW - timedelta(hours=hours_ago), size_bytes=1
    )


def _status(**overrides: object) -> BackupStatus:
    defaults: dict[str, object] = dict(
        schema=1,
        timestamp=NOW.isoformat(),
        local_outcome="success",
        local_path="/x.dump",
        size_bytes=100,
        entries=20,
        duration_seconds=1.0,
        error=None,
        remote_outcome="success",
        remote_detail="ok",
    )
    defaults.update(overrides)
    return BackupStatus(**defaults)  # type: ignore[arg-type]


# --- check_backup_command --------------------------------------------------------


def test_check_backup_command_no_status_is_info() -> None:
    assert check_backup_command(None).severity == Severity.INFO


def test_check_backup_command_failed_is_critical() -> None:
    finding = check_backup_command(_status(local_outcome="failed", error="pg_dump failed"))
    assert finding.severity == Severity.CRITICAL
    assert "pg_dump failed" in finding.message


def test_check_backup_command_success_is_info() -> None:
    assert check_backup_command(_status()).severity == Severity.INFO


# --- check_backup_freshness -------------------------------------------------------


def test_check_backup_freshness_no_files_is_info() -> None:
    assert check_backup_freshness([], now=NOW, stale_after_hours=30.0).severity == Severity.INFO


def test_check_backup_freshness_fresh_is_info() -> None:
    finding = check_backup_freshness([_bf(1)], now=NOW, stale_after_hours=30.0)
    assert finding.severity == Severity.INFO


def test_check_backup_freshness_stale_is_warning() -> None:
    finding = check_backup_freshness([_bf(40)], now=NOW, stale_after_hours=30.0)
    assert finding.severity == Severity.WARNING


def test_check_backup_freshness_very_stale_is_critical() -> None:
    finding = check_backup_freshness([_bf(100)], now=NOW, stale_after_hours=30.0)
    assert finding.severity == Severity.CRITICAL


def test_check_backup_freshness_uses_newest_file() -> None:
    finding = check_backup_freshness([_bf(100), _bf(1)], now=NOW, stale_after_hours=30.0)
    assert finding.severity == Severity.INFO


# --- check_remote_copy -------------------------------------------------------------


def test_check_remote_copy_no_status_is_info() -> None:
    assert check_remote_copy(None, remote_configured=True).severity == Severity.INFO


def test_check_remote_copy_success_is_info() -> None:
    finding = check_remote_copy(_status(remote_outcome="success"), remote_configured=True)
    assert finding.severity == Severity.INFO


def test_check_remote_copy_failed_is_warning() -> None:
    finding = check_remote_copy(_status(remote_outcome="failed"), remote_configured=True)
    assert finding.severity == Severity.WARNING


def test_check_remote_copy_not_configured_is_info_when_remote_not_configured() -> None:
    finding = check_remote_copy(_status(remote_outcome="not_configured"), remote_configured=False)
    assert finding.severity == Severity.INFO


def test_check_remote_copy_not_configured_but_should_be_is_warning() -> None:
    # remote_configured=True but the status still says not_configured --
    # e.g. config was just enabled and no run has attempted it under the
    # new config yet, or a genuine drift between config and status.
    finding = check_remote_copy(_status(remote_outcome="not_configured"), remote_configured=True)
    assert finding.severity == Severity.WARNING


# --- check_remote_staleness --------------------------------------------------------


def test_check_remote_staleness_not_configured_is_info() -> None:
    finding = check_remote_staleness(None, remote_configured=False, now=NOW, stale_after_hours=48.0)
    assert finding.severity == Severity.INFO


def test_check_remote_staleness_configured_but_never_seen_is_warning() -> None:
    finding = check_remote_staleness(None, remote_configured=True, now=NOW, stale_after_hours=48.0)
    assert finding.severity == Severity.WARNING


def test_check_remote_staleness_fresh_is_info() -> None:
    finding = check_remote_staleness(
        NOW - timedelta(hours=1), remote_configured=True, now=NOW, stale_after_hours=48.0
    )
    assert finding.severity == Severity.INFO


def test_check_remote_staleness_stale_is_warning() -> None:
    finding = check_remote_staleness(
        NOW - timedelta(hours=60), remote_configured=True, now=NOW, stale_after_hours=48.0
    )
    assert finding.severity == Severity.WARNING


def test_check_remote_staleness_very_stale_is_critical() -> None:
    finding = check_remote_staleness(
        NOW - timedelta(hours=200), remote_configured=True, now=NOW, stale_after_hours=48.0
    )
    assert finding.severity == Severity.CRITICAL


# --- check_disk_space ---------------------------------------------------------------


def test_check_disk_space_plenty_is_info() -> None:
    usage = DiskUsage(free_bytes=100 * 1024**3, total_bytes=200 * 1024**3)
    finding = check_disk_space(usage, warning_free_gb=20.0, critical_free_gb=5.0)
    assert finding.severity == Severity.INFO


def test_check_disk_space_low_is_warning() -> None:
    usage = DiskUsage(free_bytes=10 * 1024**3, total_bytes=200 * 1024**3)
    finding = check_disk_space(usage, warning_free_gb=20.0, critical_free_gb=5.0)
    assert finding.severity == Severity.WARNING


def test_check_disk_space_critical() -> None:
    usage = DiskUsage(free_bytes=(1 * 1024**3), total_bytes=200 * 1024**3)
    finding = check_disk_space(usage, warning_free_gb=20.0, critical_free_gb=5.0)
    assert finding.severity == Severity.CRITICAL


# --- check_lock_contention -----------------------------------------------------------


def test_check_lock_contention_zero_is_info() -> None:
    assert check_lock_contention(0).severity == Severity.INFO


def test_check_lock_contention_warning_threshold() -> None:
    assert check_lock_contention(2).severity == Severity.WARNING


def test_check_lock_contention_critical_threshold() -> None:
    assert check_lock_contention(5).severity == Severity.CRITICAL


# --- loaders --------------------------------------------------------------------------


def test_load_disk_usage_real_filesystem(tmp_path: Path) -> None:
    usage = load_disk_usage(tmp_path)
    assert usage.free_bytes > 0
    assert usage.free_gb > 0


def test_load_skip_count_missing_file(tmp_path: Path) -> None:
    assert load_skip_count(tmp_path) == 0


def test_load_skip_count_reads_integer(tmp_path: Path) -> None:
    (tmp_path / "lock_skip_count").write_text("3\n")
    assert load_skip_count(tmp_path) == 3


def test_load_skip_count_malformed_defaults_to_zero(tmp_path: Path) -> None:
    (tmp_path / "lock_skip_count").write_text("not-a-number")
    assert load_skip_count(tmp_path) == 0


# --- build_backup_findings orchestration + determinism --------------------------------


def test_build_backup_findings_empty_backup_dir_all_info(tmp_path: Path) -> None:
    # disk_warning/critical thresholds set near-zero so this assertion
    # never depends on how much free space the actual test host happens to
    # have at run time (backup_disk_space is the one check that reads real
    # filesystem state rather than only the inputs this test controls).
    config = BackupHealthConfig(
        backup_dir=tmp_path,
        status_path=tmp_path / STATUS_FILENAME,
        remote_type="none",
        remote_filesystem_path=None,
        remote_s3_bucket=None,
        remote_s3_prefix="postgres",
        stale_after_hours=30.0,
        remote_stale_after_hours=48.0,
        disk_warning_free_gb=0.0001,
        disk_critical_free_gb=0.00001,
    )
    findings = build_backup_findings(config, now=NOW)
    # every check is INFO on a fresh install EXCEPT the restore-drill check,
    # which deliberately warns until a first drill has ever been recorded --
    # an untested recovery path is a real gap, not a cosmetic one.
    for f in findings:
        if f.check == "backup_restore_drill":
            assert f.severity == Severity.WARNING
        else:
            assert f.severity == Severity.INFO
    assert all(f.domain == "backup" for f in findings)


def test_restore_drill_check_states(tmp_path: Path) -> None:
    from kalshi_weather.observatory.backup_health import check_restore_drill

    warn = check_restore_drill(None, now=NOW, stale_after_days=30.0)
    assert warn.severity == Severity.WARNING and "ever been recorded" in warn.message
    ok = check_restore_drill(
        {"status": "success", "completed_at": NOW.isoformat(), "backup_key": "k"},
        now=NOW,
        stale_after_days=30.0,
    )
    assert ok.severity == Severity.INFO
    stale = check_restore_drill(
        {"status": "success", "completed_at": "2026-01-01T00:00:00", "backup_key": "k"},
        now=NOW,
        stale_after_days=30.0,
    )
    assert stale.severity == Severity.WARNING
    failed = check_restore_drill(
        {"status": "failed", "failure_reason": "checksum mismatch", "completed_at": ""},
        now=NOW,
        stale_after_days=30.0,
    )
    assert failed.severity == Severity.CRITICAL and "checksum" in failed.message


def test_build_backup_findings_detects_failed_backup(tmp_path: Path) -> None:
    write_backup_status(
        tmp_path / STATUS_FILENAME,
        _status(local_outcome="failed", error="disk full", remote_outcome="skipped"),
    )
    config = BackupHealthConfig(
        backup_dir=tmp_path,
        status_path=tmp_path / STATUS_FILENAME,
        remote_type="none",
        remote_filesystem_path=None,
        remote_s3_bucket=None,
        remote_s3_prefix="postgres",
        stale_after_hours=30.0,
        remote_stale_after_hours=48.0,
        disk_warning_free_gb=20.0,
        disk_critical_free_gb=5.0,
    )
    findings = build_backup_findings(config, now=NOW)
    command_finding = next(f for f in findings if f.check == "backup_command")
    assert command_finding.severity == Severity.CRITICAL


def test_build_backup_findings_is_deterministic(tmp_path: Path) -> None:
    config = BackupHealthConfig(
        backup_dir=tmp_path,
        status_path=tmp_path / STATUS_FILENAME,
        remote_type="none",
        remote_filesystem_path=None,
        remote_s3_bucket=None,
        remote_s3_prefix="postgres",
        stale_after_hours=30.0,
        remote_stale_after_hours=48.0,
        disk_warning_free_gb=20.0,
        disk_critical_free_gb=5.0,
    )
    f1 = build_backup_findings(config, now=NOW)
    f2 = build_backup_findings(config, now=NOW)
    assert [(f.check, f.severity, f.message) for f in f1] == [
        (f.check, f.severity, f.message) for f in f2
    ]


def test_build_backup_findings_with_a_real_dump_file_on_disk(tmp_path: Path) -> None:
    """Regression test: a real .dump file on disk (not an empty directory)
    exercises `list_backup_files` -> `check_backup_freshness`'s full path,
    which is exactly where a naive-vs-aware datetime mismatch previously
    crashed live (`TypeError: can't subtract offset-naive and
    offset-aware datetimes`) the first time this code ran against a real
    backup directory -- caught only by live validation, not by any test at
    the time, because every other test here used an empty directory."""
    ts = NOW - timedelta(hours=1)
    (tmp_path / f"kalshi_weather-{ts.strftime('%Y%m%dT%H%M%SZ')}.dump").write_bytes(b"x")
    config = BackupHealthConfig(
        backup_dir=tmp_path,
        status_path=tmp_path / STATUS_FILENAME,
        remote_type="none",
        remote_filesystem_path=None,
        remote_s3_bucket=None,
        remote_s3_prefix="postgres",
        stale_after_hours=30.0,
        remote_stale_after_hours=48.0,
        disk_warning_free_gb=0.0001,
        disk_critical_free_gb=0.00001,
    )
    findings = build_backup_findings(config, now=NOW)  # must not raise
    freshness = next(f for f in findings if f.check == "backup_local_freshness")
    assert freshness.severity == Severity.INFO
