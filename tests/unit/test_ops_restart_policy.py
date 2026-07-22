"""Tests for the collector restart policy (ops/restart_policy.py).

All filesystem tests run against throwaway tmp_path trees; the real repo,
manifest, and service are never touched.
"""

import json
import os
from pathlib import Path

import pytest

from kalshi_weather.ops.restart_policy import (
    Action,
    RestartReport,
    build_manifest,
    classify,
    compare,
    format_report,
    write_manifest,
)


class TestClassify:
    @pytest.mark.parametrize(
        ("path", "action"),
        [
            # service layer -> reinstall
            ("scripts/service/run.sh", Action.REINSTALL),
            ("scripts/service/install.sh", Action.REINSTALL),
            # collector code (incl. migrations under src/) -> restart
            ("src/kalshi_weather/cli.py", Action.RESTART),
            ("src/kalshi_weather/ingestion/collector.py", Action.RESTART),
            ("src/kalshi_weather/kalshi/auth.py", Action.RESTART),
            ("src/kalshi_weather/storage/repositories.py", Action.RESTART),
            ("src/kalshi_weather/alembic/versions/0002_x.py", Action.RESTART),
            # configuration / credentials / dependencies -> restart
            (".env", Action.RESTART),
            ("secrets/kalshi-demo.pem", Action.RESTART),
            ("config/development.yaml", Action.RESTART),
            ("pyproject.toml", Action.RESTART),
            ("uv.lock", Action.RESTART),
            ("alembic.ini", Action.RESTART),
            # inert for the running collector -> none
            (".env.example", Action.NONE),
            (".env.bak-20260721", Action.NONE),
            ("docker-compose.yml", Action.NONE),
            ("docs/runbooks/operations.md", Action.NONE),
            ("docs/research/experiments/EXP-1.json", Action.NONE),
            ("tests/unit/test_config.py", Action.NONE),
            ("notebooks/scratch.ipynb", Action.NONE),
            ("scripts/exp_h0005_calibration.py", Action.NONE),
            ("scripts/backup_postgres.sh", Action.NONE),
            ("README.md", Action.NONE),
            ("Makefile", Action.NONE),
        ],
    )
    def test_rules(self, path: str, action: Action) -> None:
        assert classify(path).action is action

    def test_unknown_path_defaults_to_restart(self) -> None:
        rule = classify("mystery_new_toplevel.py")
        assert rule.action is Action.RESTART
        assert "conservative" in rule.reason

    def test_service_dir_beats_generic_scripts_rule(self) -> None:
        assert classify("scripts/service/new_helper.py").action is Action.REINSTALL
        assert classify("scripts/new_helper.py").action is Action.NONE

    def test_severity_ordering(self) -> None:
        assert Action.NONE < Action.RESTART < Action.REINSTALL


def make_repo(tmp_path: Path) -> Path:
    """Minimal fake repo containing one file of each classification.

    Returns the repo root, a *subdirectory* of tmp_path, so tests can keep
    manifests outside the tree (as in production, where the manifest lives
    in KALSHI_LOG_DIR, not the repo).
    """
    root = tmp_path / "repo"
    (root / "src/kalshi_weather").mkdir(parents=True)
    (root / "scripts/service").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "src/kalshi_weather/cli.py").write_text("print('v1')\n")
    (root / "scripts/service/run.sh").write_text("#!/bin/bash\n")
    (root / "docs/notes.md").write_text("docs\n")
    (root / ".env").write_text("KALSHI_ENV=demo\n")
    (root / ".env.bak-1").write_text("old\n")
    # pruned/hidden dirs must never be hashed
    (root / ".git").mkdir()
    (root / ".git/HEAD").write_text("ref\n")
    (root / "__pycache__").mkdir()
    (root / "__pycache__/x.pyc").write_text("junk")
    return root


