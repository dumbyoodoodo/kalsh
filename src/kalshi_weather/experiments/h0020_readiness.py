"""H0020 counts-only readiness: the official execution gate for the frozen
forecast-revision-underreaction registration (EXP-FUTURE-H0020).

READ-ONLY and deterministic. It counts eligible rows, event-groups, labels,
stations, exclusions (with reason codes, by split), verifies frozen hashes
and ledger integrity, and applies the registered calendar including the
deterministic test-extension rule. It NEVER fits the model, generates a
probability, computes a Brier score or any loss, compares against the
benchmark, or inspects validation/test performance -- there is no code path
here that could (enforced by tests, including a banned-field-name check on
the report type).

Availability is ``observed_at`` (ingestion), exactly as the registration
freezes it; asking this module to use ``issue_time`` fails closed as
INVALID. All window/gate/revision logic is imported from the frozen spec
module ``experiments/h0020.py`` -- nothing is re-declared here.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.dataset.pit import local_date
from kalshi_weather.experiments.h0020 import (
    ABSOLUTE_TEST_END,
    HORIZON_HOURS,
    MAX_STATION_CONCENTRATION,
    MAX_TEST_EXTENSIONS,
    MIN_BOOTSTRAP_UNITS,
    MIN_NEG_LABELS,
    MIN_POS_LABELS,
    MIN_STATIONS,
    MIN_TEST_EVENT_GROUPS,
    ForecastRow,
    ReadinessCounts,
    failing_gates,
    registered_test_end,
    select_revision_pair,
    window_for_target_date,
)
from kalshi_weather.settlement.labels import SettlementLabel

STATIONS = ("CHI", "DEN", "LAX", "NYC")
FAMILY_PREFIXES = ("KXHIGHT", "KXLOWT")
AVAILABILITY_FIELD = "observed_at"

#: Frozen artifact paths whose hashes must match the ledger record.
FROZEN_ARTIFACTS = {
    "config.json": "docs/research/experiments/EXP-FUTURE-H0020/config.json",
    "registration.md": "docs/research/experiments/EXP-FUTURE-H0020/registration.md",
    "h0020.py": "src/kalshi_weather/experiments/h0020.py",
}
#: Registered prior test windows H0020 must not touch (ledger ground truth).
H0018_TEST = (date(2026, 7, 13), date(2026, 7, 25))
H0019_TEST = (date(2026, 8, 12), date(2026, 8, 25))

# Exclusion reason codes (deterministic; superset of the frozen config's).
REASON_FAMILY = "ineligible_market_family"
REASON_STATION = "unsupported_station"
REASON_SETTLEMENT = "unresolved_settlement_mapping"
REASON_RANGE = "range_or_between_contract"
REASON_GAP = "excluded_registered_gap"
REASON_OUT_OF_SCOPE = "outside_registered_windows"
REASON_NO_MARKET_EVIDENCE = "no_market_evidence_at_decision"
REASON_COLLECTION_GAP = "collection_gap_no_forecast_rows"

# States.
REGISTERED_NOT_READY = "REGISTERED_NOT_READY"
COLLECTING_TRAIN = "COLLECTING_TRAIN"
COLLECTING_VALIDATION = "COLLECTING_VALIDATION"
COLLECTING_TEST = "COLLECTING_TEST"
EXTENDED_TEST_COLLECTING = "EXTENDED_TEST_COLLECTING"
READY_FOR_FINAL_TEST = "READY_FOR_FINAL_TEST"
DEFERRED_INSUFFICIENT_DATA = "DEFERRED_INSUFFICIENT_DATA"
INVALID = "INVALID"


@dataclass(frozen=True)
class EligibleMarket:
    """One market's counts-relevant facts (no probabilities, no prices used
    beyond evidence presence)."""

    market_ticker: str
    station: str
    variable: str  # tmax_f | tmin_f
    target_date: date
    result_yes: bool
    revision_is_zero: bool


@dataclass
class CoverageReport:
    """Counts, dates, exclusions, and integrity ONLY -- deliberately no field
    that could carry a score, loss, prediction, coefficient, or P&L."""

    state: str
    as_of: date
    effective_test_end: date
    extensions_active: int
    gates: dict[str, bool] = field(default_factory=dict)
    split_counts: dict[str, dict[str, Any]] = field(default_factory=dict)
    exclusions_by_split: dict[str, dict[str, int]] = field(default_factory=dict)
    integrity: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)


# --- frozen-hash / ledger integrity ------------------------------------------


def verify_frozen_integrity(repo_root: Path) -> tuple[bool, dict[str, Any]]:
    """Recompute the three frozen hashes and compare against the ledger's
    latest H0020 record; verify the ledger chain and that H0020's registered
    test window overlaps no prior test window."""
    from kalshi_weather.research.ledger import LedgerError, read_ledger

    report: dict[str, Any] = {}
    try:
        records = read_ledger(repo_root / "docs" / "research" / "ledger.jsonl")
    except LedgerError as exc:
        return False, {"ledger_chain": f"BROKEN: {exc}"}
    report["ledger_chain"] = f"verified ({len(records)} records)"
    h20 = next((r for r in reversed(records) if r.hypothesis_id == "H0020"), None)
    if h20 is None:
        return False, {**report, "ledger_record": "H0020 record missing"}
    pinned = dict(h20.artifact_hashes)
    ok = True
    for name, rel in FROZEN_ARTIFACTS.items():
        actual = hashlib.sha256((repo_root / rel).read_bytes()).hexdigest()
        match = pinned.get(name) == actual
        report[f"hash_{name}"] = "match" if match else "MISMATCH"
        ok &= match
    # registered window overlap (ledger + fixed prior windows)
    tw = h20.test_window
    report["test_window"] = tw
    if tw is None:
        return False, {**report, "test_window": "missing"}
    t_start, t_end = date.fromisoformat(tw[0]), date.fromisoformat(tw[1])
    for name, (o_start, o_end) in (("H0018", H0018_TEST), ("H0019", H0019_TEST)):
        overlap = t_start <= o_end and o_start <= t_end
        report[f"overlap_{name}"] = overlap
        ok &= not overlap
    return ok, report


# --- pure coverage builder ---------------------------------------------------


def build_coverage(
    labels: list[SettlementLabel],
    strikes: dict[str, tuple[float | None, float | None, str | None]],
    candle_evidence: dict[str, bool],
    forecast_series: dict[tuple[str, str, date], list[ForecastRow]],
    *,
    now: date,
    availability_field: str = AVAILABILITY_FIELD,
) -> tuple[list[EligibleMarket], dict[str, Counter[str]]]:
    """Classify every settled label into an eligible row or a reason-coded
    exclusion, per split. Pure; no I/O; counts only.

    ``availability_field`` is frozen to ``observed_at``; any other value is a
    registration violation the caller must surface as INVALID.
    """
    if availability_field != AVAILABILITY_FIELD:
        raise ValueError("issue_time_availability_violation")

    eligible: list[EligibleMarket] = []
    exclusions: dict[str, Counter[str]] = defaultdict(Counter)

    def bucket(target: date | None) -> str:
        if target is None:
            return "unknown"
        w = window_for_target_date(target)
        return {"train": "train", "validation": "validation", "test": "test"}.get(w, w)

    for lb in labels:
        split = bucket(lb.target_date)
        ticker = lb.market_ticker
        if not any(ticker.startswith(p) for p in FAMILY_PREFIXES):
            exclusions[split][REASON_FAMILY] += 1
            continue
        if lb.station_id not in STATIONS:
            exclusions[split][REASON_STATION] += 1
            continue
        if (
            lb.kalshi_result not in ("yes", "no")
            or lb.close_time is None
            or lb.target_date is None
            or lb.variable not in ("tmax_f", "tmin_f")
        ):
            exclusions[split][REASON_SETTLEMENT] += 1
            continue
        floor_s, cap_s, _stype = strikes.get(ticker, (None, None, None))
        if floor_s is not None and cap_s is not None:
            exclusions[split][REASON_RANGE] += 1
            continue
        if floor_s is None and cap_s is None:
            exclusions[split][REASON_SETTLEMENT] += 1
            continue
        window = window_for_target_date(lb.target_date)
        if window == "excluded_gap":
            exclusions["excluded_gap"][REASON_GAP] += 1
            continue
        if window == "out_of_scope":
            exclusions["out_of_scope"][REASON_OUT_OF_SCOPE] += 1
            continue
        split = {"train": "train", "validation": "validation", "test": "test"}[window]
        if not candle_evidence.get(ticker, False):
            exclusions[split][REASON_NO_MARKET_EVIDENCE] += 1
            continue
        decision = lb.close_time - timedelta(hours=HORIZON_HOURS)
        if decision.tzinfo is not None:  # normalize to naive UTC (storage convention)
            decision = decision.astimezone(UTC).replace(tzinfo=None)
        series = forecast_series.get((lb.station_id, lb.variable, lb.target_date), [])
        if not series:
            # distinguishable collection gap: zero ingested rows for the whole
            # (station, kind, target-date) series, vs rows present but no
            # eligible pair (reported under the frozen revision reason codes)
            exclusions[split][REASON_COLLECTION_GAP] += 1
            continue
        rev = select_revision_pair(series, decision)
        if rev.exclusion_reason is not None:
            exclusions[split][rev.exclusion_reason] += 1
            continue
        assert rev.revision is not None
        eligible.append(
            EligibleMarket(
                market_ticker=ticker,
                station=lb.station_id,
                variable=lb.variable,
                target_date=lb.target_date,
                result_yes=lb.kalshi_result == "yes",
                revision_is_zero=rev.revision == 0.0,
            )
        )
    return eligible, exclusions


def _split_stats(
    rows: list[EligibleMarket], *, test_end: date = ABSOLUTE_TEST_END
) -> dict[str, dict[str, Any]]:
    """Counts per split. ``test_end`` caps the test slice at the stage under
    evaluation: rows dated after a stage's registered end do not count toward
    that stage's readiness (the window is extended only by the frozen rule)."""
    out: dict[str, dict[str, Any]] = {}
    by_split: dict[str, list[EligibleMarket]] = defaultdict(list)
    for r in rows:
        w = window_for_target_date(r.target_date)
        name = {"train": "train", "validation": "validation", "test": "test"}.get(w)
        assert name is not None, f"eligible row outside splits: {r.target_date}"
        if name == "test" and r.target_date > test_end:
            continue
        by_split[name].append(r)
    for split in ("train", "validation", "test"):
        rs = by_split.get(split, [])
        groups = {(r.station, r.variable, r.target_date) for r in rs}
        station_groups: Counter[str] = Counter(g[0] for g in groups)
        total = len(groups)
        out[split] = {
            "rows": len(rs),
            "event_groups": total,
            "stations": len(station_groups),
            "pos_labels": sum(1 for r in rs if r.result_yes),
            "neg_labels": sum(1 for r in rs if not r.result_yes),
            "zero_revision_rows": sum(1 for r in rs if r.revision_is_zero),
            "station_concentration": (
                max(station_groups.values()) / total if total else 0.0
            ),
            "station_groups": dict(station_groups),
        }
    return out


