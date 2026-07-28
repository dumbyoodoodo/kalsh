"""E0002 exploratory liquidity module: deterministic descriptive statistics,
reserved-window refusal, as-of enforcement, and isolation (no frozen-program,
paper, or execution coupling). Synthetic fixtures only."""

import re
from datetime import date, datetime
from pathlib import Path

import pytest

from kalshi_weather.research.e0002_liquidity import (
    BANNER,
    RESERVED_WINDOWS,
    AsOfError,
    BookRow,
    ReservedWindowError,
    TradeRow,
    book_depths,
    city_code,
    family,
    presence,
    quantiles,
    run_liquidity_summary,
)

AS_OF = datetime(2026, 7, 28, 0, 0, 0)


def book(
    ticker: str = "KXHIGHTPHX-26JUL27-B85.5",
    at: datetime = datetime(2026, 7, 27, 12, 0, 0),
    bid: int | None = 40,
    ask: int | None = 45,
    env: str | None = "production",
    bid_depth: int = 100,
    ask_depth: int = 50,
    total: int = 300,
) -> BookRow:
    spread = ask - bid if bid is not None and ask is not None else None
    return BookRow(
        market_ticker=ticker,
        captured_at=at,
        best_yes_bid_cents=bid,
        best_yes_ask_cents=ask,
        spread_cents=spread,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        total_depth=total,
        environment=env,
    )


def trade(
    ticker: str = "KXHIGHTPHX-26JUL27-B85.5",
    at: datetime = datetime(2026, 7, 27, 13, 0, 0),
    count: int = 5,
    env: str | None = "production",
) -> TradeRow:
    return TradeRow(
        market_ticker=ticker, executed_at=at, price_cents=42, count=count, environment=env
    )


def test_book_depths_from_bid_levels() -> None:
    # yes bids: best is 40 (qty 100); no bids: best is 55 (qty 30) -> backs the yes ask
    bid_d, ask_d, total = book_depths([[38, 20], [40, 100]], [[55, 30], [50, 10]])
    assert bid_d == 100
    assert ask_d == 30
    assert total == 160
    assert book_depths([], []) == (0, 0, 0)


def test_family_and_city_code() -> None:
    assert family("KXHIGHTPHX-26JUL27-B85.5") == "KXHIGHTPHX"
    assert city_code("KXHIGHTPHX-26JUL27-B85.5") == "PHX"
    assert city_code("KXLOWTMIA-26JUL27-B70.5") == "MIA"
    assert city_code("KXHIGHNY-26JUL27-B90.5") == "NY"  # legacy code kept verbatim
    assert city_code("KXTEMPNYCH-26JUL27-T90") == "NYC"
    assert city_code("KXRAIN-26JUL-NYC") is None
    assert city_code("KXFIRSTHURRICANE-26AUG") is None


def test_presence_classification() -> None:
    assert presence(book(bid=40, ask=45)) == "two_sided"
    assert presence(book(bid=40, ask=None)) == "bid_only"
    assert presence(book(bid=None, ask=45)) == "ask_only"
    assert presence(book(bid=None, ask=None)) == "empty"


def test_quantiles_deterministic() -> None:
    q = quantiles([1, 2, 3, 4, 5])
    assert q is not None
    assert q["p50"] == 3.0
    assert q["p10"] == 1.4
    assert q["n"] == 5.0
    assert quantiles([]) is None


def test_reserved_window_rows_are_refused() -> None:
    inside = book(at=datetime(2026, 8, 15, 12, 0, 0))
    with pytest.raises(ReservedWindowError):
        run_liquidity_summary([inside], [], as_of=datetime(2026, 10, 10))
    inside_trade = trade(at=datetime(2026, 9, 10, 12, 0, 0))
    with pytest.raises(ReservedWindowError):
        run_liquidity_summary([], [inside_trade], as_of=datetime(2026, 10, 10))


def test_rows_after_as_of_are_refused() -> None:
    with pytest.raises(AsOfError):
        run_liquidity_summary([book(at=datetime(2026, 7, 29))], [], as_of=AS_OF)


