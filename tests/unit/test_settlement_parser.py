"""Settlement parser tests, grounded in real Kalshi production payloads
(tests/fixtures/settlement/live_samples.json, fetched read-only during
Milestone 2b research -- see docs/adr/0005-settlement-resolution.md)."""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from kalshi_weather.settlement.parser import MarketInfo, SeriesInfo, parse_settlement
from kalshi_weather.settlement.spec import Confidence, SettlementStatus

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "settlement"
_SAMPLES = json.loads((FIXTURES / "live_samples.json").read_text())


def _series(ticker: str, **overrides: Any) -> SeriesInfo:
    raw = dict(_SAMPLES["series"][ticker])
    raw.update(overrides)
    return SeriesInfo(
        ticker=raw["ticker"],
        title=raw["title"],
        frequency=raw["frequency"],
        settlement_sources=raw["settlement_sources"],
    )


def _market(ticker: str, **overrides: Any) -> MarketInfo:
    raw = dict(_SAMPLES["markets"][ticker])
    raw.update(overrides)
    close = raw.get("close_time")
    return MarketInfo(
        ticker=raw["ticker"],
        event_ticker=raw.get("event_ticker"),
        title=raw.get("title"),
        rules_primary=raw.get("rules_primary"),
        close_time=datetime.fromisoformat(close) if isinstance(close, str) else close,
    )


# --- real resolved markets ---------------------------------------------------


def test_real_nyc_high_market_resolves_high_confidence() -> None:
    spec = parse_settlement(_series("KXHIGHNY"), _market("KXHIGHNY-26JUL21-T79"))
    assert spec.status is SettlementStatus.RESOLVED
    assert spec.confidence is Confidence.HIGH
    assert spec.station_id == "NYC"
    assert spec.city == "New York"
    assert spec.variable == "tmax_f"
    assert spec.target_date == date(2026, 7, 21)
    assert spec.settlement_source == "NWS Climatological Report (Daily)"
    assert spec.wfo_site == "OKX"
    assert spec.source_location_code == "NYC"
    assert spec.unit == "F"
    assert spec.observation_window == "local_calendar_day"
    assert spec.rounding_rule == "integer_f"
    assert spec.notes == []


def test_real_nyc_low_market_resolves_with_abbreviated_month_and_minimum_wording() -> None:
    """KXLOWTNYC rules use 'minimum temperature' and 'Jul 20, 2026' (abbreviated
    month, trailing comma) -- both observed live and both must parse."""
    spec = parse_settlement(_series("KXLOWTNYC"), _market("KXLOWTNYC-26JUL20-T67"))
    assert spec.status is SettlementStatus.RESOLVED
    assert spec.confidence is Confidence.HIGH
    assert spec.variable == "tmin_f"
    assert spec.target_date == date(2026, 7, 20)


def test_determinism_same_inputs_same_spec() -> None:
    a = parse_settlement(_series("KXHIGHNY"), _market("KXHIGHNY-26JUL21-T79"))
    b = parse_settlement(_series("KXHIGHNY"), _market("KXHIGHNY-26JUL21-T79"))
    assert a == b
    assert a.rules_hash == b.rules_hash


def test_rules_hash_changes_when_rules_change() -> None:
    a = parse_settlement(_series("KXHIGHNY"), _market("KXHIGHNY-26JUL21-T79"))
    b = parse_settlement(
        _series("KXHIGHNY"),
        _market("KXHIGHNY-26JUL21-T79", rules_primary="different rules text"),
    )
    assert a.rules_hash != b.rules_hash


# --- real unsupported markets ------------------------------------------------


def test_real_snow_series_is_unsupported_source() -> None:
    """KXNYCSNOWM's settlement source is bare weather.gov, not a CLI product."""
    spec = parse_settlement(
        _series("KXNYCSNOWM"),
        MarketInfo("KXNYCSNOWM-26AUG-X", "KXNYCSNOWM-26AUG", None, "some rules", None),
    )
    assert spec.status is SettlementStatus.UNSUPPORTED
    assert spec.confidence is Confidence.NONE
    assert any("not an NWS CLI product" in n for n in spec.notes)


