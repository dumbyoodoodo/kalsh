"""Canonical settlement-time labels (E-A, docs/adr/0006-settlement-labels.md).

Reconstructs, for every supported settled market, the value that was
*knowable at each stage of the settlement timeline* -- because they are not
the same value:

- ``value_at_close``      -- latest eligible CLI issuance <= market close
                             (close is ~00:59 ET, BEFORE the final morning
                             report: this is usually the same-day preliminary)
- ``value_at_settlement`` -- latest eligible issuance <= Kalshi's settlement
                             determination (``settlement_ts``, exposed by the
                             API on finalized markets; verified live). When
                             settlement_ts is absent, a conservative bound of
                             close_time + SETTLEMENT_WINDOW_HOURS is used and
                             the label is marked BOUNDED, never silently
                             substituted.
- ``latest_final_value``  -- the last issuance ever stored, which may include
                             corrections published AFTER Kalshi settled.
- ``kalshi_expiration_value`` / ``kalshi_result`` -- what Kalshi actually
                             paid on (directly exposed by the API).

Only RESOLVED or BOUNDED labels may be used downstream; every other status
carries a machine-readable reason. Future issuances can never leak into an
earlier stage's value by construction (strict as-of selection).
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.domain.time import to_naive_utc
from kalshi_weather.settlement.resolver import ParserSettlementResolver
from kalshi_weather.settlement.spec import SettlementStatus
from kalshi_weather.storage.models import MarketSnapshot, WeatherObservation

#: Bump whenever reconstruction logic changes meaning.
RECONSTRUCTION_VERSION = "1"

#: Conservative settlement-window bound when settlement_ts is unavailable:
#: Kalshi's expected expiration is the morning after close (observed live);
#: 48h comfortably brackets it without reaching into later correction cycles.
SETTLEMENT_WINDOW_HOURS = 48


class LabelStatus(StrEnum):
    RESOLVED = "resolved"  # exact settlement_ts available, values reconstructed
    BOUNDED = "bounded"  # settlement_ts missing; conservative window used
    AMBIGUOUS = "ambiguous"  # conflicting inputs (reason in notes)
    UNSUPPORTED = "unsupported"  # market has no resolved settlement spec
    MISSING_SOURCE_DATA = "missing_source_data"  # no eligible issuances stored


@dataclass(frozen=True, slots=True)
class SettlementLabel:
    market_ticker: str
    station_id: str | None
    variable: str | None
    target_date: date | None
    status: LabelStatus
    reconstruction_version: str
    close_time: datetime | None = None
    settlement_time: datetime | None = None  # exact when RESOLVED, bound when BOUNDED
    settlement_time_is_exact: bool = False
    value_at_close: Decimal | None = None
    issuance_at_close: datetime | None = None
    value_at_settlement: Decimal | None = None
    issuance_at_settlement: datetime | None = None
    latest_final_value: Decimal | None = None
    latest_final_issuance: datetime | None = None
    kalshi_result: str | None = None
    kalshi_expiration_value: Decimal | None = None
    implied_result_at_settlement: str | None = None
    payout_value_agrees: bool | None = None  # value_at_settlement == expiration_value
    payout_result_agrees: bool | None = None  # implied result == kalshi result
    notes: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.status in (LabelStatus.RESOLVED, LabelStatus.BOUNDED)


def implied_result(
    value: Decimal, *, strike_type: str | None, floor: Decimal | None, cap: Decimal | None
) -> str | None:
    """Kalshi structured-strike semantics -> yes/no. Verified empirically in
    EXP E-A against every finalized market's own (expiration_value, result)
    pair; an unknown strike_type returns None rather than guessing."""
    if strike_type == "between" and floor is not None and cap is not None:
        return "yes" if floor <= value <= cap else "no"
    if strike_type == "greater" and floor is not None:
        return "yes" if value > floor else "no"
    if strike_type == "less" and cap is not None:
        return "yes" if value < cap else "no"
    if strike_type == "greater_or_equal" and floor is not None:
        return "yes" if value >= floor else "no"
    if strike_type == "less_or_equal" and cap is not None:
        return "yes" if value <= cap else "no"
    return None


def _as_of(
    issuances: list[tuple[datetime, Decimal]], t: datetime
) -> tuple[datetime, Decimal] | None:
    """Latest issuance at or before t. `issuances` sorted ascending; strict
    as-of -- an issuance after t can never be selected."""
    selected = None
    for issued_at, value in issuances:
        if issued_at <= t:
            selected = (issued_at, value)
        else:
            break
    return selected


def reconstruct_label(
    *,
    market_ticker: str,
    station_id: str | None,
    variable: str | None,
    target_date: date | None,
    close_time: datetime | None,
    settlement_ts: datetime | None,
    kalshi_result: str | None,
    expiration_value: Decimal | None,
    floor_strike: Decimal | None,
    cap_strike: Decimal | None,
    strike_type: str | None,
    issuances: list[tuple[datetime, Decimal]],
) -> SettlementLabel:
    """Pure reconstruction for one market. `issuances` are the (naive-UTC
    issuance_time, value) pairs for this market's (station, variable,
    target_date), sorted ascending; the caller guarantees eligibility (the
    issuances cover exactly the target date)."""
    notes: list[str] = []

    def label(status: LabelStatus, **kwargs: object) -> SettlementLabel:
        return SettlementLabel(
            market_ticker=market_ticker,
            station_id=station_id,
            variable=variable,
            target_date=target_date,
            status=status,
            reconstruction_version=RECONSTRUCTION_VERSION,
            close_time=close_time,
            kalshi_result=kalshi_result,
            kalshi_expiration_value=expiration_value,
            notes=notes,
            **kwargs,  # type: ignore[arg-type]
        )

    if station_id is None or variable is None or target_date is None:
        notes.append("no resolved settlement spec for this market")
        return label(LabelStatus.UNSUPPORTED)
    if not issuances:
        notes.append("no eligible CLI issuances stored for the target date")
        return label(LabelStatus.MISSING_SOURCE_DATA)
    if close_time is None:
        notes.append("market has no close_time; cannot anchor the timeline")
        return label(LabelStatus.AMBIGUOUS)

    if settlement_ts is not None:
        settlement_bound = settlement_ts
        exact = True
    else:
        settlement_bound = close_time + timedelta(hours=SETTLEMENT_WINDOW_HOURS)
        exact = False
        notes.append(
            f"settlement_ts unavailable; bounded at close+{SETTLEMENT_WINDOW_HOURS}h"
        )

    at_close = _as_of(issuances, close_time)
    at_settlement = _as_of(issuances, settlement_bound)
    latest = issuances[-1]

    if at_settlement is None:
        notes.append("no issuance at or before the settlement bound")
        return label(
            LabelStatus.MISSING_SOURCE_DATA,
            settlement_time=settlement_bound,
            settlement_time_is_exact=exact,
            latest_final_value=latest[1],
            latest_final_issuance=latest[0],
        )

    implied = (
        implied_result(
            at_settlement[1], strike_type=strike_type, floor=floor_strike, cap=cap_strike
        )
        if strike_type is not None
        else None
    )
    if strike_type is not None and implied is None:
        notes.append(f"unknown strike_type {strike_type!r}; implied result unavailable")

    return label(
        LabelStatus.RESOLVED if exact else LabelStatus.BOUNDED,
        settlement_time=settlement_bound,
        settlement_time_is_exact=exact,
        value_at_close=at_close[1] if at_close else None,
        issuance_at_close=at_close[0] if at_close else None,
        value_at_settlement=at_settlement[1],
        issuance_at_settlement=at_settlement[0],
        latest_final_value=latest[1],
        latest_final_issuance=latest[0],
        implied_result_at_settlement=implied,
        payout_value_agrees=(
            at_settlement[1] == expiration_value if expiration_value is not None else None
        ),
        payout_result_agrees=(
            implied == kalshi_result
            if implied is not None and kalshi_result in ("yes", "no")
            else None
        ),
    )


async def build_labels(session: AsyncSession) -> list[SettlementLabel]:
    """Reconstruct a settlement label for every market in the store, using
    the settlement parser for station/variable/date and the issuance history
    for values. One label per market (latest snapshot's settlement fields)."""
    specs = {s.market_ticker: s for s in await ParserSettlementResolver().resolve_specs(session)}

    latest_ids = (
        select(func.max(MarketSnapshot.id))
        .group_by(MarketSnapshot.market_ticker)
        .scalar_subquery()
    )
    snapshots = (
        await session.scalars(select(MarketSnapshot).where(MarketSnapshot.id.in_(latest_ids)))
    ).all()

    # issuance history grouped by (station, variable, date), sorted ascending
    issuance_rows = (
        await session.scalars(select(WeatherObservation).order_by(WeatherObservation.issuance_time))
    ).all()
    by_key: dict[tuple[str, str, date], list[tuple[datetime, Decimal]]] = {}
    for row in issuance_rows:
        by_key.setdefault((row.station_id, row.variable, row.observation_date), []).append(
            (to_naive_utc(row.issuance_time), Decimal(row.value))
        )

    labels: list[SettlementLabel] = []
    for snap in snapshots:
        spec = specs.get(snap.market_ticker)
        resolved = spec is not None and spec.status is SettlementStatus.RESOLVED
        station_id = spec.station_id if resolved and spec is not None else None
        variable = spec.variable if resolved and spec is not None else None
        target = spec.target_date if resolved and spec is not None else None
        issuances = (
            by_key.get((station_id, variable, target), [])
            if station_id and variable and target
            else []
        )
        labels.append(
            reconstruct_label(
                market_ticker=snap.market_ticker,
                station_id=station_id,
                variable=variable,
                target_date=target,
                close_time=to_naive_utc(snap.close_time) if snap.close_time else None,
                settlement_ts=to_naive_utc(snap.settlement_ts) if snap.settlement_ts else None,
                kalshi_result=snap.result,
                expiration_value=Decimal(snap.expiration_value)
                if snap.expiration_value is not None
                else None,
                floor_strike=Decimal(snap.floor_strike)
                if snap.floor_strike is not None
                else None,
                cap_strike=Decimal(snap.cap_strike) if snap.cap_strike is not None else None,
                strike_type=snap.strike_type,
                issuances=issuances,
            )
        )
    return labels