def test_reserved_windows_cover_frozen_program_calendar() -> None:
    (start, end), = RESERVED_WINDOWS
    assert start == date(2026, 8, 12)
    assert end == date(2026, 10, 6)


def test_summary_shape_and_determinism() -> None:
    books = [
        book(),
        book(ticker="KXHIGHTSEA-26JUL27-B75.5", bid=30, ask=40, bid_depth=10, ask_depth=5),
        book(ticker="KXRAIN-26JUL-NYC", bid=None, ask=None, env="demo"),
    ]
    trades = [trade(), trade(ticker="KXHIGHTSEA-26JUL27-B75.5", count=2)]
    s1 = run_liquidity_summary(books, trades, as_of=AS_OF)
    s2 = run_liquidity_summary(books, trades, as_of=AS_OF)
    assert s1 == s2  # deterministic for a fixed as-of and inputs
    assert s1["banner"] == BANNER
    assert s1["primary_frame"] == "production environment only"
    prim = s1["primary"]
    assert prim["inputs"]["book_rows"] == 2  # demo row excluded from primary
    assert prim["quote_presence"]["overall"]["rates"]["two_sided"] == 1.0
    assert prim["spread_cents_two_sided"]["overall"]["p50"] == 7.5
    assert prim["spread_cents_two_sided"]["by_city"]["PHX"]["p50"] == 5.0
    robust = s1["robustness"]
    assert robust["all_environments"]["inputs"]["book_rows"] == 3
    assert robust["all_environments"]["quote_presence"]["overall"]["counts"]["empty"] == 1
    assert set(robust["leave_one_city_out_production"]) == {"PHX", "SEA"}
    assert robust["leave_one_city_out_production"]["PHX"]["median_spread_cents"] == 10.0


def test_zero_trade_share_counts_quoted_ticker_days() -> None:
    books = [
        book(),  # PHX quoted and traded
        book(ticker="KXHIGHTSEA-26JUL27-B75.5"),  # SEA quoted, never traded
    ]
    s = run_liquidity_summary(books, [trade()], as_of=AS_OF)
    tr = s["primary"]["trades"]
    assert tr["quoted_ticker_days"] == 2
    assert tr["quoted_ticker_days_with_zero_trades"] == 1
    assert tr["zero_trade_share_of_quoted_ticker_days"] == 0.5


def test_staleness_gaps_reported_with_dedup_caveat() -> None:
    books = [
        book(at=datetime(2026, 7, 27, 12, 0, 0)),
        book(at=datetime(2026, 7, 27, 12, 5, 0)),
        book(at=datetime(2026, 7, 27, 12, 20, 0)),
    ]
    s = run_liquidity_summary(books, [], as_of=AS_OF)
    gaps = s["primary"]["stored_book_gap_seconds"]
    assert "deduplicated" in gaps["caveat"]
    assert gaps["quantiles"]["n"] == 2.0
    assert gaps["quantiles"]["p50"] == 600.0


def test_module_isolation_no_frozen_paper_or_execution_coupling() -> None:
    src = (
        Path(__file__).resolve().parents[2]
        / "src/kalshi_weather/research/e0002_liquidity.py"
    ).read_text()
    banned_imports = re.compile(
        r"from kalshi_weather\.(experiments|paper|execution|kalshi|storage)"
        r"|import kalshi_weather\.(experiments|paper|execution|kalshi|storage)"
    )
    assert banned_imports.search(src) is None
    # no ledger writes, no registration, no confirmatory status language
    banned_terms = re.compile(
        r"append_record|ledger|\bregister\b|\bsupported\b|\bvalidated\b|profitab",
        re.IGNORECASE,
    )
    assert banned_terms.search(src) is None
    assert "NOT CONFIRMATORY" in src


def test_cli_command_declares_exploratory_banner_and_reads_only() -> None:
    src = (Path(__file__).resolve().parents[2] / "src/kalshi_weather/cli.py").read_text()
    block = src.split("def research_exploratory_e0002")[1].split("\n\n\n")[0]
    assert "EXPLORATORY ONLY" in block
    assert "NOT CONFIRMATORY" in block
    # read-only: only select statements, no DML/DDL, no ledger append
    assert re.search(r"insert |update |delete |create table|append_record", block, re.I) is None