def _group_leakage(rows: list[EligibleMarket]) -> bool:
    """True if any (station, variable, target_date) group appears in more than
    one split -- impossible by construction (split is a function of
    target_date), re-checked anyway per the registration."""
    seen: dict[tuple[str, str, date], str] = {}
    for r in rows:
        w = window_for_target_date(r.target_date)
        g = (r.station, r.variable, r.target_date)
        if g in seen and seen[g] != w:
            return True
        seen[g] = w
    return False


def effective_test_window(now: date) -> tuple[date, int, bool]:
    """(effective end, active extensions, all_windows_elapsed). The extension
    activates only because a registered end has elapsed with gates unmet --
    the caller checks gates at each stage; this resolves the calendar."""
    for n in range(MAX_TEST_EXTENSIONS + 1):
        end = registered_test_end(n)
        if now <= end:
            return end, n, False
    return ABSOLUTE_TEST_END, MAX_TEST_EXTENSIONS, True


def compute_report(
    labels: list[SettlementLabel],
    strikes: dict[str, tuple[float | None, float | None, str | None]],
    candle_evidence: dict[str, bool],
    forecast_series: dict[tuple[str, str, date], list[ForecastRow]],
    *,
    now: date,
    integrity_ok: bool,
    integrity_detail: dict[str, Any],
    availability_field: str = AVAILABILITY_FIELD,
) -> CoverageReport:
    """Assemble the full counts-only readiness report (pure; injected clock)."""
    from kalshi_weather.experiments.h0020 import (
        TEST_START,
        TRAIN_START,
        VAL_START,
    )

    cal_end, cal_extensions, all_elapsed = effective_test_window(now)
    try:
        eligible, exclusions = build_coverage(
            labels,
            strikes,
            candle_evidence,
            forecast_series,
            now=now,
            availability_field=availability_field,
        )
    except ValueError as exc:
        return CoverageReport(
            state=INVALID,
            as_of=now,
            effective_test_end=cal_end,
            extensions_active=cal_extensions,
            integrity={**integrity_detail, "violation": str(exc)},
        )

    leakage = _group_leakage(eligible)

    def evaluate(end: date) -> tuple[dict[str, dict[str, Any]], list[str], dict[str, bool]]:
        stats = _split_stats(eligible, test_end=end)
        te = stats["test"]
        counts = ReadinessCounts(
            test_window_elapsed=now > end,
            test_event_groups=te["event_groups"],
            stations_in_test=te["stations"],
            pos_labels_test=te["pos_labels"],
            neg_labels_test=te["neg_labels"],
            station_concentration=te["station_concentration"],
            bootstrap_units=te["event_groups"],
            hashes_valid=integrity_ok,
            no_group_leakage=not leakage,
            availability_uses_observed_at=availability_field == AVAILABILITY_FIELD,
            exclusions_reported_by_split=True,
        )
        failures = failing_gates(counts)
        gates = {
            "test_window_elapsed": counts.test_window_elapsed,
            f"test_event_groups>={MIN_TEST_EVENT_GROUPS}": te["event_groups"]
            >= MIN_TEST_EVENT_GROUPS,
            f"stations>={MIN_STATIONS}": te["stations"] >= MIN_STATIONS,
            f"pos_labels>={MIN_POS_LABELS}": te["pos_labels"] >= MIN_POS_LABELS,
            f"neg_labels>={MIN_NEG_LABELS}": te["neg_labels"] >= MIN_NEG_LABELS,
            f"station_concentration<={MAX_STATION_CONCENTRATION}": te["station_concentration"]
            <= MAX_STATION_CONCENTRATION,
            f"bootstrap_units>={MIN_BOOTSTRAP_UNITS}": te["event_groups"] >= MIN_BOOTSTRAP_UNITS,
            "frozen_hashes_valid": integrity_ok,
            "no_group_leakage": not leakage,
            "availability_uses_observed_at": counts.availability_uses_observed_at,
            "exclusions_reported_by_split": True,
            "provenance_recorded": bool(integrity_detail),
        }
        return stats, failures, gates

    # The extension rule, exactly: readiness is checked at each ELAPSED
    # registered end in order; the first passing stage is READY (no further
    # extension). A stage failing activates the next 7-day extension, at most
    # twice; all stages elapsed and failing => DEFERRED.
    ready_stage: int | None = None
    for n in range(MAX_TEST_EXTENSIONS + 1):
        stage_end = registered_test_end(n)
        if now > stage_end:
            _stats, stage_failures, _gates = evaluate(stage_end)
            if not stage_failures and integrity_ok and not leakage:
                ready_stage = n
                break
        else:
            break

    if ready_stage is not None:
        end, extensions = registered_test_end(ready_stage), ready_stage
    else:
        end, extensions = cal_end, cal_extensions
    stats, failures, gates = evaluate(end)

    if not integrity_ok or leakage:
        state = INVALID
    elif ready_stage is not None:
        state = READY_FOR_FINAL_TEST
    elif all_elapsed:
        state = DEFERRED_INSUFFICIENT_DATA
    elif now < TRAIN_START:
        state = REGISTERED_NOT_READY
    elif now < VAL_START:
        state = COLLECTING_TRAIN
    elif now < TEST_START:
        state = COLLECTING_VALIDATION
    elif extensions == 0:
        state = COLLECTING_TEST
    else:
        state = EXTENDED_TEST_COLLECTING

    return CoverageReport(
        state=state,
        as_of=now,
        effective_test_end=end,
        extensions_active=extensions,
        gates=gates,
        split_counts=stats,
        exclusions_by_split={k: dict(v) for k, v in exclusions.items()},
        integrity=integrity_detail,
        detail={
            "days_until_test_end": (end - now).days,
            "window_kind": "initial" if extensions == 0 else f"extension_{extensions}",
            "failing_gates": failures,
            "availability_field": availability_field,
            "grouping_unit": "(station, variable, target_date)",
        },
    )