def test_real_hourly_series_is_unsupported_source() -> None:
    """KXTEMPBOSH settles via The Weather Company (weather.com), not NWS CLI."""
    spec = parse_settlement(
        _series("KXTEMPBOSH"),
        MarketInfo("KXTEMPBOSH-X", None, None, "some rules", None),
    )
    assert spec.status is SettlementStatus.UNSUPPORTED


def test_real_chicago_series_resolves_to_midway_station() -> None:
    """KXHIGHCHI cites CLI issuedby=MDW / site=LOT. With the CHI registry
    entry added (Task 3B, station-expansion), this now resolves -- exactly
    the outcome the pre-expansion version of this test predicted ('adding
    the station later resolves it'). Note the settlement station is Midway,
    not O'Hare, straight from Kalshi's own settlement URL."""
    spec = parse_settlement(
        _series("KXHIGHCHI"),
        MarketInfo(
            "KXHIGHCHI-26JUL21-B85",
            "KXHIGHCHI-26JUL21",
            None,
            "If the highest temperature recorded at Chicago Midway for July 21, 2026 ...",
            None,
        ),
    )
    assert spec.status is SettlementStatus.RESOLVED
    assert spec.station_id == "CHI"
    assert spec.city == "Chicago"
    assert spec.variable == "tmax_f"
    assert spec.source_location_code == "MDW"
    assert spec.wfo_site == "LOT"
    assert spec.target_date == date(2026, 7, 21)


def test_real_denver_series_resolves() -> None:
    """KXHIGHDEN settlement source (live-fetched 2026-07-21):
    site=BOU, issuedby=DEN."""
    spec = parse_settlement(
        _series("KXHIGHDEN"),
        MarketInfo(
            "KXHIGHDEN-26JUL22-T95",
            "KXHIGHDEN-26JUL22",
            None,
            "If the highest temperature recorded at Denver for July 22, 2026 ...",
            None,
        ),
    )
    assert spec.status is SettlementStatus.RESOLVED
    assert spec.station_id == "DEN"
    assert spec.city == "Denver"
    assert spec.variable == "tmax_f"
    assert spec.wfo_site == "BOU"
    assert spec.target_date == date(2026, 7, 22)


def test_real_lax_low_series_resolves_tmin() -> None:
    """KXLOWTLAX settlement source (live-fetched 2026-07-21):
    site=LOX, issuedby=LAX; a LOW family must map to tmin_f."""
    spec = parse_settlement(
        _series("KXLOWTLAX"),
        MarketInfo(
            "KXLOWTLAX-26JUL22-T69",
            "KXLOWTLAX-26JUL22",
            None,
            "If the lowest temperature recorded at Los Angeles for July 22, 2026 ...",
            None,
        ),
    )
    assert spec.status is SettlementStatus.RESOLVED
    assert spec.station_id == "LAX"
    assert spec.city == "Los Angeles"
    assert spec.variable == "tmin_f"
    assert spec.wfo_site == "LOX"
    assert spec.target_date == date(2026, 7, 22)


def test_unregistered_cli_station_is_still_unsupported_with_actionable_note() -> None:
    """The pre-expansion guarantee survives: a valid CLI citation for a
    station NOT in the registry stays UNSUPPORTED (never guessed), with
    the extracted code recorded so a future registry addition resolves it."""
    spec = parse_settlement(
        SeriesInfo(
            ticker="KXHIGHTBOS",
            title="Boston Maximum Daily Temperature",
            frequency="daily",
            settlement_sources=[
                {
                    "name": "NWS Climatological Report",
                    "url": (
                        "https://forecast.weather.gov/product.php?site=BOX&product=CLI&issuedby=BOS"
                    ),
                }
            ],
        ),
        MarketInfo(
            "KXHIGHTBOS-26JUL21-B85",
            "KXHIGHTBOS-26JUL21",
            None,
            "If the highest temperature recorded at Boston for July 21, 2026 ...",
            None,
        ),
    )
    assert spec.status is SettlementStatus.UNSUPPORTED
    assert spec.source_location_code == "BOS"
    assert spec.station_id is None
    assert any("BOS" in n and "registry" in n for n in spec.notes)


