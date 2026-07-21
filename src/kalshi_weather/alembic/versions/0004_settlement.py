"""settlement resolution: versioned settlement_specs table

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-21

Additive only: one new table, no changes to existing tables or data.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "settlement_specs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("market_ticker", sa.String(64), nullable=False),
        sa.Column("series_ticker", sa.String(64), nullable=False),
        sa.Column("event_ticker", sa.String(64)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("confidence", sa.String(16), nullable=False),
        sa.Column("city", sa.String(64)),
        sa.Column("station_id", sa.String(32)),
        sa.Column("variable", sa.String(32)),
        sa.Column("target_date", sa.Date()),
        sa.Column("settlement_source", sa.Text()),
        sa.Column("source_url", sa.Text()),
        sa.Column("wfo_site", sa.String(8)),
        sa.Column("source_location_code", sa.String(16)),
        sa.Column("unit", sa.String(8)),
        sa.Column("observation_window", sa.String(32)),
        sa.Column("rounding_rule", sa.String(32)),
        sa.Column("market_close_time", sa.DateTime()),
        sa.Column("notes_json", sa.JSON(), nullable=False),
        sa.Column("parser_version", sa.String(16), nullable=False),
        sa.Column("rules_hash", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(16), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_settlement_specs_dedup",
        "settlement_specs",
        ["market_ticker", "parser_version", "rules_hash"],
        unique=True,
    )
    op.create_index("ix_settlement_specs_market", "settlement_specs", ["market_ticker"])


def downgrade() -> None:
    op.drop_index("ix_settlement_specs_market", table_name="settlement_specs")
    op.drop_index("ix_settlement_specs_dedup", table_name="settlement_specs")
    op.drop_table("settlement_specs")
