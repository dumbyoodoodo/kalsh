import subprocess
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kalshi_weather.ops.backup import (
    BackupStatus,
    RemoteConfig,
    copy_to_filesystem,
    copy_to_remote,
    copy_to_s3,
    finalize_backup,
    read_backup_status,
    sha256_file,
    write_backup_status,
)

NOW = datetime(2026, 7, 23, 0, 0, 0, tzinfo=UTC)


def _make_local(tmp_path: Path, content: bytes = b"dump-bytes") -> Path:
    path = tmp_path / "kalshi_weather-20260722T034116Z.dump"
    path.write_bytes(content)
    return path


def test_sha256_file_matches_known_hash(tmp_path: Path) -> None:
    path = tmp_path / "f.bin"
    path.write_bytes(b"hello")
    import hashlib

    assert sha256_file(path) == hashlib.sha256(b"hello").hexdigest()


# --- filesystem transport --------------------------------------------------------


def test_copy_to_filesystem_success(tmp_path: Path) -> None:
    (tmp_path / "local").mkdir()
    local = _make_local(tmp_path / "local")
    dest = tmp_path / "dest"
    dest.mkdir()
    result = copy_to_filesystem(local, str(dest))
    assert result.outcome == "success"
    assert (dest / local.name).read_bytes() == local.read_bytes()


def test_copy_to_filesystem_missing_destination_is_unavailable(tmp_path: Path) -> None:
    (tmp_path / "local").mkdir()
    local = _make_local(tmp_path / "local")
    result = copy_to_filesystem(local, str(tmp_path / "does-not-exist"))
    assert result.outcome == "unavailable"
    assert "mounted" in result.detail


def test_copy_to_filesystem_checksum_mismatch_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "local").mkdir()
    local = _make_local(tmp_path / "local")
    dest = tmp_path / "dest"
    dest.mkdir()

    def _corrupt_copy(src: str, dst: str) -> None:
        Path(dst).write_bytes(b"corrupted-content-does-not-match-source")

    monkeypatch.setattr("kalshi_weather.ops.backup.shutil.copy2", _corrupt_copy)
    result = copy_to_filesystem(local, str(dest))
    assert result.outcome == "failed"
    assert "checksum mismatch" in result.detail


# --- s3 transport (subprocess mocked -- no real aws CLI or network calls) -------


def test_copy_to_s3_unavailable_when_aws_cli_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = _make_local(tmp_path)
    monkeypatch.setattr("kalshi_weather.ops.backup.shutil.which", lambda _name: None)
    result = copy_to_s3(local, bucket="my-bucket", prefix="postgres")
    assert result.outcome == "unavailable"
    assert "aws" in result.detail


def test_copy_to_s3_upload_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = _make_local(tmp_path)
    monkeypatch.setattr(
        "kalshi_weather.ops.backup.shutil.which", lambda _name: "/usr/local/bin/aws"
    )

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="AccessDenied")

    monkeypatch.setattr("kalshi_weather.ops.backup.subprocess.run", _fake_run)
    result = copy_to_s3(local, bucket="my-bucket", prefix="postgres")
    assert result.outcome == "failed"
    assert "AccessDenied" in result.detail


def test_copy_to_s3_success_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = _make_local(tmp_path, content=b"the-dump-contents")
    monkeypatch.setattr(
        "kalshi_weather.ops.backup.shutil.which", lambda _name: "/usr/local/bin/aws"
    )

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = _FakeStdout(b"the-dump-contents")
            self.returncode = 0

        def __enter__(self) -> "_FakeProc":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            return (b"", b"")

    class _FakeStdout:
        def __init__(self, data: bytes) -> None:
            self._chunks = [data, b""]
            self._i = 0

        def read(self, _n: int) -> bytes:
            chunk = self._chunks[self._i] if self._i < len(self._chunks) else b""
            self._i += 1
            return chunk

    monkeypatch.setattr("kalshi_weather.ops.backup.subprocess.run", _fake_run)
    monkeypatch.setattr("kalshi_weather.ops.backup.subprocess.Popen", lambda *a, **k: _FakeProc())
    result = copy_to_s3(local, bucket="my-bucket", prefix="postgres")
    assert result.outcome == "success"


def test_copy_to_s3_checksum_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = _make_local(tmp_path, content=b"original-content")
    monkeypatch.setattr(
        "kalshi_weather.ops.backup.shutil.which", lambda _name: "/usr/local/bin/aws"
    )

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = _FakeStdout(b"different-content-in-s3")
            self.returncode = 0

        def __enter__(self) -> "_FakeProc":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            return (b"", b"")

    class _FakeStdout:
        def __init__(self, data: bytes) -> None:
            self._chunks = [data, b""]
            self._i = 0

        def read(self, _n: int) -> bytes:
            chunk = self._chunks[self._i] if self._i < len(self._chunks) else b""
            self._i += 1
            return chunk

    monkeypatch.setattr("kalshi_weather.ops.backup.subprocess.run", _fake_run)
    monkeypatch.setattr("kalshi_weather.ops.backup.subprocess.Popen", lambda *a, **k: _FakeProc())
    result = copy_to_s3(local, bucket="my-bucket", prefix="postgres")
    assert result.outcome == "failed"
    assert "checksum mismatch" in result.detail


