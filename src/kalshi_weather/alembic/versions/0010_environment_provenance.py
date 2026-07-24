"""Kalshi source-environment provenance

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-24

Additive only, per ADR 0013. Adds a nullable ``environment`` column to every
table that stores environment-dependent Kalshi market/trade/price/liquidity/
order-book/settlement data, plus the raw-payload table (whose path-only
endpoint cannot distinguish demo from production hosts).

Every existing row keeps ``environment IS NULL`` -- the historical-unknown
representation. No existing row is read, rewritten, or backfilled: the source
environment of past rows genuinely cannot be recovered from stored data, and
guessing would be worse than an honest NULL.

A CHECK constraint restricts new values to the three canonical strings while
still permitting NULL, so historical rows remain valid and a stray value is
rejected. No primary key, foreign key, uniqueness constraint, or existing
index is touched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Tables that carry environment-dependent Kalshi data (see ADR 0013 for the
#: per-table inclusion rationale). raw_api_payloads is included as the
#: provenance anchor for newly written Kalshi payloads.
_TABLES = (
    "market_snapshots",
    "trades",
    "orderbook_snapshots",
    "market_candlesticks",
    "settlement_attempts",
    "raw_api_payloads",
)

_ALLOWED = ("demo", "production", "unknown")


def _check_name(table: str) -> str:
    return f"ck_{table}_environment"


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("environment", sa.String(16), nullable=True))
        # NULL stays valid (historical rows); any non-NULL value must be canonical.
        values = ", ".join(f"'{v}'" for v in _ALLOWED)
        op.create_check_constraint(
            _check_name(table),
            table,
            f"environment IS NULL OR environment IN ({values})",
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_constraint(_check_name(table), table, type_="check")
        op.drop_column(table, "environment")
