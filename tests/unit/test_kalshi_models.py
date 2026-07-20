import json
from pathlib import Path

from kalshi_weather.kalshi.models import MarketResponse, OrderbookResponse, SeriesListResponse

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kalshi"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_series_list_parses_and_filters_weather_by_category() -> None:
    parsed = SeriesListResponse.model_validate(_load("series_list.json"))
    assert len(parsed.series) == 2
    weather = [s for s in parsed.series if s.category == "Climate and Weather"]
    assert [s.ticker for s in weather] == ["KXHIGHNY"]


def test_market_parses_prices_and_rules() -> None:
    parsed = MarketResponse.model_validate(_load("market.json"))
    assert parsed.market.ticker == "KXHIGHNY-26JUL21-T85"
    assert parsed.market.yes_bid == 42
    assert parsed.market.yes_ask == 47
    assert "85F" in (parsed.market.rules_primary or "")


def test_orderbook_full_parses_levels() -> None:
    parsed = OrderbookResponse.model_validate(_load("orderbook_full.json"))
    assert parsed.orderbook.yes == [[42, 100], [41, 50], [38, 200]]
    assert parsed.orderbook.no == [[53, 80], [50, 20]]


def test_orderbook_empty_parses_to_empty_lists() -> None:
    parsed = OrderbookResponse.model_validate(_load("orderbook_empty.json"))
    assert parsed.orderbook.yes == []
    assert parsed.orderbook.no == []


def test_unknown_fields_do_not_fail_parsing() -> None:
    payload = _load("market.json")
    payload["market"]["some_future_field_not_yet_known"] = "value"
    parsed = MarketResponse.model_validate(payload)
    assert parsed.market.ticker == "KXHIGHNY-26JUL21-T85"