# --- dispatch --------------------------------------------------------------------


def test_copy_to_remote_none_type() -> None:
    result = copy_to_remote(Path("/x"), RemoteConfig(remote_type="none"))
    assert result.outcome == "not_configured"


def test_copy_to_remote_unknown_type() -> None:
    result = copy_to_remote(Path("/x"), RemoteConfig(remote_type="carrier-pigeon"))
    assert result.outcome == "failed"


def test_copy_to_remote_filesystem_missing_path_config() -> None:
    result = copy_to_remote(
        Path("/x"), RemoteConfig(remote_type="filesystem", filesystem_path=None)
    )
    assert result.outcome == "unavailable"


def test_copy_to_remote_s3_missing_bucket_config() -> None:
    result = copy_to_remote(Path("/x"), RemoteConfig(remote_type="s3", s3_bucket=None))
    assert result.outcome == "unavailable"


# --- finalize_backup --------------------------------------------------------------


def test_finalize_backup_local_failure_skips_remote() -> None:
    status = finalize_backup(
        now=NOW,
        local_outcome="failed",
        local_path=None,
        size_bytes=None,
        entries=None,
        duration_seconds=None,
        error="pg_dump failed",
        remote_config=RemoteConfig(remote_type="s3", s3_bucket="my-bucket"),
    )
    assert status.local_outcome == "failed"
    assert status.remote_outcome == "skipped"


def test_finalize_backup_success_with_no_remote_configured(tmp_path: Path) -> None:
    local = _make_local(tmp_path)
    status = finalize_backup(
        now=NOW,
        local_outcome="success",
        local_path=local,
        size_bytes=100,
        entries=42,
        duration_seconds=3.5,
        error=None,
        remote_config=RemoteConfig(remote_type="none"),
    )
    assert status.local_outcome == "success"
    assert status.remote_outcome == "not_configured"
    assert status.entries == 42


def test_finalize_backup_success_but_missing_local_path() -> None:
    status = finalize_backup(
        now=NOW,
        local_outcome="success",
        local_path=None,
        size_bytes=None,
        entries=None,
        duration_seconds=None,
        error=None,
        remote_config=RemoteConfig(remote_type="none"),
    )
    assert status.remote_outcome == "failed"


# --- status persistence ------------------------------------------------------------


def test_write_and_read_backup_status_roundtrip(tmp_path: Path) -> None:
    status = BackupStatus(
        schema=1,
        timestamp=NOW.isoformat(),
        local_outcome="success",
        local_path="/backups/x.dump",
        size_bytes=123,
        entries=10,
        duration_seconds=1.5,
        error=None,
        remote_outcome="success",
        remote_detail="filesystem: verified copy at /remote/x.dump",
    )
    path = tmp_path / "status.json"
    write_backup_status(path, status)
    loaded = read_backup_status(path)
    assert loaded == status


def test_read_backup_status_missing_file(tmp_path: Path) -> None:
    assert read_backup_status(tmp_path / "nope.json") is None


def test_write_backup_status_creates_parent_dirs(tmp_path: Path) -> None:
    status = BackupStatus(
        schema=1,
        timestamp=NOW.isoformat(),
        local_outcome="success",
        local_path=None,
        size_bytes=None,
        entries=None,
        duration_seconds=None,
        error=None,
        remote_outcome="not_configured",
        remote_detail="x",
    )
    path = tmp_path / "nested" / "status.json"
    write_backup_status(path, status)
    assert path.exists()


# --- secret handling ----------------------------------------------------------------


def test_remote_config_has_no_credential_fields() -> None:
    """This design deliberately keeps AWS credentials out of Python entirely
    (the `aws` CLI manages its own credential chain) -- assert the config
    dataclass has no field that could ever hold a secret."""
    field_names = {f.name for f in fields(RemoteConfig)}
    forbidden = {"secret", "key", "password", "token", "credential"}
    for name in field_names:
        assert not any(bad in name.lower() for bad in forbidden), name


def test_backup_status_to_dict_has_no_credential_fields() -> None:
    field_names = {f.name for f in fields(BackupStatus)}
    forbidden = {"secret", "password", "token", "credential"}
    for name in field_names:
        assert not any(bad in name.lower() for bad in forbidden), name
