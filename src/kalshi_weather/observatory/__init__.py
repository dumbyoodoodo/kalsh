"""Data quality observatory: continuous, deterministic verification that
incoming data across all five collection streams (forecasts, observations,
markets, trades, candles) remain scientifically usable.

No hypothesis is executed here and no statistical inference is performed.
Every finding is classified INFO / WARNING / CRITICAL (`severity.py`) and
detection only -- no automatic fixes.

See `docs/runbooks/data_quality_observatory.md` for the full rule
reference, and `report.build_observatory_report` for the entry point.
"""

from kalshi_weather.observatory.report import (
    ObservatoryConfig,
    ObservatoryReport,
    build_observatory_report,
)
from kalshi_weather.observatory.severity import Finding, Severity

__all__ = [
    "Finding",
    "ObservatoryConfig",
    "ObservatoryReport",
    "Severity",
    "build_observatory_report",
]
