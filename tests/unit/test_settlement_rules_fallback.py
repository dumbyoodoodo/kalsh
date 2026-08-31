"""Rules-text fallback for CLI settlement parsing (ADR 0026).

Kalshi replaced the structured CLI citation on weather series with a generic
partner link -- ``[{"name":"The Weather Company","url":"https://weather.com/kalshi"}]``
-- while the authoritative rules prose still cites the NWS Climatological
Report. Every in-scope market therefore parsed as UNSUPPORTED and settlement
resolution returned 0 specs from 43,203 markets, which took H0019's readiness
to zero event-groups in every split.

Every rules string below is a VERBATIM shape from the preserved production
corpus, not invented prose. The two location phrasings are both real and
differ: the daily-high family says "recorded in Central Park, New York" while
the daily-low family says "recorded at New York City".

The fallback is deliberately narrower than the structured path it stands in
for, and these tests exist mostly to pin what it REFUSES.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kalshi_weather.settlement.parser import (
    PROVENANCE_RULES_TEXT,
    PROVENANCE_STRUCTURED,
    MarketInfo,
    SeriesInfo,
    parse_settlement,
)
from kalshi_weather.settlement.spec import SettlementStatus

#: The generic partner link Kalshi now returns for weather series.
GENERIC_SOURCES = [{"name": "The Weather Company", "url": "https://weather.com/kalshi"}]
#: The structured citation the parser was originally built around.
LEGACY_CLI_SOURCES = [
    {
        "name": "NWS",
        "url": "https://forecast.weather.gov/product.php?site=OKX&product=CLI&issuedby=NYC",
    }
]

# --- verbatim production rules shapes ----------------------------------------

RULES_DAILY_HIGH = (
    "If the highest temperature recorded in Central Park, New York for August 06, 2026 "
    "as reported by the National Weather Service's Climatological Report (Daily), is "
    "less than 88°, then the market resolves to Yes."
)
RULES_DAILY_LOW = (
    "If the minimum temperature recorded at New York City for Aug 6, 2026, is less than "
    "68° fahrenheit according to the National Weather Service's Climatological Report "
    "(Daily), then the market resolves to Yes."
)
RULES_HOURLY_WEATHER_COMPANY = (
    "If the temperature recorded at Chicago, IL for Aug 6, 2026 8 AM EDT as reported by "
    "The Weather Company (for coordinates KORD), is above 70.99°, then the market "
    "resolves to Yes."
)


def build(
    rules: str | None,
    *,
    sources: list[dict[str, object]] | None = None,
    event_ticker: str | None = "KXHIGHNY-26AUG06",
):
    return parse_settlement(
        SeriesInfo(
            ticker="KXHIGHNY",
            title=None,
            frequency=None,
            settlement_sources=list(sources if sources is not None else GENERIC_SOURCES),
        ),
        MarketInfo(
            ticker="KXHIGHNY-26AUG06-T88",
            event_ticker=event_ticker,
            title=None,
            rules_primary=rules,
            close_time=datetime(2026, 8, 7, tzinfo=UTC),
        ),
    )


# --- the structured path still wins -------------------------------------------


def test_structured_cli_citation_still_resolves_and_is_labelled_structured() -> None:
    """Backward compatibility: a market with the old citation is unaffected."""
    spec = build(RULES_DAILY_HIGH, sources=LEGACY_CLI_SOURCES)
    assert spec.status is SettlementStatus.RESOLVED
    assert spec.station_id == "NYC"
    assert spec.source_provenance == PROVENANCE_STRUCTURED
    assert spec.source_url and "weather.gov" in spec.source_url


def test_structured_path_is_preferred_when_both_are_available() -> None:
    """The fallback must never pre-empt a usable structured citation."""
    assert build(RULES_DAILY_HIGH, sources=LEGACY_CLI_SOURCES).source_provenance == (
        PROVENANCE_STRUCTURED
    )
    assert build(RULES_DAILY_HIGH).source_provenance == PROVENANCE_RULES_TEXT


# --- fallback successes --------------------------------------------------------


def test_daily_high_resolves_via_rules_fallback() -> None:
    spec = build(RULES_DAILY_HIGH)
    assert spec.status is SettlementStatus.RESOLVED
    assert (spec.station_id, spec.variable) == ("NYC", "tmax_f")
    assert spec.target_date.isoformat() == "2026-08-06"
    assert spec.source_provenance == PROVENANCE_RULES_TEXT


def test_daily_low_resolves_via_rules_fallback() -> None:
    """Different phrasing from the high family -- 'recorded at New York City'."""
    spec = build(RULES_DAILY_LOW, event_ticker="KXLOWTNYC-26AUG06")
    assert spec.status is SettlementStatus.RESOLVED
    assert (spec.station_id, spec.variable) == ("NYC", "tmin_f")
    assert spec.target_date.isoformat() == "2026-08-06"
    assert spec.source_provenance == PROVENANCE_RULES_TEXT


def test_fallback_records_why_it_was_used() -> None:
    """Provenance must be auditable from the spec, not just inferable."""
    notes = " ".join(build(RULES_DAILY_HIGH).notes).lower()
    assert "structured settlement source unusable" in notes
    assert "rules text" in notes


def test_fallback_keeps_the_cli_settlement_constants() -> None:
    spec = build(RULES_DAILY_HIGH)
    assert spec.settlement_source == "NWS Climatological Report (Daily)"
    assert (spec.unit, spec.observation_window, spec.rounding_rule) == (
        "F",
        "local_calendar_day",
        "integer_f",
    )
    # Registry-derived, since no URL supplied them.
    assert (spec.source_location_code, spec.wfo_site) == ("NYC", "OKX")


# --- refusals: the point of the exercise ---------------------------------------


def test_hourly_weather_company_market_stays_unsupported() -> None:
    """KXTEMP*H is a genuinely different product: instantaneous reading, and a
    different settlement authority. It must not be dragged into scope."""
    spec = build(RULES_HOURLY_WEATHER_COMPANY, event_ticker="KXTEMPCHIH-26AUG0608")
    assert spec.status is SettlementStatus.UNSUPPORTED
    assert spec.station_id is None
    assert spec.source_provenance is None


def test_weather_company_is_rejected_even_without_a_time_of_day() -> None:
    """Each disqualifier stands alone -- neither is load-bearing by itself."""
    rules = (
        "If the highest temperature recorded in Central Park, New York for August 06, "
        "2026 as reported by The Weather Company, is less than 88°, then the market "
        "resolves to Yes."
    )
    assert build(rules).status is not SettlementStatus.RESOLVED


def test_time_of_day_is_rejected_even_with_nws_cli_wording() -> None:
    """A CLI citation cannot make an instantaneous reading a daily aggregate."""
    rules = (
        "If the temperature recorded in Central Park, New York for August 06, 2026 "
        "8 AM EDT as reported by the National Weather Service's Climatological Report "
        "(Daily), is less than 88°, then the market resolves to Yes."
    )
    assert build(rules).status is not SettlementStatus.RESOLVED


@pytest.mark.parametrize(
    "rules",
    [
        # Weather-ish prose, but names no settlement authority at all.
        "If the highest temperature recorded in Central Park, New York for August 06, "
        "2026 is less than 88°, then the market resolves to Yes.",
        # Names NWS but not the Climatological Report product.
        "If the highest temperature recorded in Central Park, New York for August 06, "
        "2026 as reported by the National Weather Service, is less than 88°, then the "
        "market resolves to Yes.",
        # Names a climatological report but not the NWS.
        "If the highest temperature recorded in Central Park, New York for August 06, "
        "2026 as reported by the Climatological Report (Daily), is less than 88°, then "
        "the market resolves to Yes.",
    ],
)
def test_both_nws_and_cli_wording_are_required(rules: str) -> None:
    """Deliberately two required matches, so generic weather language cannot
    open the fallback."""
    assert build(rules).status is not SettlementStatus.RESOLVED


def test_unknown_location_is_refused_not_guessed() -> None:
    rules = (
        "If the highest temperature recorded in Fargo, North Dakota for August 06, 2026 "
        "as reported by the National Weather Service's Climatological Report (Daily), "
        "is less than 88°, then the market resolves to Yes."
    )
    spec = build(rules)
    assert spec.status is not SettlementStatus.RESOLVED
    assert spec.station_id is None
    assert any("does not match any station" in n for n in spec.notes)


def test_ambiguous_location_fails_closed() -> None:
    """Two registry stations named in one phrase must never be silently
    resolved by preference order."""
    rules = (
        "If the highest temperature recorded in New York and Chicago for August 06, "
        "2026 as reported by the National Weather Service's Climatological Report "
        "(Daily), is less than 88°, then the market resolves to Yes."
    )
    spec = build(rules)
    assert spec.status is not SettlementStatus.RESOLVED
    assert spec.station_id is None
    assert any("multiple registry stations" in n for n in spec.notes)


@pytest.mark.parametrize("rules", ["", "   ", None])
def test_missing_rules_text_cannot_resolve(rules: str | None) -> None:
    assert build(rules).status is not SettlementStatus.RESOLVED


def test_malformed_rules_without_a_location_phrase_is_refused() -> None:
    rules = (
        "This market settles per the National Weather Service's Climatological Report "
        "(Daily) for August 06, 2026."
    )
    spec = build(rules)
    assert spec.status is not SettlementStatus.RESOLVED
    assert any("settlement location" in n for n in spec.notes)


# --- date handling --------------------------------------------------------------


@pytest.mark.parametrize(
    ("date_text", "expected"),
    [
        ("August 06, 2026", "2026-08-06"),
        ("Aug 6, 2026", "2026-08-06"),
        ("December 31, 2026", "2026-12-31"),
        ("Jan 1, 2027", "2027-01-01"),
    ],
)
def test_date_formats_observed_in_production(date_text: str, expected: str) -> None:
    rules = (
        f"If the highest temperature recorded in Central Park, New York for {date_text} "
        "as reported by the National Weather Service's Climatological Report (Daily), "
        "is less than 88°, then the market resolves to Yes."
    )
    months = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
    yy, mm, dd = expected[2:4], months[int(expected[5:7]) - 1], expected[8:10]
    spec = build(rules, event_ticker=f"KXHIGHNY-{yy}{mm}{dd}")
    assert spec.target_date is not None
    assert spec.target_date.isoformat() == expected


def test_rules_date_disagreeing_with_ticker_date_is_not_resolved() -> None:
    """The existing prose/ticker cross-check must survive the fallback."""
    spec = build(RULES_DAILY_HIGH, event_ticker="KXHIGHNY-26AUG09")
    assert spec.status is not SettlementStatus.RESOLVED


# --- no ticker inference ---------------------------------------------------------


def test_station_is_never_inferred_from_the_ticker() -> None:
    """A ticker naming one city with prose naming another must follow the
    PROSE, which is authoritative -- and never be rescued by the ticker when
    the prose is unusable."""
    unknown_place = (
        "If the highest temperature recorded in Fargo, North Dakota for August 06, 2026 "
        "as reported by the National Weather Service's Climatological Report (Daily), "
        "is less than 88°, then the market resolves to Yes."
    )
    # Ticker says NYC; prose says a place not in the registry -> refuse.
    assert build(unknown_place, event_ticker="KXHIGHNY-26AUG06").station_id is None


def test_parser_module_contains_no_ticker_to_station_table() -> None:
    """Guards against someone 'fixing' a future breakage with a ticker map.

    Scans EXECUTABLE string literals via the AST, deliberately excluding
    docstrings -- the module docstring legitimately cites ``KXHIGHNY-26JUL21``
    when documenting the event-ticker DATE format, which is not station
    inference. A plain substring scan would forbid that documentation and
    teach the next person to route around the guard.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path("src/kalshi_weather/settlement/parser.py").read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)

    offenders = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
        and "KX" in node.value.upper()
    ]
    assert offenders == [], f"ticker literals in executable code: {offenders}"
