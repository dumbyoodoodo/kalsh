"""settlement-time labels: market settlement fields

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-21

Additive only: six nullable columns on market_snapshots (result,
expiration_value, settlement_ts, floor_strike, cap_strike, strike_type).
No existing data touched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("market_snapshots", sa.Column("result", sa.String(16), nullable=True))
    op.add_column(
        "market_snapshots", sa.Column("expiration_value", sa.Numeric(10, 2), nullable=True)
    )
    op.add_column("market_snapshots", sa.Column("settlement_ts", sa.DateTime(), nullable=True))
    op.add_column(
        "market_snapshots", sa.Column("floor_strike", sa.Numeric(10, 2), nullable=True)
    )
    op.add_column("market_snapshots", sa.Column("cap_strike", sa.Numeric(10, 2), nullable=True))
    op.add_column("market_snapshots", sa.Column("strike_type", sa.String(16), nullable=True))


def downgrade() -> None:
    for column in (
        "strike_type",
        "cap_strike",
        "floor_strike",
        "settlement_ts",
        "expiration_value",
        "result",
    ):
        op.drop_column("market_snapshots", column)
