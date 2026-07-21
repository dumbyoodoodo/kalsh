"""price ingestion: market_candlesticks table

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-21

Additive only: one new table, no changes to existing tables or data.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_candlesticks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("market_ticker", sa.String(64), nullable=False),
        sa.Column("series_ticker", sa.String(64), nullable=False),
        sa.Column("period_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.DateTime(), nullable=False),
        sa.Column("period_end", sa.DateTime(), nullable=False),
        sa.Column("price_open_cents", sa.Integer()),
        sa.Column("price_high_cents", sa.Integer()),
        sa.Column("price_low_cents", sa.Integer()),
        sa.Column("price_close_cents", sa.Integer()),
        sa.Column("price_mean_cents", sa.Integer()),
        sa.Column(
            "price_close_is_carried_forward", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("yes_bid_open_cents", sa.Integer()),
        sa.Column("yes_bid_high_cents", sa.Integer()),
        sa.Column("yes_bid_low_cents", sa.Integer()),
        sa.Column("yes_bid_close_cents", sa.Integer()),
        sa.Column("yes_ask_open_cents", sa.Integer()),
        sa.Column("yes_ask_high_cents", sa.Integer()),
        sa.Column("yes_ask_low_cents", sa.Integer()),
        sa.Column("yes_ask_close_cents", sa.Integer()),
        sa.Column("volume", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("open_interest", sa.Integer()),
        sa.Column("schema_version", sa.String(16), nullable=False),
        sa.Column(
            "raw_payload_id", sa.BigInteger(), sa.ForeignKey("raw_api_payloads.id")
        ),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_market_candlesticks_dedup",
        "market_candlesticks",
        ["market_ticker", "period_interval_seconds", "period_end"],
        unique=True,
    )
    op.create_index(
        "ix_market_candlesticks_lookup",
        "market_candlesticks",
        ["market_ticker", "period_interval_seconds", "period_end"],
    )


def downgrade() -> None:
    op.drop_index("ix_market_candlesticks_lookup", table_name="market_candlesticks")
    op.drop_index("ix_market_candlesticks_dedup", table_name="market_candlesticks")
    op.drop_table("market_candlesticks")
