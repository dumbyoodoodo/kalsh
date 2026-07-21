"""ingestion collector: source timestamps, schema versions, snapshot dedup hashes

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "series", sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "series",
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
    )

    op.add_column("events", sa.Column("category", sa.String(64), nullable=True))
    op.add_column("events", sa.Column("sub_title", sa.Text(), nullable=True))
    op.add_column(
        "events", sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "events",
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
    )

    op.add_column(
        "market_snapshots",
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "market_snapshots",
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
    )
    op.add_column("market_snapshots", sa.Column("content_hash", sa.String(64), nullable=True))
    op.create_index(
        "ix_market_snapshots_content_hash", "market_snapshots", ["content_hash"]
    )

    op.add_column(
        "orderbook_snapshots",
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
    )
    op.add_column("orderbook_snapshots", sa.Column("content_hash", sa.String(64), nullable=True))
    op.create_index(
        "ix_orderbook_snapshots_content_hash", "orderbook_snapshots", ["content_hash"]
    )

    op.add_column(
        "trades",
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("trades", "schema_version")

    op.drop_index("ix_orderbook_snapshots_content_hash", table_name="orderbook_snapshots")
    op.drop_column("orderbook_snapshots", "content_hash")
    op.drop_column("orderbook_snapshots", "schema_version")

    op.drop_index("ix_market_snapshots_content_hash", table_name="market_snapshots")
    op.drop_column("market_snapshots", "content_hash")
    op.drop_column("market_snapshots", "schema_version")
    op.drop_column("market_snapshots", "source_updated_at")

    op.drop_column("events", "schema_version")
    op.drop_column("events", "source_updated_at")
    op.drop_column("events", "sub_title")
    op.drop_column("events", "category")

    op.drop_column("series", "schema_version")
    op.drop_column("series", "source_updated_at")
