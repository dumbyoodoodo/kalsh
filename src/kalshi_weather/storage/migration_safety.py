"""Guards that make it impossible for a disposable/test migration to run
against the production database.

The incident this closes (2026-08-04). A disposable-database validation script
built an Alembic ``Config``, set ``sqlalchemy.url`` to a temporary SQLite file,
and called ``command.upgrade(cfg, "0012")``. ``alembic/env.py``'s ``get_url()``
returned ``get_settings().database_url`` **unconditionally**, so the
caller-supplied URL was silently discarded and the migration ran against
production, creating the table there. It was empty and was reverted, but the
failure mode is general: *any* Alembic ``command.*`` call in this repository
targeted production regardless of the URL passed.

Two design choices follow from that:

- **Silent replacement is now impossible.** If a caller supplies a URL or a
  connection, ``env.py`` either honours it or refuses loudly. It never quietly
  substitutes Settings.
- **Disposable mode is opt-in and must prove its target.** A boolean flag is not
  enough -- the incident script would have happily set one. The target itself
  must be verifiably disposable, and it is checked *before* a connection is
  opened.

No secret is stored or logged here. Production identity is compared as a
salted-free sha256 fingerprint over ``(scheme-family, host, port, database)``
only -- never the password, never the full URL.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

#: Environment variable that opts a process into disposable-migration mode.
#: Necessary but NOT sufficient: the target must still prove it is disposable.
DISPOSABLE_MODE_ENV = "KALSHI_MIGRATION_DISPOSABLE"

#: Alembic ``config.attributes`` key through which a caller injects an already
#: created disposable connection.
INJECTED_CONNECTION_KEY = "connection"

#: Postgres databases must carry this prefix to be accepted as disposable.
TEST_DB_PREFIX = "test_"

REFUSAL_NOT_DISPOSABLE = "REFUSED: the migration target is not an approved disposable database"
REFUSAL_PRODUCTION_IDENTITY = (
    "REFUSED: the migration target is not an approved disposable database "
    "(it matches the production database identity)"
)
REFUSAL_SILENT_OVERRIDE = (
    "REFUSED: a caller supplied sqlalchemy.url but disposable mode is not enabled. "
    "Alembic would otherwise silently ignore it and migrate PRODUCTION "
    f"(set {DISPOSABLE_MODE_ENV}=1 and use an approved disposable target)"
)


class MigrationTargetError(RuntimeError):
    """The requested migration target is not permitted. Raised before any
    connection is opened."""


@dataclass(frozen=True, slots=True)
class TargetIdentity:
    """Non-secret identity of a database target, safe to print and compare."""

    scheme_family: str
    host: str
    port: str
    database: str

    @property
    def fingerprint(self) -> str:
        raw = f"{self.scheme_family}|{self.host}|{self.port}|{self.database}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def describe(self) -> str:
        """Human-readable, credential-free description."""
        if self.scheme_family == "sqlite":
            return f"sqlite:{self.database}"
        return f"{self.scheme_family}://{self.host}:{self.port}/{self.database}"


def target_identity(url: str) -> TargetIdentity:
    """Extract the non-secret identity of a database URL.

    Userinfo (which carries the password) is discarded by construction -- only
    the scheme family, host, port, and database name are retained.
    """
    parts = urlsplit(url)
    scheme_family = parts.scheme.split("+", 1)[0].lower()
    if scheme_family == "sqlite":
        # sqlite:///path  ->  path (or ":memory:")
        database = url.split("///", 1)[1] if "///" in url else ":memory:"
        return TargetIdentity("sqlite", "", "", database)
    return TargetIdentity(
        scheme_family=scheme_family,
        host=(parts.hostname or "").lower(),
        port=str(parts.port or ""),
        database=parts.path.lstrip("/"),
    )


def production_identity() -> TargetIdentity | None:
    """Identity of the configured production database, or ``None`` if Settings
    cannot be resolved (e.g. in a bare unit-test process)."""
    try:
        from kalshi_weather.config import get_settings

        return target_identity(get_settings().database_url)
    except Exception:
        return None


def disposable_mode_enabled(env: dict[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return str(source.get(DISPOSABLE_MODE_ENV, "")).strip().lower() in {"1", "true", "yes"}


def _is_temp_sqlite(identity: TargetIdentity) -> bool:
    """A SQLite target that is in memory, or a file under the OS temp dir.

    Path containment is resolved on both sides so ``/tmp/../etc/x.db`` cannot
    masquerade as temporary.
    """
    if identity.scheme_family != "sqlite":
        return False
    database = identity.database
    if not database or database == ":memory:":
        return True
    try:
        resolved = Path(database).resolve()
    except OSError:  # pragma: no cover - unresolvable path is not approved
        return False
    temp_roots = {Path(tempfile.gettempdir()).resolve()}
    # macOS reports /var/folders/... whose realpath differs from /tmp; accept
    # both the reported and resolved forms of the same root.
    with_private = Path("/private") / Path(tempfile.gettempdir()).resolve().relative_to("/")
    temp_roots.add(with_private)
    return any(resolved == root or root in resolved.parents for root in temp_roots)


def _is_test_postgres(identity: TargetIdentity) -> bool:
    """A Postgres target whose database name explicitly declares itself a test
    database. The prefix is required so an operator cannot reach production by
    forgetting a flag."""
    return identity.scheme_family in {"postgresql", "postgres"} and identity.database.startswith(
        TEST_DB_PREFIX
    )


def is_approved_disposable_target(url: str) -> bool:
    """Whether ``url`` is a target a disposable migration may run against.

    Deliberately NOT based on the absence of production credentials or on a
    hostname string: the incident script had valid production credentials and a
    perfectly ordinary hostname. The target must positively prove it is
    disposable.
    """
    identity = target_identity(url)
    production = production_identity()
    if production is not None and identity.fingerprint == production.fingerprint:
        return False
    return _is_temp_sqlite(identity) or _is_test_postgres(identity)


def assert_disposable_target(url: str) -> TargetIdentity:
    """Fail closed unless ``url`` is an approved disposable target.

    Called before any connection is opened, so a refused target is never even
    dialled.
    """
    identity = target_identity(url)
    production = production_identity()
    if production is not None and identity.fingerprint == production.fingerprint:
        raise MigrationTargetError(f"{REFUSAL_PRODUCTION_IDENTITY}: {identity.describe()}")
    if not (_is_temp_sqlite(identity) or _is_test_postgres(identity)):
        raise MigrationTargetError(
            f"{REFUSAL_NOT_DISPOSABLE}: {identity.describe()} "
            f"(approved: in-memory SQLite, a SQLite file under {tempfile.gettempdir()}, "
            f"or a Postgres database named {TEST_DB_PREFIX}*)"
        )
    return identity


def assert_no_silent_override(caller_url: str | None, *, env: dict[str, str] | None = None) -> None:
    """Refuse the exact shape of the 2026-08-04 incident.

    A caller that supplies ``sqlalchemy.url`` without enabling disposable mode
    previously had that URL silently discarded and migrated production instead.
    That combination is now an error, not a surprise.
    """
    if caller_url and not disposable_mode_enabled(env):
        raise MigrationTargetError(
            f"{REFUSAL_SILENT_OVERRIDE}: {target_identity(caller_url).describe()}"
        )
