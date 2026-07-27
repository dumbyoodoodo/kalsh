"""Deterministic settlement parser: Kalshi market metadata -> SettlementSpec.

Design (docs/adr/0005-settlement-resolution.md), grounded in live production
data fetched during Milestone 2b research -- every rule below cites a real
observed payload, not an assumption:

1. **Structured fields first.** The series' ``settlement_sources[].url`` is a
   machine-parseable citation (observed live:
   ``https://forecast.weather.gov/product.php?site=OKX&product=CLI&issuedby=NYC``).
   ``product=CLI`` identifies the NWS Climatological Report -- the settlement
   source this project supports -- and ``issuedby`` is exactly the CLI location
   code our station registry keys on (``site`` is the issuing WFO office, used
   as a cross-check). Non-CLI sources observed live (bare ``weather.gov`` for
   snow/rain series, ``weather.com`` for hourly series) are classified
   UNSUPPORTED, never guessed at.
2. **Free text only for what has no structured home, and always
   cross-validated.** The settled *variable* ("highest temperature" /
   "minimum temperature") and *target date* ("July 21, 2026" / "Jul 20,
   2026") exist only in rules prose. The date is parsed from BOTH the rules
   text and the event ticker's date segment (``KXHIGHNY-26JUL21``) and must
   agree; the variable is cross-checked against the series title. Any
   disagreement is AMBIGUOUS -- recorded, never silently resolved.
3. **Ambiguity is an explicit outcome.** Every non-RESOLVED spec carries the
   reason in ``notes``; only RESOLVED specs ever become dataset mappings.

The parser is pure (no IO, no clock): same inputs -> same spec, always.
"""

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

from kalshi_weather.settlement.spec import (
    PARSER_VERSION,
    Confidence,
    SettlementSpec,
    SettlementStatus,
    rules_hash,
)
from kalshi_weather.weather.stations import station_for_location_code

#: Values recorded on every CLI-sourced spec. The NWS Climatological Report
#: (Daily) reports whole-degree Fahrenheit values for the station's local
#: calendar day -- verified against real reports in Milestone 3
#: (docs/adr/0003-weather-data-source.md, weather/cli_parser.py).
CLI_SETTLEMENT_SOURCE = "NWS Climatological Report (Daily)"
CLI_UNIT = "F"
CLI_OBSERVATION_WINDOW = "local_calendar_day"
CLI_ROUNDING_RULE = "integer_f"

_MONTHS = {name.upper(): i for i, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.upper(): i for i, name in enumerate(calendar.month_abbr) if name})

#: "July 21, 2026" or "Jul 20, 2026" (both observed in live rules text).
_RULES_DATE_RE = re.compile(
    r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?\s+(\d{1,2}),?\s+(\d{4})",
    re.IGNORECASE,
)
#: Event-ticker date segment, e.g. "26JUL21" in "KXHIGHNY-26JUL21".
_TICKER_DATE_RE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})$")