# --- production loader (read-only) -------------------------------------------


async def load_inputs(
    session: AsyncSession,
) -> tuple[
    list[SettlementLabel],
    dict[str, tuple[float | None, float | None, str | None]],
    dict[str, bool],
    dict[tuple[str, str, date], list[ForecastRow]],
]:
    """Read-only production inputs for the coverage builder. SELECTs only."""
    from kalshi_weather.settlement.labels import build_labels
    from kalshi_weather.storage.models import WeatherForecast, WeatherStation

    labels = await build_labels(session)

    # strikes: one row per candidate ticker (identity fields, any snapshot)
    strikes: dict[str, tuple[float | None, float | None, str | None]] = {}
    rows = (
        await session.execute(
            text(
                "select market_ticker, max(floor_strike) fs, max(cap_strike) cs, "
                "max(strike_type) st from market_snapshots "
                "where market_ticker like 'KXHIGHT%' or market_ticker like 'KXLOWT%' "
                "group by market_ticker"
            )
        )
    ).all()
    for r in rows:
        strikes[r[0]] = (
            float(r[1]) if r[1] is not None else None,
            float(r[2]) if r[2] is not None else None,
            r[3],
        )

    # candle evidence at decision (close - 24h), per eligible-family label
    candle_evidence: dict[str, bool] = {}
    for lb in labels:
        t = lb.market_ticker
        if not any(t.startswith(p) for p in FAMILY_PREFIXES) or lb.close_time is None:
            continue
        decision = lb.close_time - timedelta(hours=HORIZON_HOURS)
        hit = (
            await session.execute(
                text(
                    "select 1 from market_candlesticks where market_ticker = :t "
                    "and period_end <= :d limit 1"
                ),
                {"t": t, "d": decision.replace(tzinfo=None) if decision.tzinfo else decision},
            )
        ).first()
        candle_evidence[t] = hit is not None

    # forecast series with observed_at (availability), per (station, kind, day)
    tz_by_station = {
        s.station_id: s.timezone
        for s in (await session.scalars(select(WeatherStation))).all()
    }
    raw: dict[tuple[str, date], dict[datetime, list[Any]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for f in (await session.scalars(select(WeatherForecast))).all():
        tz = tz_by_station.get(f.station_id)
        if tz is None or f.station_id not in STATIONS:
            continue
        # stored timestamps are naive UTC (CLAUDE.md convention); local_date
        # needs an aware instant to derive the station-local calendar day
        aware_vs = f.valid_start if f.valid_start.tzinfo else f.valid_start.replace(tzinfo=UTC)
        target = local_date(aware_vs, tz)
        raw[(f.station_id, target)][f.issue_time].append(f)

    def _naive_utc(dt: datetime) -> datetime:
        # storage mixes conventions (issue_time naive-UTC, observed_at aware);
        # normalize everything to naive UTC so comparisons are well-defined
        return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt

    series: dict[tuple[str, str, date], list[ForecastRow]] = defaultdict(list)
    for (station, target), by_issue in raw.items():
        for issue_time, periods in by_issue.items():
            # one logical issuance = all its stored periods; availability is
            # the LAST period's ingestion (conservative), tie id is max(id)
            observed = _naive_utc(max(_naive_utc(p.observed_at) for p in periods))
            issue_time = _naive_utc(issue_time)
            rid = max(p.id for p in periods)
            hi = max(float(p.point_estimate) for p in periods)
            lo = min(float(p.point_estimate) for p in periods)
            series[(station, "tmax_f", target)].append(
                ForecastRow(
                    row_id=rid, issue_time=issue_time, observed_at=observed, point_estimate=hi
                )
            )
            series[(station, "tmin_f", target)].append(
                ForecastRow(
                    row_id=rid, issue_time=issue_time, observed_at=observed, point_estimate=lo
                )
            )
    return labels, strikes, candle_evidence, dict(series)
