"""per-ticker polling evidence ledger

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-27

Additive only, per ADR 0020: one new append-only table, `market_poll_attempts`,
recording per-ticker per-endpoint polling evidence for FUTURE collector cycles.
No existing table or row is read, altered, or backfilled -- collector_runs and
the snapshot tables are untouched, and NO historical polling evidence is inferred
(the table is empty until the collector next runs against a 0011 database).

Indexes support the two access patterns the availability builder and inspection
tooling need: bounded (ticker, time-range) lookups and per-cycle joins, plus an
endpoint/outcome index for operational summaries.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_poll_attempts",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "collector_run_id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            sa.ForeignKey("collector_runs.id"),
            nullable=False,
        ),
        sa.Column("ticker", sa.String(64), nullable=False),
        sa.Column("endpoint_type", sa.String(24), nullable=False),
        sa.Column("environment", sa.String(16), nullable=True),
        sa.Column("eligibility_state", sa.String(24), nullable=False),
        sa.Column("attempt_state", sa.String(24), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("rate_limited", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "persisted_row_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("deduplicated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "raw_payload_id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            sa.ForeignKey("raw_api_payloads.id"),
            nullable=True,
        ),
        sa.Column("error_class", sa.String(64), nullable=True),
        sa.Column("bounded_error_detail", sa.String(500), nullable=True),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_market_poll_attempts_ticker_time", "market_poll_attempts", ["ticker", "requested_at"]
    )
    op.create_index(
        "ix_market_poll_attempts_run", "market_poll_attempts", ["collector_run_id"]
    )
    op.create_index(
        "ix_market_poll_attempts_endpoint_outcome",
        "market_poll_attempts",
        ["endpoint_type", "outcome"],
    )


def downgrade() -> None:
    for name in (
        "ix_market_poll_attempts_endpoint_outcome",
        "ix_market_poll_attempts_run",
        "ix_market_poll_attempts_ticker_time",
    ):
        op.drop_index(name, table_name="market_poll_attempts")
    op.drop_table("market_poll_attempts")
