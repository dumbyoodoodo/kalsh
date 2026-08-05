"""Migration-target safety guards.

The regression these lock down is the 2026-08-04 incident: a disposable
validation script set ``sqlalchemy.url`` to a temp SQLite file, ``env.py``
ignored it and returned ``get_settings().database_url``, and
``command.upgrade`` migrated PRODUCTION.

Every test here is synthetic. No production connection is opened -- Settings is
monkeypatched wherever production identity matters.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from kalshi_weather.storage import migration_safety as ms

PROD_URL = "postgresql+psycopg://user:pw@localhost:5432/kalshi_weather"
TEMP_DIR = Path(tempfile.gettempdir())


@pytest.fixture
def prod(monkeypatch: pytest.MonkeyPatch):
    """Pin a synthetic production identity without touching real Settings."""
    monkeypatch.setattr(ms, "production_identity", lambda: ms.target_identity(PROD_URL))


# --- identity extraction -----------------------------------------------------


def test_identity_discards_credentials() -> None:
    ident = ms.target_identity(PROD_URL)
    assert ident.host == "localhost" and ident.database == "kalshi_weather"
    described = ident.describe()
    assert "user" not in described and "pw" not in described


def test_fingerprint_is_stable_and_credential_independent() -> None:
    a = ms.target_identity("postgresql+psycopg://u1:p1@localhost:5432/kalshi_weather")
    b = ms.target_identity("postgresql+asyncpg://u2:p2@localhost:5432/kalshi_weather")
    assert a.fingerprint == b.fingerprint, "same database, different creds -> same identity"


def test_different_database_name_is_a_different_identity() -> None:
    a = ms.target_identity(PROD_URL)
    b = ms.target_identity(PROD_URL.replace("kalshi_weather", "test_migration_x"))
    assert a.fingerprint != b.fingerprint


# --- approved / refused targets ----------------------------------------------


def test_temp_sqlite_file_accepted(prod) -> None:
    assert ms.is_approved_disposable_target(f"sqlite:///{TEMP_DIR}/x.db") is True


def test_in_memory_sqlite_accepted(prod) -> None:
    assert ms.is_approved_disposable_target("sqlite+aiosqlite:///:memory:") is True


def test_sqlite_outside_temp_refused(prod) -> None:
    assert ms.is_approved_disposable_target("sqlite:////var/lib/kalshi/real.db") is False


def test_sqlite_path_traversal_out_of_temp_refused(prod) -> None:
    """`/tmp/../etc/x.db` must not masquerade as temporary."""
    assert ms.is_approved_disposable_target(f"sqlite:///{TEMP_DIR}/../etc/x.db") is False


def test_test_prefixed_postgres_accepted(prod) -> None:
    url = PROD_URL.replace("kalshi_weather", "test_migration_abc")
    assert ms.is_approved_disposable_target(url) is True


def test_production_database_name_refused(prod) -> None:
    assert ms.is_approved_disposable_target(PROD_URL) is False


def test_untagged_postgres_refused(prod) -> None:
    """A non-production Postgres database without the test_ prefix is still
    refused: forgetting the prefix must not silently permit a real database."""
    url = PROD_URL.replace("kalshi_weather", "staging")
    assert ms.is_approved_disposable_target(url) is False


def test_production_identity_beats_test_prefix(prod, monkeypatch) -> None:
    """Even a test_-prefixed name is refused if it IS the production DB."""
    monkeypatch.setattr(
        ms,
        "production_identity",
        lambda: ms.target_identity(PROD_URL.replace("kalshi_weather", "test_looks_safe")),
    )
    url = PROD_URL.replace("kalshi_weather", "test_looks_safe")
    assert ms.is_approved_disposable_target(url) is False


# --- assertion / refusal messages --------------------------------------------


def test_assert_refuses_production_with_clear_message(prod) -> None:
    with pytest.raises(ms.MigrationTargetError) as exc:
        ms.assert_disposable_target(PROD_URL)
    assert ms.REFUSAL_NOT_DISPOSABLE.split(":")[0] in str(exc.value)
    assert "production database identity" in str(exc.value)


def test_assert_refuses_non_disposable_with_clear_message(prod) -> None:
    with pytest.raises(ms.MigrationTargetError, match="not an approved disposable database"):
        ms.assert_disposable_target("postgresql://localhost:5432/staging")


def test_refusal_message_never_contains_credentials(prod) -> None:
    with pytest.raises(ms.MigrationTargetError) as exc:
        ms.assert_disposable_target(PROD_URL)
    assert "pw" not in str(exc.value) and "user:" not in str(exc.value)


def test_assert_returns_identity_for_approved_target(prod) -> None:
    ident = ms.assert_disposable_target(f"sqlite:///{TEMP_DIR}/ok.db")
    assert ident.scheme_family == "sqlite"


# --- the incident's exact shape ----------------------------------------------


def test_incident_regression_caller_url_without_disposable_mode_is_refused() -> None:
    """THE regression test.

    2026-08-04: a caller set sqlalchemy.url to a disposable database, env.py
    silently substituted Settings, and production was migrated. That exact
    combination -- a caller-supplied URL with disposable mode off -- is now a
    hard error instead of a silent substitution."""
    with pytest.raises(ms.MigrationTargetError) as exc:
        ms.assert_no_silent_override(f"sqlite:///{TEMP_DIR}/incident.db", env={})
    message = str(exc.value)
    assert "silently ignore" in message
    assert "PRODUCTION" in message
    assert ms.DISPOSABLE_MODE_ENV in message


def test_no_caller_url_is_the_normal_production_path() -> None:
    """Deployment supplies no URL and must be unaffected."""
    ms.assert_no_silent_override(None, env={})


def test_caller_url_with_disposable_mode_is_permitted() -> None:
    ms.assert_no_silent_override(f"sqlite:///{TEMP_DIR}/ok.db", env={ms.DISPOSABLE_MODE_ENV: "1"})


@pytest.mark.parametrize(
    "value,expected",
    [("1", True), ("true", True), ("YES", True), ("0", False), ("", False), ("maybe", False)],
)
def test_disposable_mode_parsing(value: str, expected: bool) -> None:
    assert ms.disposable_mode_enabled({ms.DISPOSABLE_MODE_ENV: value}) is expected


def test_boolean_flag_alone_does_not_approve_a_target(prod) -> None:
    """Opting in is necessary but NOT sufficient -- the incident script would
    have set the flag too."""
    assert ms.disposable_mode_enabled({ms.DISPOSABLE_MODE_ENV: "1"}) is True
    with pytest.raises(ms.MigrationTargetError):
        ms.assert_disposable_target(PROD_URL)


# --- env.py wiring -----------------------------------------------------------


def test_env_py_honours_injected_connection_and_never_substitutes() -> None:
    source = Path("src/kalshi_weather/alembic/env.py").read_text()
    assert "INJECTED_CONNECTION_KEY" in source
    assert "assert_no_silent_override" in source
    assert "assert_disposable_target" in source
    # The old unconditional substitution must be gone.
    assert "def get_url() -> str:\n    return get_settings().database_url" not in source
    # Injected connection is used directly, before any engine is created.
    assert source.index("if injected is not None:") < source.index("asyncio.run(")


def test_env_py_still_uses_settings_for_real_deployment() -> None:
    source = Path("src/kalshi_weather/alembic/env.py").read_text()
    assert "return get_settings().database_url" in source


def test_validation_helper_asserts_before_connecting() -> None:
    source = Path("src/kalshi_weather/storage/migration_validation.py").read_text()
    create_stmt = "CREATE " + "DATABASE"
    guard_at = source.index("assert_disposable_target(url)")
    # The first CREATE DATABASE in executable code (skip the module docstring).
    exec_at = source.index("exec_driver_sql(f'" + create_stmt)
    assert guard_at < exec_at, "target must be asserted before any DDL connection"


def test_validation_helper_names_databases_with_test_prefix() -> None:
    from kalshi_weather.storage import migration_validation as mv

    source = Path(mv.__file__).read_text()
    assert 'f"test_migration_{uuid.uuid4().hex' in source


_GUARD = "# --- isolation " + "guard"


# --- isolation guard ---------------------------------------------------------


def test_no_production_connection_attempted_in_this_module() -> None:
    """Scans only the region above the guard header, so the guard's own
    forbidden-token literals do not trip it."""
    region = Path(__file__).read_text().split(_GUARD)[0]
    assert len(region) > 4_000, "guard region split failed"
    for banned in ("create_engine(", "psycopg.connect", "docker exec"):
        assert banned not in region, banned
