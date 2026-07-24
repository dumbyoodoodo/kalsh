"""Kalshi source-environment provenance (ADR 0013).

Demo (`demo-api.kalshi.co`) and production (`api.elections.kalshi.com`) share
market definitions and settlement results but NOT liquidity: demo has ~no real
trading, so volume/open-interest/trades/order-books collected from it are
meaningless, while production carries the real values. The main collector
currently reads demo; price-sync and metadata-revision read production. Rows
from both land in the same tables under the same ticker.

This module supplies the single canonical mapping from a client's effective
base URL to the value stored alongside each row, so demo- and
production-sourced data become distinguishable going forward. It does **not**
change any endpoint, environment selection, or historical row.
"""

from enum import StrEnum
from urllib.parse import urlparse


class SourceEnvironment(StrEnum):
    """Canonical stored provenance values. Deliberately distinct from
    ``config.Environment`` (which also carries development/backtest and is
    about *runtime selection*): this enum is only ever the three values a
    persisted Kalshi row can carry."""

    DEMO = "demo"
    PRODUCTION = "production"
    #: The row's source cannot be determined -- every row written before ADR
    #: 0013, and any live write whose base URL is unrecognized.
    UNKNOWN = "unknown"


#: Exact hostnames, matched structurally (never by substring), so a path or
#: query component such as ``/redirect?to=demo-api.kalshi.co`` on a different
#: host can never be mistaken for the real environment.
_DEMO_HOST = "demo-api.kalshi.co"
_PRODUCTION_HOST = "api.elections.kalshi.com"


def resolve_kalshi_environment(base_url: str | None) -> SourceEnvironment:
    """Map a Kalshi client's effective base URL to its canonical environment.

    Recognized demo host -> ``demo``; recognized production host ->
    ``production``; missing, malformed, or unrecognized -> ``unknown``. The
    comparison is on the parsed hostname only, so it cannot be spoofed by a
    substring appearing in the scheme, path, query, or userinfo.
    """
    if not base_url:
        return SourceEnvironment.UNKNOWN
    try:
        host = urlparse(base_url).hostname
    except ValueError:
        return SourceEnvironment.UNKNOWN
    if host is None:
        return SourceEnvironment.UNKNOWN
    host = host.lower()
    if host == _DEMO_HOST:
        return SourceEnvironment.DEMO
    if host == _PRODUCTION_HOST:
        return SourceEnvironment.PRODUCTION
    return SourceEnvironment.UNKNOWN
