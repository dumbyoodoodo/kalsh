"""S3 restore drill: safety guards, verified-backup selection, failure
classification via an injected fake runner, cleanup on both paths, and
append-only history. Synthetic fixtures only -- no docker, no S3, no
production database."""

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kalshi_weather.ops.backup import BackupStatus
from kalshi_weather.ops.restore_drill import (
    DRILL_PREFIX,
    PRODUCTION_CONTAINER,
    DrillConfig,
    DrillSafetyError,
    S3Object,
    assert_disposable,
    disposable_container_name,
    read_drill_state,
    redact,
    run_drill,
    select_verified_backup,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def status(**kw: object) -> BackupStatus:
    base: dict[str, object] = {
        "schema": 1,
        "timestamp": "2026-07-27T08:00:05+00:00",
        "local_outcome": "success",
        "local_path": "/backups/kalshi_weather-20260727T080005Z.dump",
        "size_bytes": 1000,
        "entries": 136,
        "duration_seconds": 10.0,
        "error": None,
        "remote_outcome": "success",
        "remote_detail": "s3: verified upload",
    }
    base.update(kw)
    return BackupStatus(**base)  # type: ignore[arg-type]


OBJS = [
    S3Object("postgres/kalshi_weather-20260726T080000Z.dump", 900, "2026-07-26"),
    S3Object("postgres/kalshi_weather-20260727T080005Z.dump", 1000, "2026-07-27"),
]


# --- guards ------------------------------------------------------------------


def test_disposable_names_are_prefixed_and_unique() -> None:
    a, b = disposable_container_name(), disposable_container_name()
    assert a.startswith(DRILL_PREFIX) and b.startswith(DRILL_PREFIX) and a != b
    assert assert_disposable(a) == a


def test_production_container_names_refused() -> None:
    with pytest.raises(DrillSafetyError):
        assert_disposable(PRODUCTION_CONTAINER)
    with pytest.raises(DrillSafetyError):
        assert_disposable("postgres")
    with pytest.raises(DrillSafetyError):
        assert_disposable(f"{DRILL_PREFIX}{PRODUCTION_CONTAINER}")  # embedded prod name


def test_redaction_strips_credentials() -> None:
    assert "REDACTED" in redact("postgresql+psycopg://user:secret@host/db")
    assert "secret" not in redact("postgresql://u:secret@h/db")
    assert "REDACTED" in redact("AWS_SECRET_ACCESS_KEY=abc123")
    assert redact("plain text stays") == "plain text stays"


# --- selection ---------------------------------------------------------------


def test_selects_newest_verified_backup() -> None:
    c = select_verified_backup(status(), OBJS)
    assert c.reason is None
    assert c.key.endswith("20260727T080005Z.dump") and c.size == 1000


def test_no_dumps_or_zero_bytes_blocked() -> None:
    assert select_verified_backup(status(), []).reason is not None
    zero = [S3Object("postgres/kalshi_weather-20260727T080005Z.dump", 0, "")]
    assert "zero bytes" in str(select_verified_backup(status(), zero).reason)


def test_unverified_newest_blocked_no_silent_fallback() -> None:
    # newest S3 object is NOT the verified upload -> refuse, don't fall back
    extra = [*OBJS, S3Object("postgres/kalshi_weather-20260728T080000Z.dump", 500, "")]
    c = select_verified_backup(status(), extra)
    assert c.reason is not None and "not the verified upload" in c.reason


def test_missing_or_failed_status_blocked() -> None:
    assert "no backup status" in str(select_verified_backup(None, OBJS).reason)
    c = select_verified_backup(status(remote_outcome="failed"), OBJS)
    assert "remote outcome" in str(c.reason)


def test_size_mismatch_blocked_and_explicit_key_allowed() -> None:
    c = select_verified_backup(status(size_bytes=999), OBJS)
    assert "size" in str(c.reason)
    explicit = select_verified_backup(status(), OBJS, backup_key="20260726T080000Z.dump")
    assert explicit.reason is None and explicit.key.endswith("20260726T080000Z.dump")
    missing = select_verified_backup(status(), OBJS, backup_key="nope.dump")
    assert missing.reason is not None


# --- drill orchestration with a scripted fake runner -------------------------


def make_runner(overrides: dict[str, tuple[int, str, str]]):  # type: ignore[no-untyped-def]
    """Fake runner: match commands by keyword, return (rc, stdout, stderr).
    Also records every command for guard/cleanup assertions."""
    calls: list[list[str]] = []
    listing = json.dumps(
        {
            "Contents": [
                {"Key": o.key, "Size": o.size, "LastModified": o.last_modified} for o in OBJS
            ]
        }
    )
    tables = (
        "raw_api_payloads weather_stations weather_observations weather_forecasts "
        "market_snapshots orderbook_snapshots market_poll_attempts trades "
        "market_candlesticks collector_runs alembic_version"
    )
    defaults = {
        "list-objects-v2": (0, listing, ""),
        "s3 cp": (0, "", ""),
        "docker run": (0, "containerid", ""),
        "pg_isready": (0, "accepting", ""),
        "--version": (0, "pg_restore (PostgreSQL) 16.4", ""),
        "docker cp": (0, "", ""),
        "--list": (0, tables, ""),
        "pg_restore -U": (0, "", ""),
        "version_num": (0, "0011", ""),
        "count(*)": (0, "5", ""),
        "market_ticker ||": (0, "KXHIGHNY-X|active", ""),
        "raw_payload_id": (0, "KXHIGHNY-X|12", ""),
        "collector_runs r": (0, "KXHIGHNY-X|9", ""),
        "observed_at": (0, "SEA|2026-07-27 10:00:00", ""),
        "observation_date": (0, "PHX|2026-07-27", ""),
        "none-settled": (0, "KXLOWTNYC-X|no", ""),
        "max(finished_at)": (0, "2026-07-27 07:55:00", ""),
        "docker rm": (0, "", ""),
    }
    merged = {**defaults, **overrides}

    def runner(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        joined = " ".join(cmd)
        for key, (rc, out, err) in merged.items():
            if key in joined:
                return subprocess.CompletedProcess(cmd, rc, out, err)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


@pytest.fixture
def config(tmp_path: Path) -> DrillConfig:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    st = status()
    (backup_dir / "last_backup_status.json").write_text(
        json.dumps({k: getattr(st, k) for k in st.__dataclass_fields__})
    )
    return DrillConfig(bucket="b", prefix="postgres", backup_dir=backup_dir, code_commit="abc123")


def wrap_with_download(runner, size: int = 1000):  # type: ignore[no-untyped-def]
    """Wrap a fake runner so 'aws s3 cp' actually writes the target file."""

    def wrapped(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if len(cmd) > 2 and cmd[:3] == ["aws", "s3", "cp"]:
            Path(cmd[-1]).write_bytes(b"x" * size)
        return runner(cmd, timeout)

    wrapped.calls = runner.calls  # type: ignore[attr-defined]
    return wrapped


def test_successful_drill_end_to_end(config: DrillConfig, tmp_path: Path) -> None:
    runner = wrap_with_download(make_runner({}))
    result = run_drill(config, runner=runner, now=NOW)
    assert result.status == "success", result.steps
    assert result.cleanup_ok is True
    assert result.container is not None and result.container.startswith(DRILL_PREFIX)
    # every docker exec/rm addressed the disposable container only
    for call in runner.calls:  # type: ignore[attr-defined]
        if call[:2] == ["docker", "exec"] or call[:2] == ["docker", "rm"]:
            name = call[2] if call[1] == "exec" else call[-1]
            assert name.startswith(DRILL_PREFIX)
    # cleanup ran: container removed and tmp file deleted
    assert any(c[:2] == ["docker", "rm"] for c in runner.calls)  # type: ignore[attr-defined]
    # history + state written
    hist = (config.backup_dir / "restore_drill_history.jsonl").read_text().splitlines()
    assert len(hist) == 1
    entry = json.loads(hist[0])
    assert entry["status"] == "success" and entry["git_commit"] == "abc123"
    assert entry["backup_timestamp"] == "20260727T080005Z"
    state = read_drill_state(config.backup_dir / "restore_drill_state.json")
    assert state is not None and state["drill_id"] == result.drill_id


def test_dry_run_selects_but_downloads_nothing(config: DrillConfig) -> None:
    from dataclasses import replace

    runner = make_runner({})
    result = run_drill(replace(config, dry_run=True), runner=runner, now=NOW)
    assert result.status == "success"
    assert not any(c[:3] == ["aws", "s3", "cp"] for c in runner.calls)  # type: ignore[attr-defined]
    assert not (config.backup_dir / "restore_drill_history.jsonl").exists()


def test_checksum_mismatch_fails_and_cleans_up(config: DrillConfig) -> None:
    # local twin exists with different content -> sha mismatch -> fail closed
    (config.backup_dir / "kalshi_weather-20260727T080005Z.dump").write_bytes(b"different")
    runner = wrap_with_download(make_runner({}))
    result = run_drill(config, runner=runner, now=NOW)
    assert result.status == "failed"
    assert "checksum" in str(result.failure_reason)
    assert result.cleanup_ok is True  # tmp deleted even though no container yet


def test_zero_byte_download_rejected(config: DrillConfig) -> None:
    runner = wrap_with_download(make_runner({}), size=0)
    result = run_drill(config, runner=runner, now=NOW)
    assert result.status == "failed"
    assert "size" in str(result.failure_reason) or "zero" in str(result.failure_reason)


def test_invalid_dump_structure_rejected(config: DrillConfig) -> None:
    runner = wrap_with_download(make_runner({"--list": (0, "nothing useful", "")}))
    result = run_drill(config, runner=runner, now=NOW)
    assert result.status == "failed" and "structure" in str(result.failure_reason)
    assert any(c[:2] == ["docker", "rm"] for c in runner.calls)  # type: ignore[attr-defined]


def test_pg_version_mismatch_rejected(config: DrillConfig) -> None:
    runner = wrap_with_download(
        make_runner({"--version": (0, "pg_restore (PostgreSQL) 15.2", "")})
    )
    result = run_drill(config, runner=runner, now=NOW)
    assert result.status == "failed" and "version" in str(result.failure_reason)


def test_missing_alembic_version_and_wrong_revision(config: DrillConfig) -> None:
    r1 = run_drill(
        config,
        runner=wrap_with_download(make_runner({"version_num": (1, "", "no such table")})),
        now=NOW,
    )
    assert r1.status == "failed"
    r2 = run_drill(
        config, runner=wrap_with_download(make_runner({"version_num": (0, "0009", "")})), now=NOW
    )
    assert r2.status == "failed" and "0011" in str(r2.failure_reason)


def test_empty_representative_tables_rejected(config: DrillConfig) -> None:
    runner = wrap_with_download(make_runner({"count(*)": (0, "0", "")}))
    result = run_drill(config, runner=runner, now=NOW)
    assert result.status == "failed" and "tables" in str(result.failure_reason)


def test_container_start_failure_classified(config: DrillConfig) -> None:
    runner = wrap_with_download(make_runner({"docker run": (125, "", "cannot start")}))
    result = run_drill(config, runner=runner, now=NOW)
    assert result.status == "failed" and "container" in str(result.failure_reason)


def test_timeout_classified_and_cleaned_up(config: DrillConfig) -> None:
    base = wrap_with_download(make_runner({}))

    def runner(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if "pg_restore" in " ".join(cmd) and "-d" in cmd:
            raise subprocess.TimeoutExpired(cmd, timeout)
        return base(cmd, timeout)

    runner.calls = base.calls  # type: ignore[attr-defined]
    result = run_drill(config, runner=runner, now=NOW)
    assert result.status == "failed" and result.failure_reason == "timeout"
    assert any(c[:2] == ["docker", "rm"] for c in base.calls)  # type: ignore[attr-defined]


def test_history_is_append_only_across_drills(config: DrillConfig) -> None:
    ok = wrap_with_download(make_runner({}))
    run_drill(config, runner=ok, now=NOW)
    bad = wrap_with_download(make_runner({"version_num": (0, "0001", "")}))
    run_drill(config, runner=bad, now=NOW)
    lines = (config.backup_dir / "restore_drill_history.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["status"] == "success"
    assert json.loads(lines[1])["status"] == "failed"
    state = read_drill_state(config.backup_dir / "restore_drill_state.json")
    assert state is not None and state["status"] == "failed"  # latest wins in state


def test_blocked_selection_runs_nothing(config: DrillConfig) -> None:
    # status says the newest object is NOT verified -> blocked, no docker calls
    extra = json.dumps(
        {
            "Contents": [
                {
                    "Key": "postgres/kalshi_weather-20260728T000000Z.dump",
                    "Size": 5,
                    "LastModified": "",
                }
            ]
        }
    )
    runner = make_runner({"list-objects-v2": (0, extra, "")})
    result = run_drill(config, runner=runner, now=NOW)
    assert result.status == "blocked"
    assert not any(c[0] == "docker" for c in runner.calls)  # type: ignore[attr-defined]


# --- module isolation --------------------------------------------------------


def test_no_production_or_experiment_code_paths() -> None:
    src = (
        Path(__file__).resolve().parents[2] / "src/kalshi_weather/ops/restore_drill.py"
    ).read_text()
    banned = re.compile(
        r"database_url|kalshi_weather\.experiments|kalshi_weather\.paper"
        r"|kalshi_weather\.kalshi|restart|launchctl|kickstart|h0019|h0020",
        re.IGNORECASE,
    )
    m = banned.search(src)
    assert m is None, f"restore_drill references banned path: {m.group(0)!r}"
    # DROP DATABASE never appears -- teardown is container removal only
    assert "DROP DATABASE" not in src.upper()
