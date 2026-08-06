"""The CI workflow must run the same gates the project documents locally, and
must not quietly stop running safety tests.

Two failures motivated this file:

- CI ran ``uv sync --no-editable --all-groups`` while the documented local gate
  ran ``--frozen``. A stale ``uv.lock`` would therefore be re-resolved in CI and
  caught only locally -- the reverse of what a gate is for.
- CI had no PostgreSQL, so twelve integration tests that assert real dialect
  behaviour (Alembic replay, FK and unique constraints, ON CONFLICT dedup,
  cross-database isolation) skipped on every run. The suite reported green while
  the migration-replay tests had not executed.

The workflow YAML is parsed, not pattern-matched: these assert against the
artifact that actually drives CI.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
import yaml

from kalshi_weather.storage.migration_safety import TEST_DB_PREFIX, target_identity

WORKFLOW = Path(".github/workflows/ci.yml")

#: Modules whose tests must never be skipped wholesale. Each guards an
#: invariant that a green-but-skipped suite would silently stop protecting.
CRITICAL_MODULES = (
    "tests/unit/test_migration_safety.py",
    "tests/unit/test_extension_deployment.py",
    "tests/unit/test_attempt_integration.py",
    "tests/unit/test_attempt_evidence_integrity.py",
    "tests/unit/test_weather_attempt_recording.py",
    "tests/unit/test_gap_ledger.py",
)

#: Integration modules that require a real PostgreSQL server. With the CI
#: service container these RUN; without it they skip on reachability. They must
#: never carry an unconditional skip.
POSTGRES_MODULES = (
    "tests/integration/test_availability_postgres.py",
    "tests/integration/test_dataset_canonical_provenance.py",
    "tests/integration/test_environment_provenance_migration.py",
    "tests/integration/test_history_adapter_postgres.py",
    "tests/integration/test_poll_ledger_postgres.py",
)

#: The ONLY modules allowed to skip in CI, with the reason they may. Both need
#: credentials or live third-party endpoints that shared CI must not hold.
#: Adding a module here is a deliberate, reviewable act.
CI_SKIP_ALLOWLIST = {
    "tests/integration/test_kalshi_demo_client.py": "requires demo exchange credentials",
    "tests/integration/test_weather_nws_provider.py": "requires live NWS endpoints",
}


def _workflow() -> dict[str, Any]:
    assert WORKFLOW.exists(), f"{WORKFLOW} is missing"
    loaded = yaml.safe_load(WORKFLOW.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _check_job() -> dict[str, Any]:
    job = _workflow()["jobs"]["check"]
    assert isinstance(job, dict)
    return job


def _steps() -> list[dict[str, Any]]:
    return [s for s in _check_job()["steps"] if isinstance(s, dict)]


def _run_commands() -> list[str]:
    return [str(s["run"]) for s in _steps() if "run" in s]


def _command_containing(fragment: str) -> str:
    matches = [c for c in _run_commands() if fragment in c]
    assert matches, f"no CI step runs {fragment!r}; commands were {_run_commands()}"
    assert len(matches) == 1, f"{fragment!r} appears in more than one step: {matches}"
    return matches[0]


# --- dependency-installation parity ------------------------------------------


def test_sync_is_frozen_non_editable_and_all_groups() -> None:
    """The exact defect: CI omitted --frozen, so a stale lockfile passed CI."""
    sync = _command_containing("uv sync")
    for flag in ("--frozen", "--no-editable", "--all-groups"):
        assert flag in sync, f"CI dependency sync must pass {flag}; got {sync!r}"


def test_ci_does_not_install_the_project_editable() -> None:
    sync = _command_containing("uv sync")
    assert "--editable" not in sync.replace("--no-editable", "")


# --- gate parity --------------------------------------------------------------


def test_ci_runs_the_documented_gates() -> None:
    assert "ruff check ." in _command_containing("ruff")
    assert "mypy src" in _command_containing("mypy")
    pytest_cmd = _command_containing("pytest")
    assert " -v" in f" {pytest_cmd} ", "CI must run pytest verbosely"


def test_ci_reports_skip_reasons() -> None:
    """Without -rs a safety test that starts skipping is invisible in the log."""
    assert "-rs" in _command_containing("pytest")


def test_ci_runs_the_whole_suite() -> None:
    """No -k/-m filter may quietly exclude the unit suite -- including this
    workflow-contract module itself."""
    pytest_cmd = _command_containing("pytest")
    for narrowing in (" -k ", " -m ", "--ignore"):
        assert narrowing not in f" {pytest_cmd} ", f"CI pytest must not narrow with {narrowing!r}"


def test_required_gates_have_no_continue_on_error() -> None:
    job = _check_job()
    assert not job.get("continue-on-error", False), "the check job must fail the run"
    for step in _steps():
        assert not step.get("continue-on-error", False), (
            f"step {step.get('name', step.get('uses'))!r} must not continue on error"
        )


# --- the CI PostgreSQL service ------------------------------------------------


def test_ci_provides_a_postgres_service() -> None:
    services = _check_job().get("services", {})
    assert "postgres" in services, "CI must provide PostgreSQL or the dialect tests skip"
    image = str(services["postgres"]["image"])
    assert image.startswith("postgres:16"), f"align with the project's PostgreSQL 16; got {image}"


def test_ci_postgres_database_is_an_approved_disposable_name() -> None:
    """The service database must carry the prefix migration_safety requires of
    a disposable migration target, so CI can never look like production."""
    env = _check_job()["services"]["postgres"]["env"]
    assert str(env["POSTGRES_DB"]).startswith(TEST_DB_PREFIX)


def test_ci_postgres_has_a_health_check() -> None:
    options = str(_check_job()["services"]["postgres"]["options"])
    assert "--health-cmd" in options and "pg_isready" in options


def test_test_step_points_at_the_disposable_service() -> None:
    step = next(s for s in _steps() if "pytest" in str(s.get("run", "")))
    url = str(step["env"]["DATABASE_URL"])
    identity = target_identity(url)
    assert identity.scheme_family in {"postgresql", "postgres"}
    assert identity.database.startswith(TEST_DB_PREFIX), (
        f"CI DATABASE_URL must name a disposable test database; got {identity.describe()}"
    )


def test_ci_database_url_is_not_a_production_host() -> None:
    """A hostname check is not the primary defence (migration_safety compares
    fingerprints), but a CI URL pointing anywhere but the local service is a
    mistake worth catching in review."""
    step = next(s for s in _steps() if "pytest" in str(s.get("run", "")))
    identity = target_identity(str(step["env"]["DATABASE_URL"]))
    assert identity.host in {"localhost", "127.0.0.1"}


# --- skip policy --------------------------------------------------------------


def _load_module(path: str) -> object:
    spec = importlib.util.spec_from_file_location(Path(path).stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _skip_marks(module: object) -> list[pytest.MarkDecorator]:
    marks = getattr(module, "pytestmark", [])
    if not isinstance(marks, list):
        marks = [marks]
    return [
        m
        for m in marks
        if getattr(m, "name", getattr(m, "markname", "")) in {"skip", "skipif"}
    ]


@pytest.mark.parametrize("path", CRITICAL_MODULES)
def test_critical_modules_are_never_skipped_wholesale(path: str) -> None:
    """Inspects real mark objects, not source text: a module-level skip on any
    of these would leave a safety invariant unprotected while CI stayed green."""
    assert Path(path).exists(), f"{path} disappeared; update CRITICAL_MODULES deliberately"
    assert _skip_marks(_load_module(path)) == [], f"{path} must not carry a module-level skip"


@pytest.mark.parametrize("path", POSTGRES_MODULES)
def test_postgres_modules_skip_only_on_server_reachability(path: str) -> None:
    """These may skip when no server is reachable, but never unconditionally --
    otherwise the CI service would be provisioned and still unused."""
    assert Path(path).exists()
    module = _load_module(path)
    assert _skip_marks(module) == [], f"{path} must not skip unconditionally"
    assert hasattr(module, "throwaway_db"), f"{path} must gate on a disposable-database fixture"


def test_skip_allowlist_covers_exactly_the_credentialed_modules() -> None:
    """Every integration module either runs in CI (PostgreSQL) or is on the
    allowlist with a stated reason. A new skipping module fails here until
    someone decides which it is."""
    present = {str(p) for p in Path("tests/integration").glob("test_*.py")}
    accounted = set(POSTGRES_MODULES) | set(CI_SKIP_ALLOWLIST)
    assert present == accounted, (
        f"unclassified integration modules: {sorted(present - accounted)}; "
        f"missing from disk: {sorted(accounted - present)}"
    )


def test_allowlisted_skips_state_a_specific_reason() -> None:
    for path, reason in CI_SKIP_ALLOWLIST.items():
        assert Path(path).exists()
        assert len(reason) > 20 and "not supported" not in reason, (
            f"{path}: skip reason must say what environment is required"
        )
