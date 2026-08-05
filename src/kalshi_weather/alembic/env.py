"""Alembic environment.

Hardened after the 2026-08-04 incident: ``get_url()`` previously returned
``get_settings().database_url`` unconditionally, so a caller that set
``sqlalchemy.url`` to a disposable database had that URL SILENTLY DISCARDED and
migrated production instead. Three rules now hold:

1. An injected connection (``config.attributes["connection"]``) is used
   directly and is never replaced by Settings.
2. A caller-supplied ``sqlalchemy.url`` is either honoured (disposable mode,
   approved target) or refused loudly. It is never quietly ignored.
3. Disposable targets must prove they are disposable *before* a connection is
   opened -- an opt-in flag alone is not sufficient.

Production deployment is unchanged: with no injected connection and no
caller-supplied URL, the Settings-derived database is used exactly as before.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import AsyncEngine

from kalshi_weather.config import get_settings
from kalshi_weather.storage.database import create_engine
from kalshi_weather.storage.migration_safety import (
    INJECTED_CONNECTION_KEY,
    assert_disposable_target,
    assert_no_silent_override,
    disposable_mode_enabled,
)
from kalshi_weather.storage.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _caller_url() -> str | None:
    """A ``sqlalchemy.url`` explicitly supplied by the caller, if any.

    ``alembic.ini`` may itself carry a placeholder; only a value that differs
    from the file's own default counts as caller intent.
    """
    try:
        url = config.get_main_option("sqlalchemy.url", None)
    except Exception:  # pragma: no cover - alembic version differences
        url = None
    return url or None


def resolve_url() -> str:
    """The URL this migration will actually run against.

    Refuses the incident's shape (caller URL + production default) rather than
    silently preferring Settings.
    """
    caller = _caller_url()
    assert_no_silent_override(caller)
    if caller and disposable_mode_enabled():
        assert_disposable_target(caller)
        return caller
    return get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: object) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)  # type: ignore[arg-type]
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable: AsyncEngine = create_engine(resolve_url())
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


#: A caller may inject an already-created disposable connection. It is used as
#: given -- no second engine, no Settings lookup, no substitution.
injected = config.attributes.get(INJECTED_CONNECTION_KEY)

if injected is not None:
    do_run_migrations(injected)
elif context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