# --- ambiguity and failure paths (never silently guess) ----------------------


def test_date_conflict_between_rules_and_ticker_is_ambiguous() -> None:
    spec = parse_settlement(
        _series("KXHIGHNY"),
        _market(
            "KXHIGHNY-26JUL21-T79",
            rules_primary=(
                "If the highest temperature recorded in Central Park, New York for "
                "July 22, 2026 as reported by the National Weather Service's "
                "Climatological Report (Daily), is less than 79°, then the market "
                "resolves to Yes."
            ),
        ),
    )
    assert spec.status is SettlementStatus.AMBIGUOUS
    assert spec.target_date is None
    assert any("2026-07-22" in n and "2026-07-21" in n for n in spec.notes)


def test_variable_conflict_between_rules_and_title_is_ambiguous() -> None:
    """Rules say lowest, series title says Highest -> conflict, not a guess."""
    spec = parse_settlement(
        _series("KXHIGHNY"),  # title: "Highest temperature in NYC"
        _market(
            "KXHIGHNY-26JUL21-T79",
            title=None,
            rules_primary=(
                "If the lowest temperature recorded in Central Park, New York for "
                "July 21, 2026 as reported by the National Weather Service's "
                "Climatological Report (Daily), is less than 79°, then the market "
                "resolves to Yes."
            ),
        ),
    )
    assert spec.status is SettlementStatus.AMBIGUOUS


def test_rules_mentioning_both_high_and_low_is_ambiguous() -> None:
    spec = parse_settlement(
        _series("KXHIGHNY"),
        _market(
            "KXHIGHNY-26JUL21-T79",
            rules_primary=(
                "If the highest temperature minus the lowest temperature recorded "
                "for July 21, 2026 exceeds 20°, then the market resolves to Yes."
            ),
        ),
    )
    assert spec.status is SettlementStatus.AMBIGUOUS


def test_missing_rules_text_is_unresolved() -> None:
    spec = parse_settlement(
        _series("KXHIGHNY"), _market("KXHIGHNY-26JUL21-T79", rules_primary=None)
    )
    assert spec.status is SettlementStatus.UNRESOLVED
    assert spec.station_id == "NYC"  # source/station were still identifiable


def test_no_settlement_source_is_unresolved() -> None:
    spec = parse_settlement(
        _series("KXHIGHNY", settlement_sources=[]), _market("KXHIGHNY-26JUL21-T79")
    )
    assert spec.status is SettlementStatus.UNRESOLVED


def test_multiple_distinct_cli_urls_is_unresolved_not_picked() -> None:
    spec = parse_settlement(
        _series(
            "KXHIGHNY",
            settlement_sources=[
                {
                    "url": "https://forecast.weather.gov/product.php?site=OKX&product=CLI&issuedby=NYC"
                },
                {
                    "url": "https://forecast.weather.gov/product.php?site=LOT&product=CLI&issuedby=MDW"
                },
            ],
        ),
        _market("KXHIGHNY-26JUL21-T79"),
    )
    assert spec.status is not SettlementStatus.RESOLVED
    assert any("multiple distinct CLI product URLs" in n for n in spec.notes)


def test_wfo_site_mismatch_is_ambiguous() -> None:
    """URL cites the right location code but the wrong issuing office."""
    spec = parse_settlement(
        _series(
            "KXHIGHNY",
            settlement_sources=[
                {
                    "url": "https://forecast.weather.gov/product.php?site=PHI&product=CLI&issuedby=NYC"
                }
            ],
        ),
        _market("KXHIGHNY-26JUL21-T79"),
    )
    assert spec.status is SettlementStatus.AMBIGUOUS
    assert any("registry says 'OKX'" in n for n in spec.notes)