class TestManifest:
    def test_contains_only_restart_relevant_files(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        manifest = build_manifest(repo)
        assert set(manifest["files"]) == {
            "src/kalshi_weather/cli.py",
            "scripts/service/run.sh",
            ".env",
        }
        assert manifest["schema"] == 1
        assert manifest["pid"] == os.getpid()

    def test_hashes_never_contain_file_contents(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        secret = "KALSHI_ENV=demo"
        raw = json.dumps(build_manifest(repo))
        assert secret not in raw
        assert all(len(h) == 64 for h in build_manifest(repo)["files"].values())

    def test_write_is_loadable_and_atomic(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        manifest_path = tmp_path / "logs" / "collector.manifest.json"
        written = write_manifest(repo, manifest_path)
        assert json.loads(manifest_path.read_text()) == written
        assert not manifest_path.with_suffix(".tmp").exists()


class TestCompare:
    def make_baseline(self, tmp_path: Path) -> tuple[Path, Path]:
        repo = make_repo(tmp_path)
        manifest_path = tmp_path / "manifest.json"  # outside the repo, as in production
        write_manifest(repo, manifest_path)
        return repo, manifest_path

    def test_no_changes(self, tmp_path: Path) -> None:
        repo, manifest_path = self.make_baseline(tmp_path)
        report = compare(repo, manifest_path)
        assert report.verdict is Action.NONE
        assert report.changes == ()
        assert report.exit_code == 0
        assert report.headline == "Collector restart not required"

    def test_doc_change_needs_nothing(self, tmp_path: Path) -> None:
        repo, manifest_path = self.make_baseline(tmp_path)
        (repo / "docs/notes.md").write_text("edited docs\n")
        (repo / ".env.bak-1").write_text("edited backup\n")
        report = compare(repo, manifest_path)
        assert report.verdict is Action.NONE

    def test_src_change_needs_restart(self, tmp_path: Path) -> None:
        repo, manifest_path = self.make_baseline(tmp_path)
        (repo / "src/kalshi_weather/cli.py").write_text("print('v2')\n")
        report = compare(repo, manifest_path)
        assert report.verdict is Action.RESTART
        assert report.exit_code == 10
        assert report.headline == "Collector restart required"
        (change,) = report.changes
        assert (change.path, change.kind) == ("src/kalshi_weather/cli.py", "modified")

    def test_added_and_removed_files_detected(self, tmp_path: Path) -> None:
        repo, manifest_path = self.make_baseline(tmp_path)
        (repo / "src/kalshi_weather/new_module.py").write_text("x = 1\n")
        (repo / ".env").unlink()  # disposable fake repo, not the real .env
        report = compare(repo, manifest_path)
        kinds = {c.path: c.kind for c in report.changes}
        assert kinds == {"src/kalshi_weather/new_module.py": "added", ".env": "removed"}
        assert report.verdict is Action.RESTART

    def test_service_change_outranks_src_change(self, tmp_path: Path) -> None:
        repo, manifest_path = self.make_baseline(tmp_path)
        (repo / "src/kalshi_weather/cli.py").write_text("print('v2')\n")
        (repo / "scripts/service/run.sh").write_text("#!/bin/bash\nedited\n")
        report = compare(repo, manifest_path)
        assert report.verdict is Action.REINSTALL
        assert report.exit_code == 11
        assert report.headline == "Collector service REINSTALL required"

    def test_missing_manifest_is_unknown(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        report = compare(repo, tmp_path / "missing.json")
        assert report.verdict is None
        assert report.exit_code == 12
        assert "UNKNOWN" in report.headline
        assert any("no manifest" in n for n in report.notes)

    def test_dead_pid_produces_note(self, tmp_path: Path) -> None:
        repo, manifest_path = self.make_baseline(tmp_path)
        manifest = json.loads(manifest_path.read_text())
        manifest["pid"] = 2**22 - 1  # exceeds default macOS/Linux pid ranges
        manifest_path.write_text(json.dumps(manifest))
        report = compare(repo, manifest_path)
        assert any("no longer running" in n for n in report.notes)


class TestFormatReport:
    def test_verdict_is_last_meaningful_content(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        manifest_path = tmp_path / "manifest.json"
        write_manifest(repo, manifest_path)
        (repo / "src/kalshi_weather/cli.py").write_text("print('v2')\n")
        text = format_report(compare(repo, manifest_path))
        assert "Collector restart required" in text
        assert "restart.sh" in text
        assert "src/kalshi_weather/cli.py (modified)" in text

    def test_unknown_report_renders(self) -> None:
        text = format_report(RestartReport(verdict=None, notes=("no manifest at x",)))
        assert "UNKNOWN" in text
