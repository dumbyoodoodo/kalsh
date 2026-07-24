"""PostgreSQL-backed validation of migration 0010 (ADR 0013).

SQLite (the unit suite) does not enforce CHECK constraints the same way and
cannot prove the constraint/type behavior of this migration -- exactly the
class of gap that let an earlier FK bug through. This test runs the *real*
Alembic migration against a throwaway PostgreSQL database on the same server as
the configured `DATABASE_URL`, then drops it. It skips cleanly if no PostgreSQL
server is reachable, so it never touches the real project database.
"""

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

_TABLES = (
    "market_snapshots",
    "trades",
    "orderbook_snapshots",
    "market_candlesticks",
    "settlement_attempts",
    "raw_api_payloads",
)
_TEST_DB = "kalshi_provenance_it"


def _with_database(url: str, database: str) -> str:
    # render_as_string(hide_password=False) -- plain str(make_url()) masks the
    # password as '***', which then fails authentication.
    return make_url(url).set(database=database).render_as_string(hide_password=False)


def _admin_url() -> str:
    from kalshi_weather.config import get_settings

    real = str(get_settings().database_url)
    assert make_url(real).database != _TEST_DB, "test DB name must differ from the real one"
    return _with_database(real, "postgres")


@pytest.fixture
def throwaway_db():  # type: ignore[no-untyped-def]
    admin = _admin_url()
    try:
        eng = create_engine(admin, isolation_level="AUTOCOMMIT")
        with eng.connect() as c:
            c.execute(text("select 1"))
    except Exception:
        pytest.skip("no reachable PostgreSQL server for integration test")

    with eng.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {_TEST_DB}"))
        c.execute(text(f"CREATE DATABASE {_TEST_DB}"))
    test_url = _with_database(admin, _TEST_DB)
    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = test_url
    try:
        yield test_url
    finally:
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev
        with eng.connect() as c:
            c.execute(
                text(
                    "select pg_terminate_backend(pid) from pg_stat_activity "
                    "where datname = :d and pid <> pg_backend_pid()"
                ),
                {"d": _TEST_DB},
            )
            c.execute(text(f"DROP DATABASE IF EXISTS {_TEST_DB}"))
        eng.dispose()


def _alembic(url: str):  # type: ignore[no-untyped-def]
    from alembic.config import Config

    cfg = Config("alembic.ini")
    return cfg


def test_migration_0010_upgrade_constraint_and_downgrade(throwaway_db: str) -> None:
    from alembic import command

    cfg = _alembic(throwaway_db)
    command.upgrade(cfg, "0010")

    eng = create_engine(throwaway_db)
    with eng.connect() as c:
        assert c.execute(text("select version_num from alembic_version")).scalar() == "0010"
        # column exists, correct type, nullable, on every in-scope table
        for t in _TABLES:
            row = c.execute(
                text(
                    "select data_type, is_nullable from information_schema.columns "
                    "where table_name = :t and column_name = 'environment'"
                ),
                {"t": t},
            ).one()
            assert row.data_type == "character varying"
            assert row.is_nullable == "YES"

    with eng.begin() as c:
        # each allowed value accepted, plus NULL
        c.execute(
            text(
                "insert into raw_api_payloads "
                "(source, endpoint_or_channel, request_key, received_at, http_status, "
                " content_hash, payload_json, schema_version, environment) values "
                "('kalshi_rest','/m','k1',now(),200,'h1','{}','1','demo'),"
                "('kalshi_rest','/m','k2',now(),200,'h2','{}','1','production'),"
                "('kalshi_rest','/m','k3',now(),200,'h3','{}','1','unknown'),"
                "('kalshi_rest','/m','k4',now(),200,'h4','{}','1',NULL)"
            )
        )
        n = c.execute(text("select count(*) from raw_api_payloads")).scalar()
        assert n == 4

    # invalid value rejected by the CHECK constraint
    with eng.begin() as c, pytest.raises(Exception) as exc:
        c.execute(
            text(
                "insert into raw_api_payloads "
                "(source, endpoint_or_channel, request_key, received_at, http_status, "
                " content_hash, payload_json, schema_version, environment) values "
                "('kalshi_rest','/m','k5',now(),200,'h5','{}','1','staging')"
            )
        )
    assert "check" in str(exc.value).lower() or "constraint" in str(exc.value).lower()

    # downgrade removes the column cleanly on every table
    command.downgrade(cfg, "0009")
    with eng.connect() as c:
        assert c.execute(text("select version_num from alembic_version")).scalar() == "0009"
        for t in _TABLES:
            present = c.execute(
                text(
                    "select 1 from information_schema.columns "
                    "where table_name = :t and column_name = 'environment'"
                ),
                {"t": t},
            ).scalar()
            assert present is None

    # re-upgrade is repeatable
    command.upgrade(cfg, "0010")
    with eng.connect() as c:
        assert c.execute(text("select version_num from alembic_version")).scalar() == "0010"
    eng.dispose()
