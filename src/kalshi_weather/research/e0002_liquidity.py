"""E0002 -- exploratory weather-market liquidity and microstructure summary.

EXPLORATORY ONLY -- NOT PREREGISTERED -- NOT CONFIRMATORY. Pure descriptive
statistics (counts, rates, quantiles) over caller-supplied order-book and
trade rows: spreads, displayed depth, quote presence, traded volume,
time-of-day activity, and stored-book staleness. Nothing here fits a model,
selects a threshold, or scores anything against an outcome; outputs may
generate future candidate ideas but can never serve as confirmatory
evidence. All timestamps are naive UTC supplied by the caller (no ambient
clock); every row must predate the injected ``as_of`` and must fall outside
the reserved confirmatory calendar windows of the frozen research program.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

BANNER = "EXPLORATORY ONLY — NOT PREREGISTERED — NOT CONFIRMATORY"

#: Calendar periods reserved by the frozen confirmatory program (registered
#: hold-out windows plus their maximum extensions). Exploratory rows whose
#: capture/execution date falls inside any of these ranges are refused.
RESERVED_WINDOWS: tuple[tuple[date, date], ...] = ((date(2026, 8, 12), date(2026, 10, 6)),)

QUANTILE_POINTS = (0.10, 0.25, 0.50, 0.75, 0.90)


class ReservedWindowError(ValueError):
    """A supplied row falls inside a reserved confirmatory window."""


class AsOfError(ValueError):
    """A supplied row postdates the injected as-of timestamp."""


@dataclass(frozen=True, slots=True)
class BookRow:
    """One stored order-book snapshot, already flattened by the loader."""

    market_ticker: str
    captured_at: datetime
    best_yes_bid_cents: int | None
    best_yes_ask_cents: int | None
    spread_cents: int | None
    bid_depth: int
    ask_depth: int
    total_depth: int
    environment: str | None


@dataclass(frozen=True, slots=True)
class TradeRow:
    """One executed trade."""

    market_ticker: str
    executed_at: datetime
    price_cents: int
    count: int
    environment: str | None


def book_depths(
    yes_levels: list[list[int]] | list[tuple[int, int]],
    no_levels: list[list[int]] | list[tuple[int, int]],
) -> tuple[int, int, int]:
    """(depth at best yes bid, depth at best yes ask, total displayed depth).

    Kalshi books store resting bids per side only; the yes ask is backed by
    the best *no* bid, so ask-side depth is the quantity resting there.
    """
    bid_depth = 0
    if yes_levels:
        best = max(int(p) for p, _q in yes_levels)
        bid_depth = sum(int(q) for p, q in yes_levels if int(p) == best)
    ask_depth = 0
    if no_levels:
        best = max(int(p) for p, _q in no_levels)
        ask_depth = sum(int(q) for p, q in no_levels if int(p) == best)
    total = sum(int(q) for _p, q in yes_levels) + sum(int(q) for _p, q in no_levels)
    return bid_depth, ask_depth, total


def family(ticker: str) -> str:
    """Series-family segment of a market ticker (text before the first '-')."""
    return ticker.split("-", 1)[0]


def city_code(ticker: str) -> str | None:
    """Best-effort city code for temperature-family tickers; None otherwise.

    Codes are reported exactly as they appear in the ticker (legacy 'NY' and
    newer 'NYC' style codes are NOT unified -- callers must not assume the
    labels are canonical station identifiers).
    """
    fam = family(ticker)
    for prefix in ("KXHIGHT", "KXLOWT", "KXHIGH"):
        if fam.startswith(prefix) and len(fam) > len(prefix):
            return fam[len(prefix) :]
    if fam.startswith("KXTEMP") and fam.endswith("H") and len(fam) > 7:
        return fam[6:-1]
    return None


def presence(row: BookRow) -> str:
    """two_sided / bid_only / ask_only / empty for one stored book."""
    has_bid = row.best_yes_bid_cents is not None
    has_ask = row.best_yes_ask_cents is not None
    if has_bid and has_ask:
        return "two_sided"
    if has_bid:
        return "bid_only"
    if has_ask:
        return "ask_only"
    return "empty"


def quantiles(values: list[int] | list[float]) -> dict[str, float] | None:
    """Deterministic linear-interpolation quantiles plus n and mean."""
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    n = len(ordered)
    out: dict[str, float] = {"n": float(n), "mean": round(sum(ordered) / n, 3)}
    for q in QUANTILE_POINTS:
        pos = q * (n - 1)
        lo = int(pos)
        hi = min(lo + 1, n - 1)
        frac = pos - lo
        out[f"p{int(q * 100)}"] = round(ordered[lo] * (1 - frac) + ordered[hi] * frac, 3)
    return out


def _check_frame(
    books: list[BookRow],
    trades: list[TradeRow],
    as_of: datetime,
    reserved: tuple[tuple[date, date], ...],
) -> None:
    for when, ticker in [(b.captured_at, b.market_ticker) for b in books] + [
        (t.executed_at, t.market_ticker) for t in trades
    ]:
        if when > as_of:
            raise AsOfError(
                f"{ticker} row at {when.isoformat()} postdates as_of {as_of.isoformat()}"
            )
        d = when.date()
        for start, end in reserved:
            if start <= d <= end:
                raise ReservedWindowError(
                    f"{ticker} row on {d.isoformat()} falls in reserved window {start}..{end}"
                )


def _presence_rates(rows: list[BookRow]) -> dict[str, Any]:
    counts: dict[str, int] = {"two_sided": 0, "bid_only": 0, "ask_only": 0, "empty": 0}
    for r in rows:
        counts[presence(r)] += 1
    n = len(rows)
    return {
        "n": n,
        "counts": counts,
        "rates": {k: round(v / n, 4) if n else 0.0 for k, v in counts.items()},
    }


def _grouped(rows: list[BookRow], key: Any) -> dict[str, list[BookRow]]:
    out: dict[str, list[BookRow]] = {}
    for r in rows:
        out.setdefault(str(key(r)), []).append(r)
    return out


def _spread_summary(rows: list[BookRow]) -> dict[str, Any] | None:
    return quantiles([r.spread_cents for r in rows if r.spread_cents is not None])


def _staleness_gaps_seconds(rows: list[BookRow]) -> list[float]:
    """Gaps between consecutive *stored* books per ticker. Stored snapshots
    are content-hash deduplicated upstream, so a gap conflates 'book did not
    change across polls' with 'ticker was not polled' -- callers must report
    this caveat alongside the distribution."""
    by_ticker: dict[str, list[datetime]] = {}
    for r in rows:
        by_ticker.setdefault(r.market_ticker, []).append(r.captured_at)
    gaps: list[float] = []
    for stamps in by_ticker.values():
        ordered = sorted(stamps)
        gaps.extend((b - a).total_seconds() for a, b in itertools.pairwise(ordered))
    return gaps


def _trade_summary(trades: list[TradeRow], books: list[BookRow]) -> dict[str, Any]:
    by_fam: dict[str, dict[str, int]] = {}
    by_hour: dict[str, dict[str, int]] = {}
    per_ticker_day: dict[tuple[str, date], int] = {}
    for t in trades:
        f = by_fam.setdefault(family(t.market_ticker), {"trades": 0, "contracts": 0})
        f["trades"] += 1
        f["contracts"] += t.count
        h = by_hour.setdefault(f"{t.executed_at.hour:02d}", {"trades": 0, "contracts": 0})
        h["trades"] += 1
        h["contracts"] += t.count
        key = (t.market_ticker, t.executed_at.date())
        per_ticker_day[key] = per_ticker_day.get(key, 0) + t.count
    quoted_ticker_days = {(b.market_ticker, b.captured_at.date()) for b in books}
    traded_ticker_days = set(per_ticker_day)
    quiet = quoted_ticker_days - traded_ticker_days
    return {
        "total_trades": len(trades),
        "total_contracts": sum(t.count for t in trades),
        "by_family": dict(sorted(by_fam.items())),
        "by_hour_utc": dict(sorted(by_hour.items())),
        "contracts_per_traded_ticker_day": quantiles(list(per_ticker_day.values())),
        "quoted_ticker_days": len(quoted_ticker_days),
        "quoted_ticker_days_with_zero_trades": len(quiet),
        "zero_trade_share_of_quoted_ticker_days": round(
            len(quiet) / len(quoted_ticker_days), 4
        )
        if quoted_ticker_days
        else None,
    }


def summarize(books: list[BookRow], trades: list[TradeRow]) -> dict[str, Any]:
    """Core descriptive block for one (books, trades) frame. No validation."""
    two_sided = [r for r in books if presence(r) == "two_sided"]
    return {
        "inputs": {
            "book_rows": len(books),
            "trade_rows": len(trades),
            "tickers_with_books": len({r.market_ticker for r in books}),
            "tickers_with_trades": len({t.market_ticker for t in trades}),
            "families": len({family(r.market_ticker) for r in books}),
        },
        "quote_presence": {
            "overall": _presence_rates(books),
            "by_family": {
                k: _presence_rates(v)
                for k, v in sorted(_grouped(books, lambda r: family(r.market_ticker)).items())
            },
            "by_hour_utc": {
                k: _presence_rates(v)
                for k, v in sorted(
                    _grouped(books, lambda r: f"{r.captured_at.hour:02d}").items()
                )
            },
        },
        "spread_cents_two_sided": {
            "overall": _spread_summary(two_sided),
            "by_family": {
                k: _spread_summary(v)
                for k, v in sorted(
                    _grouped(two_sided, lambda r: family(r.market_ticker)).items()
                )
            },
            "by_city": {
                k: _spread_summary(v)
                for k, v in sorted(
                    _grouped(two_sided, lambda r: city_code(r.market_ticker)).items()
                )
            },
            "by_hour_utc": {
                k: _spread_summary(v)
                for k, v in sorted(
                    _grouped(two_sided, lambda r: f"{r.captured_at.hour:02d}").items()
                )
            },
        },
        "depth_contracts_two_sided": {
            "at_best_bid": quantiles([r.bid_depth for r in two_sided]),
            "at_best_ask": quantiles([r.ask_depth for r in two_sided]),
            "total_book": quantiles([r.total_depth for r in two_sided]),
        },
        "trades": _trade_summary(trades, books),
        "stored_book_gap_seconds": {
            "caveat": (
                "stored books are content-hash deduplicated; a gap conflates "
                "'unchanged across polls' with 'not polled'"
            ),
            "quantiles": quantiles(_staleness_gaps_seconds(books)),
        },
    }


def run_liquidity_summary(
    books: list[BookRow],
    trades: list[TradeRow],
    *,
    as_of: datetime,
    reserved: tuple[tuple[date, date], ...] = RESERVED_WINDOWS,
) -> dict[str, Any]:
    """Validate the frame, then produce the full exploratory summary with the
    predetermined robustness variants. Raises on as-of or reserved-window
    violations instead of silently filtering."""
    _check_frame(books, trades, as_of, reserved)
    prod_books = [b for b in books if b.environment == "production"]
    prod_trades = [t for t in trades if t.environment == "production"]

    def temp_only(rows: list[BookRow]) -> list[BookRow]:
        return [r for r in rows if city_code(r.market_ticker) is not None]

    cities = sorted({c for r in prod_books if (c := city_code(r.market_ticker)) is not None})
    loo: dict[str, Any] = {}
    for city in cities:
        kept = [r for r in prod_books if city_code(r.market_ticker) != city]
        two = [r for r in kept if presence(r) == "two_sided"]
        spread = _spread_summary(two)
        loo[city] = {
            "median_spread_cents": spread["p50"] if spread else None,
            "two_sided_rate": _presence_rates(kept)["rates"]["two_sided"],
        }

    timestamps = [b.captured_at for b in books] + [t.executed_at for t in trades]
    return {
        "banner": BANNER,
        "as_of": as_of.isoformat(),
        "window": {
            "start": min(timestamps).isoformat() if timestamps else None,
            "end": max(timestamps).isoformat() if timestamps else None,
        },
        "reserved_windows_enforced": [
            {"start": s.isoformat(), "end": e.isoformat()} for s, e in reserved
        ],
        "primary_frame": "production environment only",
        "primary": summarize(prod_books, prod_trades),
        "robustness": {
            "all_environments": summarize(books, trades),
            "temperature_families_only_production": summarize(
                temp_only(prod_books),
                [t for t in prod_trades if city_code(t.market_ticker) is not None],
            ),
            "leave_one_city_out_production": loo,
        },
        "comparisons": [
            "quote presence overall / by family / by hour (planned)",
            "spread quantiles overall / by family / by city / by hour (planned)",
            "depth quantiles at best bid, best ask, total book (planned)",
            "trade counts and contracts by family / by hour; zero-trade share (planned)",
            "stored-book gap distribution, dedup caveat (planned; stale-quote note)",
            "robustness: all-environments, temperature-only, leave-one-city-out (planned)",
        ],
    }
