"""Disposable-database migration validation.

Approach chosen (of the three the hardening spec allows): **Alembic with an
injected connection**, passed through ``config.attributes["connection"]`` to a
freshly created, uniquely named ``test_migration_*`` database on the configured
Postgres server. ``env.py`` uses that connection directly and never consults
Settings, so the code path exercised is the real migration -- not a metadata
approximation -- against the real dialect.

Why not the alternatives. Metadata-based validation
(``Base.metadata.create_all``) never runs the migration at all, so it cannot
catch a defect in the migration script itself, which is exactly what needs
validating. A temporary SQLite file was tried first and **does not work for this
repository**: an earlier migration in the chain alters a constraint, which
SQLite cannot do (``No support for ALTER of constraints in SQLite dialect``), so
the chain cannot be replayed there at all.

Known limitations of this approach, stated rather than hidden:

- It creates and drops a database **on the production Postgres SERVER**. That
  server is shared, so a crash between create and drop can leave a stray
  ``test_migration_*`` database behind. It never connects to, reads, or writes
  the configured production DATABASE, and the name guard plus identity
  fingerprint make targeting it impossible.
- It requires ``CREATE DATABASE`` privileges.
- It proves the upgrade applies cleanly and yields the expected columns,
  indexes, foreign keys, and constraints. It does not prove production
  performance, locking behaviour, or migration duration under load.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as _sync_create_engine
from sqlalchemy import inspect

from kalshi_weather.storage.migration_safety import (
    DISPOSABLE_MODE_ENV,
    INJECTED_CONNECTION_KEY,
    MigrationTargetError,
    TargetIdentity,
    assert_disposable_target,
    target_identity,
)

ALEMBIC_SCRIPT_LOCATION = "src/kalshi_weather/alembic"


@dataclass(frozen=True, slots=True)
class SchemaReport:
    """What a disposable upgrade actually produced."""

    revision: str
    target: str
    tables: tuple[str, ...]
    columns: dict[str, tuple[str, ...]]
    indexes: dict[str, tuple[str, ...]]
    foreign_keys: dict[str, tuple[str, ...]]
    unique_constraints: dict[str, tuple[str, ...]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "target": self.target,
            "tables": list(self.tables),
            "columns": {k: list(v) for k, v in self.columns.items()},
            "indexes": {k: list(v) for k, v in self.indexes.items()},
            "foreign_keys": {k: list(v) for k, v in self.foreign_keys.items()},
            "unique_constraints": {k: list(v) for k, v in self.unique_constraints.items()},
        }


def make_disposable_postgres_url(base_url: str, database: str) -> str:
    """Point ``base_url`` at a different database, preserving nothing else.

    Only the path is replaced; credentials stay in the URL object and are never
    logged or returned by any describe() call.
    """
    parts = urlsplit(base_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", "", ""))


def validate_migration_schema(
    revision: str = "head",
    *,
    tables_of_interest: Sequence[str] = (),
    keep_database: bool = False,
) -> tuple[SchemaReport, TargetIdentity]:
    """Upgrade a throwaway ``test_migration_*`` database to ``revision``.

    The database is created before and dropped after, and the target is asserted
    disposable BEFORE any migration connection is opened. Raises
    ``MigrationTargetError`` if the resolved target is not approved.
    """
    from kalshi_weather.config import get_settings

    base_url = get_settings().database_url
    # psycopg3 serves both sync and async, so keep the +psycopg driver; only an
    # asyncpg URL needs rewriting for the plain-sync DDL used here. Stripping
    # the driver entirely would fall back to psycopg2, which is not installed.
    sync_base = base_url.replace("+asyncpg", "+psycopg")
    db_name = f"test_migration_{uuid.uuid4().hex[:12]}"
    url = make_disposable_postgres_url(sync_base, db_name)
    identity = assert_disposable_target(url)

    admin_url = make_disposable_postgres_url(sync_base, "postgres")
    admin = _sync_create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{db_name}"')
    finally:
        admin.dispose()

    engine = _sync_create_engine(url)
    try:
        cfg = Config()
        cfg.set_main_option("script_location", ALEMBIC_SCRIPT_LOCATION)
        with engine.begin() as connection:
            # The connection is INJECTED: env.py uses it directly and never
            # resolves Settings, so this cannot reach production.
            cfg.attributes[INJECTED_CONNECTION_KEY] = connection
            command.upgrade(cfg, revision)

        insp = inspect(engine)
        tables = tuple(sorted(insp.get_table_names()))
        wanted = tuple(t for t in tables_of_interest if t) or tables
        report = SchemaReport(
            revision=revision,
            target=identity.describe(),
            tables=tables,
            columns={
                t: tuple(c["name"] for c in insp.get_columns(t)) for t in wanted if t in tables
            },
            indexes={
                t: tuple(sorted(i["name"] or "" for i in insp.get_indexes(t)))
                for t in wanted
                if t in tables
            },
            foreign_keys={
                t: tuple(sorted(fk["referred_table"] for fk in insp.get_foreign_keys(t)))
                for t in wanted
                if t in tables
            },
            unique_constraints={
                t: tuple(sorted(u["name"] or "" for u in insp.get_unique_constraints(t)))
                for t in wanted
                if t in tables
            },
        )
        return report, identity
    finally:
        engine.dispose()
        if not keep_database:
            admin = _sync_create_engine(admin_url, isolation_level="AUTOCOMMIT")
            try:
                with admin.connect() as conn:
                    conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{db_name}"')
            finally:
                admin.dispose()


def describe_configured_target() -> str:
    """Credential-free description of the CONFIGURED production target, printed
    before validation so an operator can see what is *not* being touched."""
    try:
        from kalshi_weather.config import get_settings

        return target_identity(get_settings().database_url).describe()
    except Exception:
        return "<unresolved>"


__all__ = [
    "DISPOSABLE_MODE_ENV",
    "MigrationTargetError",
    "SchemaReport",
    "describe_configured_target",
    "make_disposable_postgres_url",
    "validate_migration_schema",
]