_HIGH_RE = re.compile(r"\b(highest|maximum|max(?:imum)?)\s+temperature\b", re.IGNORECASE)
_LOW_RE = re.compile(r"\b(lowest|minimum|min(?:imum)?)\s+temperature\b", re.IGNORECASE)
#: Fixed word table of NON-temperature settlement quantities this parser
#: recognizes but deliberately does not support (observed live: monthly
#: precipitation markets citing a CLI URL, e.g. "total precipitation at
#: Central Park ... in Aug 2026"). Rules naming one of these are classified
#: UNSUPPORTED (out of scope), not UNRESOLVED (missing input) -- a fixed
#: table, never fuzzy matching. Rules naming NO recognizable quantity at all
#: remain UNRESOLVED, preserving the genuine-problem signal.
_NON_TEMPERATURE_RE = re.compile(
    r"\b(precipitation|rainfall|rain|snowfall|snow)\b", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class SeriesInfo:
    """The series-level inputs the parser needs (from the DB or live API)."""

    ticker: str
    title: str | None
    frequency: str | None
    settlement_sources: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class MarketInfo:
    """The market-level inputs the parser needs."""

    ticker: str
    event_ticker: str | None
    title: str | None
    rules_primary: str | None
    close_time: datetime | None


@dataclass(frozen=True, slots=True)
class _SourceMatch:
    url: str
    wfo_site: str
    location_code: str


def _parse_cli_source(sources: list[dict[str, Any]]) -> tuple[_SourceMatch | None, str | None]:
    """Extract the CLI product citation from settlement_sources.

    Returns (match, failure_note). Exactly one distinct CLI URL is required;
    anything else is a described failure, not a guess.
    """
    urls = [str(s["url"]) for s in sources if isinstance(s, dict) and s.get("url")]
    if not urls:
        return None, "no settlement source URL provided"

    matches: dict[str, _SourceMatch] = {}
    for url in urls:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if (
            parsed.hostname
            and parsed.hostname.endswith("weather.gov")
            and query.get("product") == ["CLI"]
            and query.get("site")
            and query.get("issuedby")
        ):
            matches[url] = _SourceMatch(
                url=url, wfo_site=query["site"][0], location_code=query["issuedby"][0]
            )
    if not matches:
        return None, f"settlement source is not an NWS CLI product URL: {urls[0]}"
    if len(matches) > 1:
        return None, f"multiple distinct CLI product URLs: {sorted(matches)}"
    return next(iter(matches.values())), None


def _parse_ticker_date(event_ticker: str | None) -> date | None:
    if not event_ticker:
        return None
    parts = event_ticker.split("-")
    if len(parts) < 2:
        return None
    m = _TICKER_DATE_RE.match(parts[1])
    if not m:
        return None
    year, mon_abbr, day = 2000 + int(m.group(1)), m.group(2), int(m.group(3))
    month = _MONTHS.get(mon_abbr.upper())
    if month is None:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_rules_date(rules: str) -> tuple[date | None, str | None]:
    """First calendar date in the rules text; (None, note) on failure."""
    m = _RULES_DATE_RE.search(rules)
    if not m:
        return None, "no calendar date found in rules text"
    month = _MONTHS[m.group(1).upper()]
    try:
        return date(int(m.group(3)), month, int(m.group(2))), None
    except ValueError:
        return None, f"rules text date is not a valid date: {m.group(0)!r}"


def _parse_variable(rules: str) -> tuple[str | None, str | None]:
    high, low = bool(_HIGH_RE.search(rules)), bool(_LOW_RE.search(rules))
    if high and low:
        return None, "rules text mentions both highest and lowest temperature"
    if high:
        return "tmax_f", None
    if low:
        return "tmin_f", None
    return None, "rules text names neither a highest nor a lowest temperature"


def _title_variable(title: str | None) -> str | None:
    """Variable implied by a title, if it names one at all (cross-check only)."""
    if not title:
        return None
    high, low = bool(_HIGH_RE.search(title)), bool(_LOW_RE.search(title))
    if high == low:  # neither, or (pathologically) both
        return None
    return "tmax_f" if high else "tmin_f"


def parse_settlement(series: SeriesInfo, market: MarketInfo) -> SettlementSpec:
    """Derive a SettlementSpec from one market + its series. Pure and total:
    every input yields a spec; failures are statuses, not exceptions."""
    source_url = None
    if series.settlement_sources:
        first = series.settlement_sources[0]
        source_url = str(first.get("url")) if isinstance(first, dict) else None
    digest = rules_hash(
        market.rules_primary, source_url, market.event_ticker, series.ticker, market.ticker
    )
    notes: list[str] = []

    def spec(status: SettlementStatus, confidence: Confidence, **fields: Any) -> SettlementSpec:
        return SettlementSpec(
            market_ticker=market.ticker,
            series_ticker=series.ticker,
            event_ticker=market.event_ticker,
            status=status,
            confidence=confidence,
            parser_version=PARSER_VERSION,
            rules_hash=digest,
            market_close_time=market.close_time,
            notes=notes,
            **fields,
        )

    # -- 1. settlement source (structured) --------------------------------
    source, source_note = _parse_cli_source(series.settlement_sources)
    if source is None:
        notes.append(source_note or "unusable settlement source")
        status = (
            SettlementStatus.UNSUPPORTED
            if source_note and "not an NWS CLI product" in source_note
            else SettlementStatus.UNRESOLVED
        )
        return spec(status, Confidence.NONE, source_url=source_url)

    common: dict[str, Any] = {
        "settlement_source": CLI_SETTLEMENT_SOURCE,
        "source_url": source.url,
        "wfo_site": source.wfo_site,
        "source_location_code": source.location_code,
        "unit": CLI_UNIT,
        "observation_window": CLI_OBSERVATION_WINDOW,
        "rounding_rule": CLI_ROUNDING_RULE,
    }

    # -- 2. station (registry lookup by CLI location code) ----------------
    station = station_for_location_code(source.location_code)
    if station is None:
        notes.append(
            f"CLI location code {source.location_code!r} (WFO {source.wfo_site!r}) is not in "
            "the station registry; add it to weather/stations.py to support this market"
        )
        return spec(SettlementStatus.UNSUPPORTED, Confidence.NONE, **common)
    if station.wfo_site != source.wfo_site:
        notes.append(
            f"source URL cites WFO {source.wfo_site!r} but registry says "
            f"{station.wfo_site!r} for station {station.station_id!r}"
        )
        return spec(SettlementStatus.AMBIGUOUS, Confidence.NONE, **common)
    common.update({"station_id": station.station_id, "city": station.city})

    # -- 3. rules text: variable + target date ----------------------------
    if not market.rules_primary or not market.rules_primary.strip():
        notes.append("market has no rules_primary text")
        return spec(SettlementStatus.UNRESOLVED, Confidence.NONE, **common)

    variable, var_note = _parse_variable(market.rules_primary)
    if variable is None:
        if var_note and "both" in var_note:
            notes.append(var_note)
            return spec(SettlementStatus.AMBIGUOUS, Confidence.NONE, **common)
        if _NON_TEMPERATURE_RE.search(market.rules_primary):
            # deterministic out-of-scope quantity (precipitation family):
            # recognized and intentionally unsupported, not a missing input
            notes.append(
                "settlement variable is a non-temperature quantity (precipitation/"
                "snow); the CLI daily-temperature parser does not support it"
            )
            return spec(SettlementStatus.UNSUPPORTED, Confidence.NONE, **common)
        notes.append(var_note or "variable not identifiable")
        return spec(SettlementStatus.UNRESOLVED, Confidence.NONE, **common)

    rules_date, date_note = _parse_rules_date(market.rules_primary)
    ticker_date = _parse_ticker_date(market.event_ticker)
    if rules_date is None and ticker_date is None:
        notes.append(date_note or "no target date found")
        notes.append("event ticker has no parseable date segment")
        return spec(SettlementStatus.UNRESOLVED, Confidence.NONE, variable=variable, **common)
    if rules_date is not None and ticker_date is not None and rules_date != ticker_date:
        notes.append(
            f"rules text says {rules_date.isoformat()} but event ticker "
            f"{market.event_ticker!r} says {ticker_date.isoformat()}"
        )
        return spec(SettlementStatus.AMBIGUOUS, Confidence.NONE, variable=variable, **common)
    target = rules_date if rules_date is not None else ticker_date
    assert target is not None

    # -- 4. cross-checks -> confidence ------------------------------------
    confidence = Confidence.HIGH
    if rules_date is None or ticker_date is None:
        missing = "rules text" if rules_date is None else "event ticker"
        notes.append(f"target date from a single source only ({missing} date missing)")
        confidence = Confidence.MEDIUM
    title_var = _title_variable(series.title) or _title_variable(market.title)
    if title_var is not None and title_var != variable:
        notes.append(
            f"rules text implies {variable!r} but series/market title implies {title_var!r}"
        )
        return spec(SettlementStatus.AMBIGUOUS, Confidence.NONE, variable=variable, **common)
    if series.frequency is not None and series.frequency != "daily":
        notes.append(f"series frequency is {series.frequency!r}, expected 'daily'")
        confidence = Confidence.MEDIUM
    if market.close_time is not None and abs((market.close_time.date() - target).days) > 2:
        notes.append(
            f"market close {market.close_time.date().isoformat()} is far from "
            f"target date {target.isoformat()}"
        )
        confidence = Confidence.MEDIUM

    return spec(
        SettlementStatus.RESOLVED, confidence, variable=variable, target_date=target, **common
    )
