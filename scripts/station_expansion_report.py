"""Station-expansion coverage report (Task 3B, 2026-07-21).

Produces a deterministic per-station report over the registry and the
local store: observation coverage/quality, forecast-collection status,
Task 3A monitor compatibility, Kalshi series mapping status, candle
coverage, and a mechanically-derived replication-readiness verdict for a
future H0002/H0011 replication (which needs only the observation
archive; forecast/candle status are secondary and can only add
limitations, never block).

Deterministic by construction: reads only local state (registry + DB),
sorts every collection, and derives `generated_at` from the data itself
(the maximum stored observation/forecast timestamp), so two runs against
an unchanged store produce byte-identical output. The authoritative
Kalshi settlement mappings below were captured live (read-only public
API + NWS /points + IEM probes, 2026-07-21) and are embedded statically
so report generation itself never touches the network.

Usage:
    PYTHONPATH=src python scripts/station_expansion_report.py \
        --out docs/research/investigations/2026-07-21-station-expansion-report.json \
        --out-md docs/research/investigations/2026-07-21-station-expansion-report.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sqlalchemy import select

from kalshi_weather.config import get_settings
from kalshi_weather.ops.forecast_cadence import CadenceConfig, run_forecast_cadence
from kalshi_weather.storage.database import create_engine, create_session_factory
from kalshi_weather.storage.models import MarketSnapshot, WeatherForecast, WeatherObservation
from kalshi_weather.weather.stations import list_stations

#: Authoritative Kalshi series -> station mappings, captured live 2026-07-21
#: (Task 3B Part 1): settlement-source URLs verbatim from GET /series/{t};
#: WFO cross-verified via NWS /points on the station coordinate; IEM CLI
#: history probed at 2023-01-02 and 2026-07-20 (both non-empty). Close-time
#: convention observed on live open markets: ~00:59-01:00 station-local of
#: the day after the target date, mirroring NYC.
VERIFIED_SERIES_MAPPINGS: dict[str, dict[str, Any]] = {
    "NYC": {
        "series_families": ["KXHIGHNY", "KXLOWTNYC"],
        "cli_issuedby": "NYC",
        "wfo_site": "OKX",
        "evidence": "docs/adr/0003-weather-data-source.md (pre-expansion baseline)",
        "active_markets_seen_live": True,
        "iem_history_probed": True,
    },
    "CHI": {
        "series_families": ["KXHIGHCHI", "KXLOWTCHI"],
        "cli_issuedby": "MDW",
        "wfo_site": "LOT",
        "evidence": (
            "GET /series/KXHIGHCHI + /series/KXLOWTCHI settlement URL "
            "site=LOT&issuedby=MDW (Midway, NOT O'Hare); NWS /points(41.7842,-87.7553) "
            "-> LOT; IEM CLIMDW products present 2023-01-02 and 2026-07-20"
        ),
        "active_markets_seen_live": True,
        "iem_history_probed": True,
    },
    "DEN": {
        "series_families": ["KXHIGHDEN", "KXLOWTDEN"],
        "cli_issuedby": "DEN",
        "wfo_site": "BOU",
        "evidence": (
            "GET /series/KXHIGHDEN + /series/KXLOWTDEN settlement URL "
            "site=BOU&issuedby=DEN; NWS /points(39.8466,-104.6562) -> BOU; "
            "IEM CLIDEN products present 2023-01-02 and 2026-07-20"
        ),
        "active_markets_seen_live": True,
        "iem_history_probed": True,
    },
    "LAX": {
        "series_families": ["KXHIGHLAX", "KXLOWTLAX"],
        "cli_issuedby": "LAX",
        "wfo_site": "LOX",
        "evidence": (
            "GET /series/KXHIGHLAX + /series/KXLOWTLAX settlement URL "
            "site=LOX&issuedby=LAX; NWS /points(33.9382,-118.3866) -> LOX; "
            "IEM CLILAX products present 2023-01-02 and 2026-07-20"
        ),
        "active_markets_seen_live": True,
        "iem_history_probed": True,
    },
}

#: Replication-readiness gates (mechanical). Primary gates concern the
#: observation archive H0002/H0011 replication actually needs; secondary
#: gates can only downgrade READY to READY WITH DOCUMENTED LIMITATIONS.
MIN_OBS_DAYS_PER_VARIABLE = 1200  # ~ NYC's 1,282-day replication baseline
MAX_MISSING_DATE_PCT = 2.0  # NYC's own archive has ~1.2% source gaps
VARIABLES = ["tmax_f", "tmin_f"]


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


async def _collect(session: Any) -> dict[str, Any]:
    obs_rows = (
        await session.execute(
            select(
                WeatherObservation.station_id,
                WeatherObservation.variable,
                WeatherObservation.observation_date,
                WeatherObservation.issuance_time,
                WeatherObservation.value,
                WeatherObservation.provider,
            )
        )
    ).all()
    fc_rows = (
        await session.execute(
            select(
                WeatherForecast.station_id,
                WeatherForecast.issue_time,
                WeatherForecast.observed_at,
            )
        )
    ).all()
    snapshot_rows = (await session.execute(select(MarketSnapshot.market_ticker).distinct())).all()
    cadence = await run_forecast_cadence(session, config=CadenceConfig())
    return {
        "obs": obs_rows,
        "forecasts": fc_rows,
        "market_tickers": sorted({r.market_ticker for r in snapshot_rows}),
        "cadence": cadence,
    }


def _ensure_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _station_report(
    station: Any, data: dict[str, Any], market_tickers: list[str]
) -> dict[str, Any]:
    sid = station.station_id
    obs = [r for r in data["obs"] if r.station_id == sid]
    forecasts = [r for r in data["forecasts"] if r.station_id == sid]
    mapping = VERIFIED_SERIES_MAPPINGS.get(sid, {})
    tz = ZoneInfo(station.timezone)

    per_variable: dict[str, Any] = {}
    boundary_anomalies = 0
    late_corrections = 0
    duplicate_keys = 0
    seen_keys: set[tuple[str, datetime]] = set()
    for row in obs:
        key = (row.variable, _ensure_utc(row.issuance_time))
        if key in seen_keys:
            duplicate_keys += 1
        seen_keys.add(key)
        local_date = _ensure_utc(row.issuance_time).astimezone(tz).date()
        # An issuance for observation_date D legitimately lands on D (same-day
        # preliminary), D+1 local (next-morning final), or LATER (a late CLI
        # correction -- real, documented NWS behavior per H0002/H0012; single
        # digits per ~1,290 days on every station including NYC's validated
        # archive). Only an issuance BEFORE its observation date is
        # impossible and gates readiness.
        if local_date < row.observation_date:
            boundary_anomalies += 1
        elif local_date > row.observation_date + timedelta(days=1):
            late_corrections += 1

    for variable in VARIABLES:
        var_rows = [r for r in obs if r.variable == variable]
        days = sorted({r.observation_date for r in var_rows})
        if days:
            span_days = (days[-1] - days[0]).days + 1
            missing = span_days - len(days)
            missing_pct = round(100.0 * missing / span_days, 2)
            issuances_per_day: dict[date, int] = {}
            for r in var_rows:
                issuances_per_day[r.observation_date] = (
                    issuances_per_day.get(r.observation_date, 0) + 1
                )
            single = sum(1 for n in issuances_per_day.values() if n == 1)
        else:
            span_days, missing, missing_pct, single = 0, 0, 0.0, 0
        per_variable[variable] = {
            "days_with_data": len(days),
            "coverage_start": days[0].isoformat() if days else None,
            "coverage_end": days[-1].isoformat() if days else None,
            "span_days": span_days,
            "missing_dates_in_span": missing,
            "missing_date_pct": missing_pct,
            "single_issuance_days": single,
            "rows": len(var_rows),
        }

    providers = sorted({r.provider for r in obs})
    station_cadence = next((s for s in data["cadence"].stations if s.station_id == sid), None)
    series_families = mapping.get("series_families", [])
    market_count = sum(
        1 for t in market_tickers if any(t.startswith(f + "-") for f in series_families)
    )

    # --- mechanical readiness gates -------------------------------------
    primary_checks = {
        "registry_entry_valid": True,  # validate_registry() ran at import
        "settlement_mapping_verified": bool(mapping),
        "observation_days_sufficient": all(
            per_variable[v]["days_with_data"] >= MIN_OBS_DAYS_PER_VARIABLE for v in VARIABLES
        ),
        "missing_dates_within_tolerance": all(
            per_variable[v]["days_with_data"] > 0
            and per_variable[v]["missing_date_pct"] <= MAX_MISSING_DATE_PCT
            for v in VARIABLES
        ),
        "no_duplicate_observations": duplicate_keys == 0,
        "no_day_boundary_anomalies": boundary_anomalies == 0,
    }
    secondary_checks = {
        "forecast_capture_started": len(forecasts) > 0,
        "forecast_multi_issuance_observed": len({r.issue_time for r in forecasts}) >= 2,
        "cadence_monitor_covers_station": station_cadence is not None
        and not station_cadence.timezone_assumed_utc,
        "market_snapshots_present": market_count > 0,
    }
    if all(primary_checks.values()):
        if all(secondary_checks.values()):
            readiness = "READY"
            reason = "all primary and secondary checks pass"
        else:
            readiness = "READY WITH DOCUMENTED LIMITATIONS"
            failed = sorted(k for k, v in secondary_checks.items() if not v)
            reason = f"primary checks pass; secondary limitations: {', '.join(failed)}"
    else:
        readiness = "NOT READY"
        failed = sorted(k for k, v in primary_checks.items() if not v)
        reason = f"primary check(s) failed: {', '.join(failed)}"

    return {
        "registry": {
            "station_id": sid,
            "source_location_code": station.source_location_code,
            "name": station.name,
            "latitude": str(station.latitude),
            "longitude": str(station.longitude),
            "timezone": station.timezone,
            "city": station.city,
            "wfo_site": station.wfo_site,
        },
        "settlement_mapping": mapping,
        "observations": {
            "providers": providers,
            "per_variable": per_variable,
            "duplicate_natural_keys": duplicate_keys,
            "day_boundary_anomalies": boundary_anomalies,
            "late_correction_issuances": late_corrections,
        },
        "forecasts": {
            "rows": len(forecasts),
            "distinct_issuances": len({r.issue_time for r in forecasts}),
            "note": (
                "single-run capture only; cadence completeness is NOT claimed "
                "from one run -- see ops forecast-cadence for ongoing monitoring"
            ),
        },
        "kalshi_markets": {
            "series_families": series_families,
            "market_snapshots_in_store": market_count,
            "candles_in_store": 0 if market_count == 0 else None,
        },
        "replication_readiness": {
            "status": readiness,
            "reason": reason,
            "primary_checks": primary_checks,
            "secondary_checks": secondary_checks,
        },
    }


async def build_report() -> dict[str, Any]:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            data = await _collect(session)
    finally:
        await engine.dispose()

    all_times = [_ensure_utc(r.issuance_time) for r in data["obs"]] + [
        _ensure_utc(r.observed_at) for r in data["forecasts"]
    ]
    generated_at = _iso(max(all_times)) if all_times else "no-data"

    stations = {
        s.station_id: _station_report(s, data, data["market_tickers"]) for s in list_stations()
    }
    return {
        "report": "station-expansion (Task 3B)",
        "generated_from_data_through": generated_at,
        "thresholds": {
            "min_obs_days_per_variable": MIN_OBS_DAYS_PER_VARIABLE,
            "max_missing_date_pct": MAX_MISSING_DATE_PCT,
        },
        "stations": stations,
        "unresolved_questions": [
            (
                "Prospective Kalshi market/candle collection for the new series "
                "requires the authenticated collector runner; the local .env "
                "private-key formatting currently fails PEM parsing "
                "(MalformedFraming), so live collector cycles are blocked in this "
                "environment until the operator repairs the key material. "
                "Discovery is category-wide and the settlement parser resolves "
                "all three new cities, so no code change is needed once the "
                "runner is restored."
            ),
            (
                "Forecast cadence for the new stations has exactly one captured "
                "issuance; completeness claims require the continuously "
                "scheduled weather collector plus ops forecast-cadence "
                "observation over multiple days."
            ),
        ],
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Station-Expansion Report (Task 3B)",
        "",
        f"Data through: {report['generated_from_data_through']}",
        "",
        (
            "| Station | Series | Obs days (tmax/tmin) | Missing % | Dup | "
            "Anomalies/late-corr | Forecast issuances | Readiness |"
        ),
        "|---|---|---|---|---|---|---|---|",
    ]
    for sid in sorted(report["stations"]):
        s = report["stations"][sid]
        pv = s["observations"]["per_variable"]
        lines.append(
            "| {sid} | {fams} | {tmax}/{tmin} | {miss} | {dup} | {bound} | {fc} | {ready} |".format(
                sid=sid,
                fams=" ".join(s["kalshi_markets"]["series_families"]),
                tmax=pv["tmax_f"]["days_with_data"],
                tmin=pv["tmin_f"]["days_with_data"],
                miss=pv["tmax_f"]["missing_date_pct"],
                dup=s["observations"]["duplicate_natural_keys"],
                bound="{}/{}".format(
                    s["observations"]["day_boundary_anomalies"],
                    s["observations"]["late_correction_issuances"],
                ),
                fc=s["forecasts"]["distinct_issuances"],
                ready=s["replication_readiness"]["status"],
            )
        )
    lines += ["", "## Readiness reasons", ""]
    for sid in sorted(report["stations"]):
        s = report["stations"][sid]
        lines.append(f"- **{sid}**: {s['replication_readiness']['reason']}")
    lines += ["", "## Unresolved questions", ""]
    for q in report["unresolved_questions"]:
        lines.append(f"- {q}")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(build_report())
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    args.out_md.write_text(render_markdown(result))
    print(f"wrote {args.out}")
    print(f"wrote {args.out_md}")
