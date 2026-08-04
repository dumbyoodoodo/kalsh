"""station-level weather collection-attempt evidence ledger

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-04

Additive only: one new append-only table, ``weather_collection_attempts``,
recording per-station per-product weather collection evidence for FUTURE
collector cycles. Mirrors the ADR 0020 precedent set by ``market_poll_attempts``
on the Kalshi side.

Motivating defect (2026-08-04 SEA/PHX/MIA pilot review). ``WeatherCycleStats``
records only cycle-level scalars -- ``invalid_items`` and ``errors`` carry no
station dimension -- so a parser rejection or an unavailable source could not be
attributed to the station that caused it. The review therefore had to mark
``parser_failures`` and ``source_unavailable_attempts`` as
``evidence_unavailable``, which blocks KEEP by construction for every station.

Note precisely what was missing. Successes were ALREADY attributable:
``weather_observations`` rows carry ``station_id``. It is FAILURES AND
NON-EVENTS that left no trace -- a request that returned nothing, a source that
never answered, a body that failed to parse, a save that raised. Those produce
no row anywhere, so "no observation" and "never attempted" were
indistinguishable.

NO historical row is ever backfilled. The table is empty until the collector
next runs against a 0012 database; every pre-0012 window stays explicitly
LEGACY_UNKNOWN rather than being converted to zero or divided across stations.

Downgrade drops the table. That is safe ONLY because nothing has been deployed
or populated at authoring time. Once production attempt evidence exists,
downgrading would DESTROY irreplaceable operational evidence and requires
explicit operator approval -- treat it as a destructive migration from that
point on.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "weather_collection_attempts",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        # --- identity and lineage -------------------------------------------
        sa.Column("attempt_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "collector_run_id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            sa.ForeignKey("collector_runs.id"),
            nullable=False,
        ),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("station_code", sa.String(32), nullable=False),
        sa.Column("wfo", sa.String(8), nullable=True),
        sa.Column("product_type", sa.String(32), nullable=False),
        sa.Column("logical_request_key", sa.String(255), nullable=False),
        sa.Column("source_request_id", sa.String(255), nullable=True),
        sa.Column("source_product_id", sa.String(128), nullable=True),
        # --- timing ----------------------------------------------------------
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_station_local_date", sa.Date(), nullable=True),
        # --- request evidence -------------------------------------------------
        sa.Column("source_endpoint", sa.String(255), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        # --- stage / outcome --------------------------------------------------
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("source_availability", sa.String(24), nullable=False),
        # --- parser evidence --------------------------------------------------
        sa.Column("parser_name", sa.String(64), nullable=True),
        sa.Column("parser_version", sa.String(16), nullable=True),
        sa.Column("parser_error_type", sa.String(64), nullable=True),
        sa.Column("parser_error_message", sa.String(500), nullable=True),
        sa.Column(
            "raw_payload_id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            sa.ForeignKey("raw_api_payloads.id"),
            nullable=True,
        ),
        # --- persistence evidence ---------------------------------------------
        sa.Column("parsed_entity_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "persisted_entity_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "duplicate_entity_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("persistence_error_type", sa.String(64), nullable=True),
        sa.Column("persistence_error_message", sa.String(500), nullable=True),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
        # One TERMINAL row per logical attempt. Retries roll up into
        # retry_count and never produce a second row.
        sa.UniqueConstraint(
            "collector_run_id",
            "environment",
            "station_code",
            "product_type",
            "logical_request_key",
            name="uq_weather_attempt_logical",
        ),
    )
    op.create_index(
        "ix_weather_attempts_station_time",
        "weather_collection_attempts",
        ["station_code", "requested_at"],
    )
    op.create_index("ix_weather_attempts_run", "weather_collection_attempts", ["collector_run_id"])
    op.create_index(
        "ix_weather_attempts_product_outcome",
        "weather_collection_attempts",
        ["product_type", "outcome"],
    )
    op.create_index(
        "ix_weather_attempts_target_date",
        "weather_collection_attempts",
        ["target_station_local_date"],
    )
    op.create_index(
        "ix_weather_attempts_env_time",
        "weather_collection_attempts",
        ["environment", "requested_at"],
    )


def downgrade() -> None:
    """DESTRUCTIVE once production evidence exists -- see the module docstring."""
    for name in (
        "ix_weather_attempts_env_time",
        "ix_weather_attempts_target_date",
        "ix_weather_attempts_product_outcome",
        "ix_weather_attempts_run",
        "ix_weather_attempts_station_time",
    ):
        op.drop_index(name, table_name="weather_collection_attempts")
    op.drop_table("weather_collection_attempts")
