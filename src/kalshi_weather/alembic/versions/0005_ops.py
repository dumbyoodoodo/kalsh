"""research operations: collector_runs metrics table

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-21

Additive only: one new table, no changes to existing tables or data.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collector_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("collector", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("requests_attempted", sa.Integer(), nullable=False),
        sa.Column("retries", sa.Integer(), nullable=False),
        sa.Column("stats_json", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("schema_version", sa.String(16), nullable=False),
    )
    op.create_index(
        "ix_collector_runs_collector", "collector_runs", ["collector", "started_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_collector_runs_collector", table_name="collector_runs")
    op.drop_table("collector_runs")
