"""External uptime heartbeat (dead-man's-switch).

The observatory + Telegram alerting (``ops monitor``) run *on* the collector
machine, so they cannot fire when that machine is powered off -- exactly the
2026-07-26 power-loss failure mode, where the box was dead for ~14.5 h and
nothing off-device noticed.

``ops heartbeat`` closes that gap. It pings an EXTERNAL push-monitor URL
(healthchecks.io / Better Uptime / Cronitor / any "alert me when the pings
stop" service) on a schedule, but **only while the collector is demonstrably
healthy**. If the machine is off, the process is wedged, or the database is
unreachable, the ping is withheld and the external service raises the alert
after its own grace period. That single mechanism therefore covers all three
failure modes -- power loss (primary), software wedge, and DB outage -- from a
system that fails independently of this one.

The URL is a capability token (anyone holding it can suppress your outage
alert), so it is a ``SecretStr`` and is never logged; only the decision and
outcome are.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kalshi_weather.config import Settings
from kalshi_weather.domain.time import to_naive_utc, utc_now
from kalshi_weather.logging import get_logger
from kalshi_weather.storage.models import MarketSnapshot

logger = get_logger(__name__)

#: Outcomes, in the two dimensions that matter operationally: did we ping, and
#: why / why not. Kept as plain strings so they log and test cleanly.
SKIPPED_NOT_CONFIGURED = "skipped_not_configured"
PINGED_HEALTHY = "pinged_healthy"
WITHHELD_STALE = "withheld_stale"
WITHHELD_NO_DATA = "withheld_no_data"
WITHHELD_DB_ERROR = "withheld_db_error"
PING_FAILED = "ping_failed"


@dataclass(frozen=True)
class HeartbeatResult:
    """What one ``ops heartbeat`` invocation did. ``emitted`` is the single
    fact the external monitor ultimately reacts to (via the ping's presence or
    absence); ``outcome`` and ``detail`` explain it for local logs."""

    outcome: str
    emitted: bool
    detail: str
    newest_snapshot_at: datetime | None = None


def heartbeat_decision(
    newest_snapshot_at: datetime | None,
    now: datetime,
    max_staleness: timedelta,
) -> tuple[bool, str]:
    """Decide whether the collector is healthy enough to emit a heartbeat.

    Pure and side-effect-free so the policy is unit-testable without a DB or
    network. Emit iff there is a snapshot and it is newer than
    ``max_staleness``. A missing snapshot or a stale one withholds the ping,
    which is what makes this a dead-man's-switch rather than a liveness lie.
    """
    if newest_snapshot_at is None:
        return False, WITHHELD_NO_DATA
    # Normalize both sides to naive-UTC: `now` is aware (utc_now) but the
    # stored timestamp may be aware (Postgres timestamptz) or naive (SQLite).
    age = to_naive_utc(now) - to_naive_utc(newest_snapshot_at)
    if age > max_staleness:
        return False, WITHHELD_STALE
    return True, PINGED_HEALTHY


async def _newest_snapshot_at(session_factory: async_sessionmaker) -> datetime | None:  # type: ignore[type-arg]
    async with session_factory() as session:
        newest: datetime | None = await session.scalar(select(func.max(MarketSnapshot.observed_at)))
        return newest


async def run_heartbeat(
    settings: Settings,
    session_factory: async_sessionmaker,  # type: ignore[type-arg]
    *,
    now: datetime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> HeartbeatResult:
    """Run one heartbeat cycle. Never raises: any failure path withholds the
    ping (so the external monitor still alerts) and is returned as a result.

    ``now``/``transport`` are injectable for tests.
    """
    now = now or utc_now()

    if settings.heartbeat_url is None:
        logger.info("ops.heartbeat.skipped", reason=SKIPPED_NOT_CONFIGURED)
        return HeartbeatResult(
            SKIPPED_NOT_CONFIGURED, emitted=False, detail="HEARTBEAT_URL not set"
        )

    try:
        newest = await _newest_snapshot_at(session_factory)
    except Exception as exc:  # DB unreachable -> withhold, external monitor alerts
        logger.warning("ops.heartbeat.db_error", error=type(exc).__name__)
        return HeartbeatResult(WITHHELD_DB_ERROR, emitted=False, detail=str(exc)[:200])

    should_ping, reason = heartbeat_decision(
        newest, now, timedelta(seconds=settings.heartbeat_stale_after_seconds)
    )
    if not should_ping:
        logger.warning("ops.heartbeat.withheld", reason=reason, newest_snapshot_at=str(newest))
        return HeartbeatResult(
            reason, emitted=False, detail="collector not healthy", newest_snapshot_at=newest
        )

    # Unwrap the URL only here, at the point of use, and never log it.
    url = settings.heartbeat_url.get_secret_value()
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except Exception as exc:
        logger.warning("ops.heartbeat.ping_failed", error=type(exc).__name__)
        return HeartbeatResult(
            PING_FAILED, emitted=False, detail=str(exc)[:200], newest_snapshot_at=newest
        )

    logger.info("ops.heartbeat.pinged", newest_snapshot_at=str(newest))
    return HeartbeatResult(PINGED_HEALTHY, emitted=True, detail="ok", newest_snapshot_at=newest)
