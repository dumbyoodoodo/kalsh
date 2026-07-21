from datetime import date
from pathlib import Path

import pytest

from kalshi_weather.dataset.market_map import (
    MarketMapError,
    load_market_map,
    parse_market_map,
)


def test_parse_valid_mapping() -> None:
    raw = {
        "markets": [
            {
                "market_ticker": "KXHIGHNY-1",
                "station_id": "NYC",
                "variable": "tmax_f",
                "target_date": "2026-07-20",
            }
        ]
    }
    result = parse_market_map(raw)
    assert len(result) == 1
    assert result[0].market_ticker == "KXHIGHNY-1"
    assert result[0].variable == "tmax_f"
    assert result[0].target_date == date(2026, 7, 20)


def test_parse_none_and_empty() -> None:
    assert parse_market_map(None) == []
    assert parse_market_map({"markets": []}) == []


def test_parse_rejects_missing_keys() -> None:
    with pytest.raises(MarketMapError):
        parse_market_map({"markets": [{"market_ticker": "X"}]})


def test_parse_rejects_bad_variable() -> None:
    with pytest.raises(MarketMapError):
        parse_market_map(
            {
                "markets": [
                    {
                        "market_ticker": "X",
                        "station_id": "NYC",
                        "variable": "humidity",
                        "target_date": "2026-07-20",
                    }
                ]
            }
        )


def test_parse_rejects_duplicate_ticker() -> None:
    entry = {
        "market_ticker": "X",
        "station_id": "NYC",
        "variable": "tmax_f",
        "target_date": "2026-07-20",
    }
    with pytest.raises(MarketMapError):
        parse_market_map({"markets": [entry, entry]})


def test_parse_rejects_non_list_markets() -> None:
    with pytest.raises(MarketMapError):
        parse_market_map({"markets": "nope"})


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_market_map(None) == []
    assert load_market_map(tmp_path / "does_not_exist.yaml") == []


def test_load_reads_yaml_file(tmp_path: Path) -> None:
    path = tmp_path / "map.yaml"
    path.write_text(
        "markets:\n"
        "  - market_ticker: KXHIGHNY-1\n"
        "    station_id: NYC\n"
        "    variable: tmax_f\n"
        "    target_date: 2026-07-20\n",
        encoding="utf-8",
    )
    result = load_market_map(path)
    assert result[0].station_id == "NYC"
    assert result[0].target_date == date(2026, 7, 20)