def test_date_from_single_source_resolves_medium_confidence() -> None:
    """Event ticker has no date segment -> rules date alone still resolves,
    but confidence drops and the weakness is noted."""
    spec = parse_settlement(
        _series("KXHIGHNY"),
        _market("KXHIGHNY-26JUL21-T79", event_ticker="KXHIGHNY"),
    )
    assert spec.status is SettlementStatus.RESOLVED
    assert spec.confidence is Confidence.MEDIUM
    assert spec.target_date == date(2026, 7, 21)
    assert any("single source" in n for n in spec.notes)


def test_close_time_far_from_target_date_downgrades_confidence() -> None:
    spec = parse_settlement(
        _series("KXHIGHNY"),
        _market(
            "KXHIGHNY-26JUL21-T79",
            close_time=datetime(2026, 9, 1, tzinfo=UTC),
        ),
    )
    assert spec.status is SettlementStatus.RESOLVED
    assert spec.confidence is Confidence.MEDIUM
    assert any("far from" in n for n in spec.notes)


def test_non_daily_frequency_downgrades_confidence() -> None:
    spec = parse_settlement(
        _series("KXHIGHNY", frequency="weekly"), _market("KXHIGHNY-26JUL21-T79")
    )
    assert spec.status is SettlementStatus.RESOLVED
    assert spec.confidence is Confidence.MEDIUM


# --- non-temperature variable classification (fixed word table) --------------


def test_precipitation_rules_with_cli_source_is_unsupported_not_unresolved() -> None:
    """A monthly rain market citing a valid CLI URL (observed live:
    KXRAINNYCM) is out of SCOPE (non-temperature quantity), not missing
    input -- classified UNSUPPORTED with an explicit reason."""
    m = _market(
        "KXHIGHNY-26JUL21-T79",
        event_ticker="KXRAINNYCM-26AUG",
        rules_primary=(
            "If the total precipitation at Central Park, New York City in "
            "Aug 2026 is strictly greater than 4 inches, then the market "
            "resolves to Yes."
        ),
    )
    spec = parse_settlement(_series("KXHIGHNY", title="Rain in NYC in Aug 2026?"), m)
    assert spec.status is SettlementStatus.UNSUPPORTED
    assert any("non-temperature quantity" in n for n in spec.notes)
    assert spec.market_ticker == m.ticker  # exact source ticker preserved


def test_snowfall_rules_with_cli_source_is_unsupported() -> None:
    spec = parse_settlement(
        _series("KXHIGHNY"),
        _market(
            "KXHIGHNY-26JUL21-T79",
            rules_primary="If total snowfall at Central Park exceeds 2 inches, Yes.",
        ),
    )
    assert spec.status is SettlementStatus.UNSUPPORTED
    assert any("non-temperature quantity" in n for n in spec.notes)


def test_rules_with_no_recognizable_quantity_stay_unresolved() -> None:
    """The genuine-problem signal is preserved: garbled rules naming NO
    quantity at all (temperature OR precipitation) remain UNRESOLVED."""
    spec = parse_settlement(
        _series("KXHIGHNY"),
        _market(
            "KXHIGHNY-26JUL21-T79",
            rules_primary="If the recorded value satisfies the condition, Yes.",
        ),
    )
    assert spec.status is SettlementStatus.UNRESOLVED
    assert any("names neither" in n for n in spec.notes)


def test_both_temperatures_still_ambiguous_not_unsupported() -> None:
    spec = parse_settlement(
        _series("KXHIGHNY"),
        _market(
            "KXHIGHNY-26JUL21-T79",
            rules_primary=(
                "If the highest temperature exceeds the lowest temperature "
                "recorded on July 21, 2026, Yes."
            ),
        ),
    )
    assert spec.status is SettlementStatus.AMBIGUOUS


