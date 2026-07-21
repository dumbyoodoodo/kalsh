"""Typed schemas for Kalshi API responses.

# CONFIRMED (docs/API_VERIFICATION.md): field names below were verified
# against live demo API responses. Kalshi's current wire format represents
# prices as decimal-dollar strings (`*_dollars`, e.g. "0.4500") and
# quantities as fixed-point strings (`*_fp`, e.g. "100.00") rather than the
# integer-cents/integer-count fields (`yes_bid`, `volume`, ...) this codebase
# was originally built against -- those plain integer fields are frequently
# absent from live responses entirely. `Market`, `OrderbookResponse`, and
# `Trade` below normalize the `*_dollars`/`*_fp` wire fields into the
# original integer-cents/integer-count fields (rounding to the nearest cent)
# so the rest of the codebase (order-book reconstruction, CLI, storage) is
# unaffected. Sub-cent ("deci_cent") prices, if ever encountered, round to
# the nearest cent -- a known, documented limitation, not silent data loss.
`extra="allow"` remains in place so any further unexpected/renamed fields do
not hard-fail ingestion of raw payloads -- they surface as extra attributes
for later inspection instead of exceptions.
"""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator


def _dollars_to_cents(value: str) -> int:
    """Convert a Kalshi decimal-dollar-string price (e.g. "0.4500") to integer cents."""
    return int((Decimal(value) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _fp_to_int(value: str) -> int:
    """Convert a Kalshi fixed-point quantity string (e.g. "2.00") to an int."""
    return int(Decimal(value).to_integral_value(rounding=ROUND_HALF_UP))


def _apply_dollar_field_map(data: dict[str, Any], field_map: dict[str, str]) -> None:
    """Fill `target` (int cents) from `source` (dollar string) when target is absent, in place."""
    for target, source in field_map.items():
        if data.get(target) is None and data.get(source) is not None:
            data[target] = _dollars_to_cents(data[source])


def _apply_fp_field_map(data: dict[str, Any], field_map: dict[str, str]) -> None:
    """Fill `target` (int) from `source` (fixed-point string) when target is absent, in place."""
    for target, source in field_map.items():
        if data.get(target) is None and data.get(source) is not None:
            data[target] = _fp_to_int(data[source])


class KalshiModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Series(KalshiModel):
    ticker: str
    category: str | None = None
    title: str | None = None
    frequency: str | None = None
    settlement_sources: list[dict[str, Any]] | None = None
    last_updated_ts: datetime | None = None


class SeriesListResponse(KalshiModel):
    series: list[Series] = []
    cursor: str | None = None


class Event(KalshiModel):
    event_ticker: str
    series_ticker: str | None = None
    category: str | None = None
    title: str | None = None
    sub_title: str | None = None
    status: str | None = None
    last_updated_ts: datetime | None = None


class EventListResponse(KalshiModel):
    events: list[Event] = []
    cursor: str | None = None


class Market(KalshiModel):
    ticker: str
    event_ticker: str | None = None
    market_type: str | None = None
    title: str | None = None
    subtitle: str | None = None
    status: str | None = None
    yes_bid: int | None = None
    yes_ask: int | None = None
    no_bid: int | None = None
    no_ask: int | None = None
    last_price: int | None = None
    volume: int | None = None
    open_interest: int | None = None
    close_time: datetime | None = None
    rules_primary: str | None = None
    rules_secondary: str | None = None
    updated_time: datetime | None = None
    # Settlement fields, verified live on finalized markets (E-A research,
    # docs/adr/0006-settlement-labels.md): `result` is the payout side,
    # `expiration_value` is the underlying value Kalshi settled against
    # (e.g. "90.00" degrees), `settlement_ts` is the exact determination
    # time, and floor/cap/strike_type are the structured strike definition.
    result: str | None = None
    expiration_value: str | None = None
    settlement_ts: datetime | None = None
    floor_strike: float | None = None
    cap_strike: float | None = None
    strike_type: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_wire_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        _apply_dollar_field_map(
            data,
            {
                "yes_bid": "yes_bid_dollars",
                "yes_ask": "yes_ask_dollars",
                "no_bid": "no_bid_dollars",
                "no_ask": "no_ask_dollars",
                "last_price": "last_price_dollars",
            },
        )
        _apply_fp_field_map(data, {"volume": "volume_fp", "open_interest": "open_interest_fp"})
        return data


class MarketListResponse(KalshiModel):
    markets: list[Market] = []
    cursor: str | None = None


class MarketResponse(KalshiModel):
    market: Market


class OrderbookLevels(KalshiModel):
    """Normalized resting bid levels: [price_cents, quantity] pairs. No asks
    are returned by the API on either the legacy or current wire format --
    see kalshi.orderbook for how asks are derived from the complementary bid.
    """

    yes: list[list[int]] = []
    no: list[list[int]] = []


class OrderbookResponse(KalshiModel):
    orderbook: OrderbookLevels

    @model_validator(mode="before")
    @classmethod
    def _normalize_envelope(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "orderbook" in data:
            return data
        fp = data.get("orderbook_fp")
        if fp is None:
            return data
        data = dict(data)
        data["orderbook"] = {
            "yes": [
                [_dollars_to_cents(price), _fp_to_int(qty)]
                for price, qty in (fp.get("yes_dollars") or [])
            ],
            "no": [
                [_dollars_to_cents(price), _fp_to_int(qty)]
                for price, qty in (fp.get("no_dollars") or [])
            ],
        }
        return data


class Trade(KalshiModel):
    trade_id: str
    ticker: str | None = None
    created_time: datetime | None = None
    yes_price: int | None = None
    no_price: int | None = None
    count: int | None = None
    taker_side: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_wire_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        _apply_dollar_field_map(
            data, {"yes_price": "yes_price_dollars", "no_price": "no_price_dollars"}
        )
        _apply_fp_field_map(data, {"count": "count_fp"})
        return data


class TradeListResponse(KalshiModel):
    trades: list[Trade] = []
    cursor: str | None = None


class CandlestickPriceBlock(KalshiModel):
    """One OHLC(+mean) sub-object as returned for `price`, `yes_bid`, or
    `yes_ask` within a candlestick (docs/research/investigations/
    INV-20260721-price-history-recovery.md). All fields are optional: a
    zero-volume period's `price` block carries only `previous_dollars` (the
    price carried forward from the prior period) -- confirmed live -- while
    `yes_bid`/`yes_ask` blocks always carry full OHLC (quotes persist even
    with no trades)."""

    open_cents: int | None = None
    high_cents: int | None = None
    low_cents: int | None = None
    close_cents: int | None = None
    mean_cents: int | None = None
    previous_cents: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_wire_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        _apply_dollar_field_map(
            data,
            {
                "open_cents": "open_dollars",
                "high_cents": "high_dollars",
                "low_cents": "low_dollars",
                "close_cents": "close_dollars",
                "mean_cents": "mean_dollars",
                "previous_cents": "previous_dollars",
            },
        )
        return data


class Candlestick(KalshiModel):
    """One OHLC candle for a market at a given resolution. `end_period_ts` is
    the only timestamp the wire format provides; the period's start is
    derived by the caller from the requested `period_interval`."""

    end_period_ts: int
    volume: int | None = None
    open_interest: int | None = None
    price: CandlestickPriceBlock = CandlestickPriceBlock()
    yes_bid: CandlestickPriceBlock = CandlestickPriceBlock()
    yes_ask: CandlestickPriceBlock = CandlestickPriceBlock()

    @model_validator(mode="before")
    @classmethod
    def _normalize_wire_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        _apply_fp_field_map(data, {"volume": "volume_fp", "open_interest": "open_interest_fp"})
        return data


class CandlestickListResponse(KalshiModel):
    """Envelope confirmed live: `{"candlesticks": [...], "ticker": "..."}` --
    no cursor; the endpoint has no pagination mechanism of its own (see the
    investigation report for the observed 5,000-candle-per-request cap that
    range-chunking must respect instead)."""

    candlesticks: list[Candlestick] = []
    ticker: str | None = None
