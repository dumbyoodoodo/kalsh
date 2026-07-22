"""Unexpected-change detection: stations appearing in collected data but
absent from the in-code registry, plus adapters that fold two existing
reports' relevant findings into this observatory's unified `Finding` type
(schema drift from `ops/quality.py`; issuance-schedule anomalies from
`ops/forecast_cadence.py`) rather than reimplementing either.

This module computes no research statistic. It only detects and adapts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from kalshi_weather.observatory.severity import Finding, Severity
from kalshi_weather.storage.models import WeatherForecast, WeatherObservation
from kalshi_weather.weather.stations import list_stations

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from kalshi_weather.ops.forecast_cadence import CadenceAlert
    from kalshi_weather.ops.quality import QualityFinding

#: Severity policy for forecast-cadence alerts, disclosed here since
#: `CadenceAlert` itself carries no severity: an outage or stall risks a
#: permanent, unrecoverable capture gap (ADR 0003 -- forecasts have no
#: backfill source), so those two kinds are CRITICAL; the others are
#: anomalies worth an operator's attention but not yet data loss.
_CADENCE_ALERT_SEVERITY: dict[str, Severity] = {
    "missing_issuance": Severity.WARNING,
    "duplicate_issuance": Severity.WARNING,
    "abnormal_cadence": Severity.WARNING,
    "issuance_gap": Severity.WARNING,
    "collector_outage": Severity.CRITICAL,
    "stalled_updates": Severity.CRITICAL,
}

#: Severity policy for `ops/quality.py`'s own three-level scheme.
_QUALITY_SEVERITY: dict[str, Severity] = {
    "error": Severity.CRITICAL,
    "warning": Severity.WARNING,
    "info": Severity.INFO,
}


def find_orphan_stations(known_station_ids: set[str], data_station_ids: set[str]) -> set[str]:
    """Station ids present in collected data but absent from the current
    registry -- e.g. a station renamed or removed from the registry while
    its historical data remains, or a data-entry bug bypassing the
    registry lookup entirely."""
    return data_station_ids - known_station_ids


def adapt_quality_finding(qf: QualityFinding, *, domain: str) -> Finding:
    """Fold one `ops/quality.py` `QualityFinding` into this module's
    `Finding` type. `domain` is supplied by the caller (the source report
    doesn't carry one) -- see `report.py`'s per-check domain assignment."""
    return Finding(
        domain=domain,
        check=qf.check,
        severity=_QUALITY_SEVERITY[qf.severity],
        count=qf.count,
        message=qf.message,
        samples=tuple(str(s) for s in qf.samples),
    )


def adapt_cadence_alert(alert: CadenceAlert) -> Finding:
    """Fold one `ops/forecast_cadence.py` `CadenceAlert` into a `Finding`."""
    severity = _CADENCE_ALERT_SEVERITY.get(alert.kind, Severity.WARNING)
    return Finding(
        domain="forecast",
        check=f"cadence_{alert.kind}",
        severity=severity,
        count=1,
        message=f"{alert.station_id}: expected {alert.expected}, observed {alert.observed}",
        samples=(alert.recommended_action,),
    )


def orphan_station_finding(orphans: set[str], *, domain: str, source: str) -> Finding:
    ids = sorted(orphans)
    return Finding(
        domain=domain,
        check=f"unexpected_stations_{source}",
        severity=Severity.WARNING if ids else Severity.INFO,
        count=len(ids),
        message=(
            f"{len(ids)} station id(s) present in {source} data but absent from "
            "the current registry"
        ),
        samples=tuple(ids[:10]),
    )


# --- Async loaders ------------------------------------------------------------


async def load_observation_station_ids(session: AsyncSession) -> set[str]:
    rows = await session.scalars(select(WeatherObservation.station_id).distinct())
    return set(rows.all())


async def load_forecast_station_ids(session: AsyncSession) -> set[str]:
    rows = await session.scalars(select(WeatherForecast.station_id).distinct())
    return set(rows.all())


def registry_station_ids() -> set[str]:
    return {s.station_id for s in list_stations()}
