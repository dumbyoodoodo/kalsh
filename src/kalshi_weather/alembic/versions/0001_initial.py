"""initial schema: raw payloads, series, events, market/orderbook snapshots, trades

Revision ID: 0001
Revises:
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "raw_api_payloads",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("endpoint_or_channel", sa.String(255), nullable=False),
        sa.Column("request_key", sa.String(512), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
    )
    op.create_index(
        "ix_raw_api_payloads_source_request_hash",
        "raw_api_payloads",
        ["source", "request_key", "content_hash"],
        unique=True,
    )

    op.create_table(
        "series",
        sa.Column("series_ticker", sa.String(64), primary_key=True),
        sa.Column("category", sa.String(64)),
        sa.Column("title", sa.Text()),
        sa.Column("frequency", sa.String(32)),
        sa.Column("settlement_source", sa.Text()),
        sa.Column("raw_payload_id", sa.BigInteger(), sa.ForeignKey("raw_api_payloads.id")),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "events",
        sa.Column("event_ticker", sa.String(64), primary_key=True),
        sa.Column("series_ticker", sa.String(64), sa.ForeignKey("series.series_ticker")),
        sa.Column("title", sa.Text()),
        sa.Column("status", sa.String(32)),
        sa.Column("open_time", sa.DateTime(timezone=True)),
        sa.Column("close_time", sa.DateTime(timezone=True)),
        sa.Column("settlement_time", sa.DateTime(timezone=True)),
        sa.Column("raw_payload_id", sa.BigInteger(), sa.ForeignKey("raw_api_payloads.id")),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "market_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("market_ticker", sa.String(64), nullable=False),
        sa.Column("event_ticker", sa.String(64)),
        sa.Column("market_type", sa.String(32)),
        sa.Column("title", sa.Text()),
        sa.Column("subtitle", sa.Text()),
        sa.Column("status", sa.String(32)),
        sa.Column("yes_bid_cents", sa.Integer()),
        sa.Column("yes_ask_cents", sa.Integer()),
        sa.Column("last_price_cents", sa.Integer()),
        sa.Column("volume", sa.BigInteger()),
        sa.Column("open_interest", sa.BigInteger()),
        sa.Column("close_time", sa.DateTime(timezone=True)),
        sa.Column("rules_primary", sa.Text()),
        sa.Column("rules_secondary", sa.Text()),
        sa.Column("raw_payload_id", sa.BigInteger(), sa.ForeignKey("raw_api_payloads.id")),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_market_snapshots_market_ticker", "market_snapshots", ["market_ticker"])

    op.create_table(
        "orderbook_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("market_ticker", sa.String(64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("yes_levels_json", sa.JSON(), nullable=False),
        sa.Column("no_levels_json", sa.JSON(), nullable=False),
        sa.Column("best_yes_bid_cents", sa.Integer()),
        sa.Column("best_yes_ask_cents", sa.Integer()),
        sa.Column("best_no_bid_cents", sa.Integer()),
        sa.Column("best_no_ask_cents", sa.Integer()),
        sa.Column("spread_cents", sa.Integer()),
        sa.Column("raw_payload_id", sa.BigInteger(), sa.ForeignKey("raw_api_payloads.id")),
    )
    op.create_index(
        "ix_orderbook_snapshots_market_ticker", "orderbook_snapshots", ["market_ticker"]
    )

    op.create_table(
        "trades",
        sa.Column("trade_id", sa.String(64), primary_key=True),
        sa.Column("market_ticker", sa.String(64), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("taker_side", sa.String(8)),
        sa.Column("raw_payload_id", sa.BigInteger(), sa.ForeignKey("raw_api_payloads.id")),
    )
    op.create_index("ix_trades_market_ticker", "trades", ["market_ticker"])


def downgrade() -> None:
    op.drop_table("trades")
    op.drop_table("orderbook_snapshots")
    op.drop_table("market_snapshots")
    op.drop_table("events")
    op.drop_table("series")
    op.drop_table("raw_api_payloads")
