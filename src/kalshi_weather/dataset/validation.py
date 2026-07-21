"""Validation of a built research dataset.

Produces a structured report rather than raising: a research dataset with gaps
(missing observations, orphaned markets) is still useful and expected --
Milestone 2b isn't built, weather history has holes -- so the point is to make
those gaps *visible and counted*, not to abort. Genuine corruption (impossible
timestamps, a high below a low, duplicated join rows) is flagged as an error;
``ValidationReport.ok`` is true only when no error-severity finding fired.
"""

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import polars as pl

from kalshi_weather.dataset.builder import (
    SourceFrames,
    orphan_market_tickers,
    orphan_station_ids,
)
from kalshi_weather.dataset.market_map import MarketMapping

MIN_TEMPERATURE_F = -60.0
MAX_TEMPERATURE_F = 130.0


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    check: str
    severity: str  # "error" | "warning" | "info"
    count: int
    message: str
    samples: list[Any] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ValidationReport:
    findings: list[ValidationFinding]

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "error")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error_count": self.error_count,
            "findings": [asdict(f) for f in self.findings],
        }


def _samples(frame: pl.DataFrame, cols: list[str], limit: int = 5) -> list[Any]:
    if frame.height == 0:
        return []
    return frame.select(cols).head(limit).to_dicts()


def validate(
    sources: SourceFrames,
    frames: dict[str, pl.DataFrame],
    mappings: list[MarketMapping],
) -> ValidationReport:
    findings: list[ValidationFinding] = []

    panel = frames.get("weather_panel", pl.DataFrame())

    # 1. Missing weather observations: a target date that has forecasts but no
    #    settled observation to check them against.
    if panel.height and "settled_tmax_f" in panel.columns:
        missing_obs = panel.filter(
            pl.col("n_forecast_issuances").is_not_null()
            & pl.col("settled_tmax_f").is_null()
            & pl.col("settled_tmin_f").is_null()
        )
        findings.append(
            ValidationFinding(
                check="missing_weather_observations",
                severity="warning",
                count=missing_obs.height,
                message="(station, target_date) with forecasts but no settled observation",
                samples=_samples(missing_obs, ["station_id", "target_date"]),
            )
        )

    # 2. Missing forecasts: a target date with a settled observation but no
    #    forecast issuance.
    if panel.height and "n_forecast_issuances" in panel.columns:
        missing_fc = panel.filter(
            pl.col("settled_tmax_f").is_not_null()
            & pl.col("n_forecast_issuances").is_null()
        )
        findings.append(
            ValidationFinding(
                check="missing_forecasts",
                severity="warning",
                count=missing_fc.height,
                message="(station, target_date) with an observation but no forecast",
                samples=_samples(missing_fc, ["station_id", "target_date"]),
            )
        )

    # 3. Duplicate joins: each grain must be unique. A duplicate means the
    #    as-of/outer join fanned out incorrectly -- a real bug, so error.
    for name, keys in (
        ("weather_panel", ["station_id", "target_date"]),
        ("market_weather", ["market_ticker", "observed_at"]),
    ):
        frame = frames.get(name)
        if frame is None or frame.height == 0:
            continue
        dupes = frame.height - frame.select(keys).unique().height
        if dupes:
            findings.append(
                ValidationFinding(
                    check=f"duplicate_join_{name}",
                    severity="error",
                    count=dupes,
                    message=f"{name} has {dupes} rows sharing a {keys} grain",
                )
            )

    # 4. Impossible timestamps in the source data.
    now = datetime.now(UTC).replace(tzinfo=None)
    bad_fc = sources.forecasts.filter(pl.col("valid_start") > pl.col("valid_end"))
    bad_obs = sources.observations.filter(
        pl.col("issuance_time").dt.date() < pl.col("observation_date")
    )
    future_markets = sources.markets.filter(pl.col("observed_at") > pl.lit(now))
    impossible = bad_fc.height + bad_obs.height + future_markets.height
    if impossible:
        findings.append(
            ValidationFinding(
                check="impossible_timestamps",
                severity="error",
                count=impossible,
                message=(
                    "forecast valid_start>valid_end, observation issued before its "
                    "date, or market snapshot dated in the future"
                ),
                samples=_samples(bad_obs, ["station_id", "observation_date", "issuance_time"]),
            )
        )

    # 5. Settlement mismatches: a settled daily high below the settled daily
    #    low, or a settled value outside physically plausible bounds.
    if panel.height and "settled_tmax_f" in panel.columns:
        inverted = panel.filter(
            pl.col("settled_tmax_f").is_not_null()
            & pl.col("settled_tmin_f").is_not_null()
            & (pl.col("settled_tmax_f") < pl.col("settled_tmin_f"))
        )
        out_of_range = panel.filter(
            (pl.col("settled_tmax_f") > MAX_TEMPERATURE_F)
            | (pl.col("settled_tmax_f") < MIN_TEMPERATURE_F)
            | (pl.col("settled_tmin_f") > MAX_TEMPERATURE_F)
            | (pl.col("settled_tmin_f") < MIN_TEMPERATURE_F)
        )
        mismatch = inverted.height + out_of_range.height
        if mismatch:
            findings.append(
                ValidationFinding(
                    check="settlement_mismatches",
                    severity="error",
                    count=mismatch,
                    message="settled high<low, or a settled temperature out of range",
                    samples=_samples(
                        inverted, ["station_id", "target_date", "settled_tmax_f", "settled_tmin_f"]
                    ),
                )
            )

    # 6. Orphaned market records: markets with data but no settlement mapping.
    orphan_markets = orphan_market_tickers(sources, mappings)
    findings.append(
        ValidationFinding(
            check="orphaned_market_records",
            severity="warning",
            count=len(orphan_markets),
            message="market tickers present in data but absent from the settlement mapping",
            samples=orphan_markets[:5],
        )
    )

    # 7. Orphaned weather records: weather for stations not in the registry.
    orphan_stations = orphan_station_ids(sources)
    findings.append(
        ValidationFinding(
            check="orphaned_weather_records",
            severity="warning",
            count=len(orphan_stations),
            message="weather rows referencing a station absent from the registry",
            samples=orphan_stations[:5],
        )
    )

    return ValidationReport(findings=findings)
