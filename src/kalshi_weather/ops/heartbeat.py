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

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

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


# --- persisted state (read by `ops heartbeat-status` / status.sh) ----------

#: Outcomes where the *collector* was healthy at attempt time. PING_FAILED
#: means the collector was fine but the external ping didn't get through -- a
#: delivery problem, not a data problem -- so it counts as collector-healthy.
_COLLECTOR_HEALTHY_OUTCOMES = frozenset({PINGED_HEALTHY, PING_FAILED})
#: Outcomes that deliberately withheld the ping because the collector was NOT
#: healthy (this is the dead-man's-switch firing on purpose).
_COLLECTOR_UNHEALTHY_OUTCOMES = frozenset({WITHHELD_STALE, WITHHELD_NO_DATA, WITHHELD_DB_ERROR})


@dataclass(frozen=True)
class HeartbeatState:
    """The last-attempt bookkeeping `ops heartbeat` persists so a read-only
    status check can report recency without sending a request. ``last_success_at``
    tracks the last time a ping was actually emitted (not just attempted), so
    the staleness check reflects real delivery, not withheld attempts."""

    last_attempt_at: datetime | None
    last_outcome: str | None
    last_emitted: bool
    last_success_at: datetime | None
    newest_snapshot_at: datetime | None = None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def load_heartbeat_state(path: Path) -> HeartbeatState | None:
    """Read the persisted heartbeat state. Returns None if the file is absent
    (no attempt yet) or unparseable (treated as no usable state, never raises)
    -- a status check must degrade gracefully, not crash."""
    try:
        raw = path.read_text()
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return HeartbeatState(
        last_attempt_at=_parse_dt(data.get("last_attempt_at")),
        last_outcome=data.get("last_outcome"),
        last_emitted=bool(data.get("last_emitted", False)),
        last_success_at=_parse_dt(data.get("last_success_at")),
        newest_snapshot_at=_parse_dt(data.get("newest_snapshot_at")),
    )


def write_heartbeat_state(path: Path, result: HeartbeatResult, now: datetime) -> None:
    """Persist the outcome of one attempt, preserving the prior ``last_success_at``
    unless this attempt itself emitted a ping. Written atomically (tmp + replace)
    so a status check never reads a half-written file."""
    prior = load_heartbeat_state(path)
    last_success = now if result.emitted else (prior.last_success_at if prior else None)
    payload = {
        "last_attempt_at": now.isoformat(),
        "last_outcome": result.outcome,
        "last_emitted": result.emitted,
        "last_success_at": last_success.isoformat() if last_success else None,
        "newest_snapshot_at": (
            result.newest_snapshot_at.isoformat() if result.newest_snapshot_at else None
        ),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)


# --- read-only status classification (pure; unit-tested) -------------------

STATUS_NOT_CONFIGURED = "not_configured"
STATUS_AGENT_NOT_RUNNING = "agent_not_running"
STATUS_NO_ATTEMPT = "no_attempt"
STATUS_LAST_ATTEMPT_FAILED = "last_attempt_failed"
STATUS_HEALTHY = "healthy"


@dataclass(frozen=True)
class HeartbeatStatusReport:
    """The read-only verdict rendered by `ops heartbeat-status`. ``label`` is
    the coarse state (one of the five required cases); ``collector_healthy`` is
    the orthogonal question of whether the *collector* was healthy at the last
    attempt (None when there is no attempt to judge); ``stale`` warns that no
    ping has succeeded within period+grace."""

    label: str
    stale: bool
    collector_healthy: bool | None
    summary: str
    last_attempt_at: datetime | None = None
    last_outcome: str | None = None
    last_success_at: datetime | None = None


def _collector_healthy(outcome: str | None) -> bool | None:
    if outcome in _COLLECTOR_HEALTHY_OUTCOMES:
        return True
    if outcome in _COLLECTOR_UNHEALTHY_OUTCOMES:
        return False
    return None


def classify_heartbeat_status(
    *,
    url_configured: bool,
    agent_running: bool,
    state: HeartbeatState | None,
    now: datetime,
    period_seconds: float,
    grace_seconds: float,
) -> HeartbeatStatusReport:
    """Map the gathered facts to one of the five required states plus a
    staleness warning. Pure: all inputs are passed in (launchd/config facts are
    gathered by the caller), so the policy is unit-testable without a host."""
    if not url_configured:
        return HeartbeatStatusReport(
            STATUS_NOT_CONFIGURED,
            stale=False,
            collector_healthy=None,
            summary="HEARTBEAT_URL not set -- external uptime monitoring disabled",
        )
    if not agent_running:
        return HeartbeatStatusReport(
            STATUS_AGENT_NOT_RUNNING,
            stale=False,
            collector_healthy=None,
            summary="configured, but the heartbeat agent is not running -- no external pings sent",
        )
    if state is None or state.last_attempt_at is None:
        return HeartbeatStatusReport(
            STATUS_NO_ATTEMPT,
            stale=False,
            collector_healthy=None,
            summary="agent running, but no heartbeat attempt has been recorded yet",
        )

    window = timedelta(seconds=period_seconds + grace_seconds)
    if state.last_success_at is None:
        stale = True
    else:
        stale = to_naive_utc(now) - to_naive_utc(state.last_success_at) > window
    collector_healthy = _collector_healthy(state.last_outcome)

    if not state.last_emitted:
        summary = f"last attempt did not emit a ping (outcome={state.last_outcome})"
        label = STATUS_LAST_ATTEMPT_FAILED
    else:
        summary = "heartbeat healthy -- last attempt emitted a ping"
        label = STATUS_HEALTHY
    if stale:
        window_min = round(window.total_seconds() / 60, 1)
        summary += f" -- WARNING: no successful ping within {window_min} min (period+grace)"

    return HeartbeatStatusReport(
        label,
        stale=stale,
        collector_healthy=collector_healthy,
        summary=summary,
        last_attempt_at=state.last_attempt_at,
        last_outcome=state.last_outcome,
        last_success_at=state.last_success_at,
    )
