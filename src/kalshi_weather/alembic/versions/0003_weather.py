"""weather historical data platform: stations, observations, forecasts

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "weather_stations",
        sa.Column("station_id", sa.String(32), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("source_location_code", sa.String(16), nullable=False),
        sa.Column("office", sa.String(8)),
        sa.Column("latitude", sa.Numeric(9, 6), nullable=False),
        sa.Column("longitude", sa.Numeric(9, 6), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
        sa.Column("raw_payload_id", sa.BigInteger(), sa.ForeignKey("raw_api_payloads.id")),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "weather_observations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "station_id", sa.String(32), sa.ForeignKey("weather_stations.station_id"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("variable", sa.String(32), nullable=False),
        sa.Column("value", sa.Numeric(6, 2), nullable=False),
        sa.Column("unit", sa.String(8), nullable=False),
        sa.Column("observation_date", sa.Date(), nullable=False),
        sa.Column("issuance_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_product_id", sa.String(128), nullable=False),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
        sa.Column("raw_payload_id", sa.BigInteger(), sa.ForeignKey("raw_api_payloads.id")),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_weather_observations_dedup",
        "weather_observations",
        ["station_id", "variable", "issuance_time"],
        unique=True,
    )
    op.create_index(
        "ix_weather_observations_lookup",
        "weather_observations",
        ["station_id", "variable", "observation_date"],
    )

    op.create_table(
        "weather_forecasts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "station_id", sa.String(32), sa.ForeignKey("weather_stations.station_id"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("variable", sa.String(32), nullable=False),
        sa.Column("point_estimate", sa.Numeric(6, 2), nullable=False),
        sa.Column("unit", sa.String(8), nullable=False),
        sa.Column("issue_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
        sa.Column("raw_payload_id", sa.BigInteger(), sa.ForeignKey("raw_api_payloads.id")),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_weather_forecasts_dedup",
        "weather_forecasts",
        ["station_id", "variable", "issue_time", "valid_start"],
        unique=True,
    )
    op.create_index(
        "ix_weather_forecasts_lookup",
        "weather_forecasts",
        ["station_id", "variable", "valid_start"],
    )


def downgrade() -> None:
    op.drop_index("ix_weather_forecasts_lookup", table_name="weather_forecasts")
    op.drop_index("ix_weather_forecasts_dedup", table_name="weather_forecasts")
    op.drop_table("weather_forecasts")

    op.drop_index("ix_weather_observations_lookup", table_name="weather_observations")
    op.drop_index("ix_weather_observations_dedup", table_name="weather_observations")
    op.drop_table("weather_observations")

    op.drop_table("weather_stations")