def test_temperature_rules_mentioning_rain_incidentally_still_resolve() -> None:
    """Backward compatibility: a valid temperature market whose prose happens
    to mention rain resolves exactly as before (the non-temperature table is
    consulted ONLY when no temperature variable was found)."""
    spec = parse_settlement(
        _series("KXHIGHNY"),
        _market(
            "KXHIGHNY-26JUL21-T79",
            rules_primary=(
                "If the highest temperature at Central Park on July 21, 2026 "
                "exceeds 79F (rain or shine), then the market resolves to Yes."
            ),
        ),
    )
    assert spec.status is SettlementStatus.RESOLVED
    assert spec.variable == "tmax_f"


# --- SEA/PHX/MIA pilot fixtures (ADR 0023): registry-only expansion ----------
# Deterministic real-structure fixtures: same CLI URL grammar Kalshi uses in
# production, with each pilot city's exact (issuedby, WFO) pair. The parser
# grammar is UNCHANGED -- these resolve purely because the registry now maps
# the CLI location code.

_PILOT = {
    "SEA": ("SEW", "Seattle"),
    "PHX": ("PSR", "Phoenix"),
    "MIA": ("MFL", "Miami"),
}


def _pilot_series(code: str, wfo: str, *, title: str | None = None) -> SeriesInfo:
    overrides: dict[str, Any] = {
        "settlement_sources": [
            {
                "url": (
                    "https://forecast.weather.gov/product.php"
                    f"?site={wfo}&product=CLI&issuedby={code}"
                )
            }
        ]
    }
    if title is not None:
        overrides["title"] = title
    return _series("KXHIGHNY", **overrides)


def test_pilot_cities_daily_high_resolves() -> None:
    for code, (wfo, city) in _PILOT.items():
        spec = parse_settlement(
            _pilot_series(code, wfo),
            _market(
                "KXHIGHNY-26JUL21-T79",
                rules_primary=(
                    "If the highest temperature recorded on July 21, 2026 is "
                    "above 79F, then the market resolves to Yes."
                ),
            ),
        )
        assert spec.status is SettlementStatus.RESOLVED, (code, spec.notes)
        assert spec.station_id == code
        assert spec.wfo_site == wfo
        assert spec.city == city
        assert spec.variable == "tmax_f"
        assert spec.target_date == date(2026, 7, 21)


def test_pilot_cities_daily_low_resolves() -> None:
    for code, (wfo, city) in _PILOT.items():
        spec = parse_settlement(
            _pilot_series(code, wfo, title=f"Lowest temperature in {city}"),
            _market(
                "KXHIGHNY-26JUL21-T79",
                title=None,
                rules_primary=(
                    "If the lowest temperature recorded on July 21, 2026 is "
                    "below 60F, then the market resolves to Yes."
                ),
            ),
        )
        assert spec.status is SettlementStatus.RESOLVED, (code, spec.notes)
        assert spec.station_id == code
        assert spec.variable == "tmin_f"


def test_pilot_city_wrong_wfo_fails_closed() -> None:
    # SEA cited with the wrong WFO must be AMBIGUOUS, never silently mapped
    spec = parse_settlement(
        _pilot_series("SEA", "PSR"),  # Phoenix's WFO on Seattle's code
        _market("KXHIGHNY-26JUL21-T79"),
    )
    assert spec.status is SettlementStatus.AMBIGUOUS
    assert any("WFO" in n for n in spec.notes)


def test_pilot_city_precipitation_stays_unsupported() -> None:
    spec = parse_settlement(
        _pilot_series("MIA", "MFL"),
        _market(
            "KXHIGHNY-26JUL21-T79",
            rules_primary=(
                "If the total precipitation at Miami International in Aug 2026 "
                "is strictly greater than 8 inches, then the market resolves to Yes."
            ),
        ),
    )
    assert spec.status is SettlementStatus.UNSUPPORTED
    assert any("non-temperature quantity" in n for n in spec.notes)
