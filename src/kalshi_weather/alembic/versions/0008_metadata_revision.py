"""post-expiration settled metadata revision capture

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-23

Additive only, per ADR 0011:

- `market_snapshots.expiration_time` (nullable TIMESTAMPTZ) -- the venue's own
  finality marker. Declared timezone-aware to match `close_time` (migration
  0001), the semantically parallel field; `settlement_ts` in 0006 is naive,
  which is a pre-existing inconsistency this migration deliberately does not
  propagate.
- `market_metadata_verifications` -- append-only record of post-finality
  re-checks, so that a verification finding *no change* (which correctly
  appends no snapshot) is still distinguishable from never having checked.

No existing row is read, rewritten, or backfilled. Every pre-existing
`market_snapshots` row keeps `expiration_time IS NULL`, which the revision
pass handles via its documented close_time fallback.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "market_snapshots",
        sa.Column("expiration_time", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "market_metadata_verifications",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("market_ticker", sa.String(64), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finality_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column(
            "snapshot_appended", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("changed_fields", sa.String(128), nullable=True),
        sa.Column(
            "raw_payload_id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            sa.ForeignKey("raw_api_payloads.id"),
            nullable=True,
        ),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
    )
    op.create_index(
        "ix_market_metadata_verifications_market_ticker",
        "market_metadata_verifications",
        ["market_ticker"],
    )
    op.create_index(
        "ix_market_metadata_verifications_ticker",
        "market_metadata_verifications",
        ["market_ticker", "verified_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_market_metadata_verifications_ticker", table_name="market_metadata_verifications"
    )
    op.drop_index(
        "ix_market_metadata_verifications_market_ticker",
        table_name="market_metadata_verifications",
    )
    op.drop_table("market_metadata_verifications")
    op.drop_column("market_snapshots", "expiration_time")
