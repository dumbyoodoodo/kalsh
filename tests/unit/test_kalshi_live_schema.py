"""Regression tests against real (sanitized) Kalshi demo API responses.

Fixtures in tests/fixtures/kalshi/live/ were captured from the live demo API
on 2026-07-20 (see docs/API_VERIFICATION.md) and confirm the current wire
format: prices as decimal-dollar strings (`*_dollars`) and quantities as
fixed-point strings (`*_fp`), normalized by kalshi/models.py into the
integer-cents/integer-count fields the rest of the codebase expects.
"""

import json
from pathlib import Path

from kalshi_weather.kalshi.models import (
    MarketListResponse,
    MarketResponse,
    OrderbookResponse,
    SeriesListResponse,
    TradeListResponse,
)
from kalshi_weather.kalshi.orderbook import reconstruct_best_quote

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kalshi" / "live"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_live_series_list_parses() -> None:
    parsed = SeriesListResponse.model_validate(_load("series_list_live.json"))
    assert len(parsed.series) == 3
    assert all(s.ticker for s in parsed.series)


def test_live_markets_page_parses_with_real_cursor() -> None:
    payload = _load("markets_page_live.json")
    parsed = MarketListResponse.model_validate(payload)
    assert len(parsed.markets) == 3
    assert parsed.cursor == payload["cursor"]
    assert parsed.cursor  # a real, non-empty cursor from a genuinely paginated page


def test_live_active_market_normalizes_dollar_and_fp_fields() -> None:
    """Confirms *_dollars/*_fp wire fields populate the legacy int fields
    the rest of the codebase (CLI, storage) reads."""
    payload = _load("market_active_live.json")
    parsed = MarketResponse.model_validate(payload)
    market = parsed.market

    raw_market = payload["market"]
    assert "yes_bid" not in raw_market  # confirms the live payload has no plain int field
    assert "yes_bid_dollars" in raw_market  # confirms it's present as a dollar string

    assert market.yes_bid is not None
    assert market.yes_ask is not None
    assert market.volume is not None
    # spot check the actual conversion against the raw dollar string
    from decimal import Decimal

    expected_yes_bid_cents = int(Decimal(raw_market["yes_bid_dollars"]) * 100)
    assert market.yes_bid == expected_yes_bid_cents


def test_live_orderbook_empty_normalizes_via_orderbook_fp_envelope() -> None:
    payload = _load("orderbook_empty_live.json")
    assert "orderbook_fp" in payload
    assert "orderbook" not in payload  # confirms the live envelope key really differs

    parsed = OrderbookResponse.model_validate(payload)
    assert parsed.orderbook.yes == []
    assert parsed.orderbook.no == []

    quote = reconstruct_best_quote([], [])
    assert quote.best_yes_bid_cents is None
    assert quote.best_yes_ask_cents is None


def test_live_orderbook_one_sided_normalizes_and_reconstructs() -> None:
    payload = _load("orderbook_one_sided_live.json")
    parsed = OrderbookResponse.model_validate(payload)

    assert parsed.orderbook.yes == []
    assert parsed.orderbook.no == [[1, 2], [2, 2]]  # "0.0100"->1 cent, "0.0200"->2 cents

    yes_bids = [(lvl[0], lvl[1]) for lvl in parsed.orderbook.yes]
    no_bids = [(lvl[0], lvl[1]) for lvl in parsed.orderbook.no]
    quote = reconstruct_best_quote(yes_bids, no_bids)

    assert quote.best_yes_bid_cents is None
    assert quote.best_no_bid_cents == 2  # highest resting NO bid
    assert quote.best_yes_ask_cents == 98  # 100 - best_no_bid
    assert quote.best_no_ask_cents is None  # no YES bids to derive it from


def test_live_trade_normalizes_dollar_and_fp_fields() -> None:
    payload = _load("trades_live.json")
    raw_trade = payload["trades"][0]
    assert "yes_price" not in raw_trade
    assert "yes_price_dollars" in raw_trade

    parsed = TradeListResponse.model_validate(payload)
    trade = parsed.trades[0]
    assert trade.yes_price == 49  # "0.4900" -> 49 cents
    assert trade.no_price == 51  # "0.5100" -> 51 cents
    assert trade.count == 1  # "0.91" rounds to nearest whole contract (documented limitation)
