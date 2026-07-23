"""settlement attempt ledger (queue fairness)

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-23

Additive only, per ADR 0012: one new append-only table,
`settlement_attempts`. No existing table or row is read, altered, or
backfilled -- in particular `market_snapshots` is untouched, and retry state
is deliberately NOT added to it because it is append-only.

Indexes support the three access patterns the queue needs: latest attempt per
ticker, retry-eligibility scans (`next_attempt_at`), and terminal-outcome
anti-joins.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "settlement_attempts",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("market_ticker", sa.String(64), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("detail", sa.String(300), nullable=True),
        sa.Column(
            "raw_payload_id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            sa.ForeignKey("raw_api_payloads.id"),
            nullable=True,
        ),
        sa.Column(
            "snapshot_id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            sa.ForeignKey("market_snapshots.id"),
            nullable=True,
        ),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
    )
    op.create_index(
        "ix_settlement_attempts_market_ticker", "settlement_attempts", ["market_ticker"]
    )
    op.create_index(
        "ix_settlement_attempts_latest", "settlement_attempts", ["market_ticker", "attempted_at"]
    )
    op.create_index(
        "ix_settlement_attempts_retry", "settlement_attempts", ["retryable", "next_attempt_at"]
    )
    op.create_index("ix_settlement_attempts_outcome", "settlement_attempts", ["outcome"])


def downgrade() -> None:
    for name in (
        "ix_settlement_attempts_outcome",
        "ix_settlement_attempts_retry",
        "ix_settlement_attempts_latest",
        "ix_settlement_attempts_market_ticker",
    ):
        op.drop_index(name, table_name="settlement_attempts")
    op.drop_table("settlement_attempts")
