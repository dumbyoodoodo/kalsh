"""CLI for read-only Kalshi market-data collection and inspection.

Every command persists raw payloads (append-only) as they're fetched; the
`market`/`orderbook` commands additionally persist normalized snapshots.
No order-submission command exists here or anywhere else in this milestone.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import typer
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.config import Environment, Settings, get_settings
from kalshi_weather.kalshi.auth import load_private_key_from_setting
from kalshi_weather.kalshi.client import KalshiClient
from kalshi_weather.kalshi.orderbook import reconstruct_best_quote
from kalshi_weather.storage.database import create_engine, create_session_factory, session_scope
from kalshi_weather.storage.repositories import (
    save_market_snapshot,
    save_orderbook_snapshot,
    save_raw_payload,
    save_series,
)

app = typer.Typer(help="Kalshi weather market-data CLI (read-only).")
series_app = typer.Typer(help="Inspect Kalshi series.")
markets_app = typer.Typer(help="Inspect Kalshi markets.")
orderbook_app = typer.Typer(help="Inspect Kalshi order books.")
app.add_typer(series_app, name="series")
app.add_typer(markets_app, name="markets")
app.add_typer(orderbook_app, name="orderbook")


@asynccontextmanager
async def _open_session(settings: Settings) -> AsyncIterator[AsyncSession]:
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            yield session
    finally:
        await engine.dispose()


def _build_client(
    settings: Settings, session: AsyncSession | None = None
) -> KalshiClient:
    base_url = settings.base_url_for(settings.kalshi_env)
    key_id: str | None = None
    private_key: rsa.RSAPrivateKey | None = None
    if settings.kalshi_env == Environment.DEMO and settings.kalshi_demo_private_key:
        key_id = settings.kalshi_demo_api_key_id
        private_key = load_private_key_from_setting(settings.kalshi_demo_private_key)

    raw_payload_sink = None
    if session is not None:

        async def sink(
            source: str, endpoint: str, request_key: str, status: int, payload: Any
        ) -> None:
            await save_raw_payload(
                session,
                source=source,
                endpoint_or_channel=endpoint,
                request_key=request_key,
                http_status=status,
                payload_json=payload,
            )

        raw_payload_sink = sink

    return KalshiClient(
        base_url=base_url,
        environment=settings.kalshi_env,
        key_id=key_id,
        private_key=private_key,
        raw_payload_sink=raw_payload_sink,
    )


@series_app.command("list")
def series_list(
    category: str | None = typer.Option(None, help="Filter by series category."),
) -> None:
    """List Kalshi series, optionally filtered by category (e.g. weather)."""

    async def run() -> None:
        settings = get_settings()
        async with _open_session(settings) as session, _build_client(settings, session) as client:
            series = await client.list_series(category=category)
            for s in series:
                await save_series(
                    session,
                    series_ticker=s.ticker,
                    category=s.category,
                    title=s.title,
                    frequency=s.frequency,
                )
                typer.echo(f"{s.ticker}\t{s.category or '-'}\t{s.title or '-'}")

    asyncio.run(run())


@markets_app.command("list")
def markets_list(
    event_ticker: str | None = typer.Option(None),
    status: str | None = typer.Option(None),
) -> None:
    """List Kalshi markets, optionally filtered by event ticker and status."""

    async def run() -> None:
        settings = get_settings()
        async with _open_session(settings) as session, _build_client(settings, session) as client:
            markets = await client.list_markets(event_ticker=event_ticker, status=status)
            for m in markets:
                typer.echo(f"{m.ticker}\t{m.status or '-'}\t{m.title or '-'}")

    asyncio.run(run())


@app.command("market")
def market_show(ticker: str) -> None:
    """Show a single market snapshot and persist it."""

    async def run() -> None:
        settings = get_settings()
        async with _open_session(settings) as session, _build_client(settings, session) as client:
            market = await client.get_market(ticker)
            await save_market_snapshot(
                session,
                market_ticker=market.ticker,
                event_ticker=market.event_ticker,
                market_type=market.market_type,
                title=market.title,
                subtitle=market.subtitle,
                status=market.status,
                yes_bid_cents=market.yes_bid,
                yes_ask_cents=market.yes_ask,
                last_price_cents=market.last_price,
                volume=market.volume,
                open_interest=market.open_interest,
                close_time=market.close_time,
                rules_primary=market.rules_primary,
                rules_secondary=market.rules_secondary,
                raw_payload_id=None,
            )
            typer.echo(f"ticker:         {market.ticker}")
            typer.echo(f"title:          {market.title or '-'}")
            typer.echo(f"status:         {market.status or '-'}")
            if market.yes_bid is not None:
                typer.echo(f"yes_bid:        {Decimal(market.yes_bid) / 100}")
            if market.yes_ask is not None:
                typer.echo(f"yes_ask:        {Decimal(market.yes_ask) / 100}")
            volume = market.volume if market.volume is not None else "-"
            open_interest = market.open_interest if market.open_interest is not None else "-"
            typer.echo(f"volume:         {volume}")
            typer.echo(f"open_interest:  {open_interest}")

    asyncio.run(run())


@orderbook_app.command("show")
def orderbook_show(ticker: str, depth: int | None = typer.Option(None)) -> None:
    """Show best derived YES/NO bid and ask for a market's order book and persist it."""

    async def run() -> None:
        settings = get_settings()
        async with _open_session(settings) as session, _build_client(settings, session) as client:
            book = await client.get_orderbook(ticker, depth=depth)
            await save_orderbook_snapshot(
                session,
                market_ticker=ticker,
                yes_levels=book.orderbook.yes,
                no_levels=book.orderbook.no,
                raw_payload_id=None,
            )
            yes_bids = [(level[0], level[1]) for level in book.orderbook.yes]
            no_bids = [(level[0], level[1]) for level in book.orderbook.no]
            quote = reconstruct_best_quote(yes_bids, no_bids)
            typer.echo(f"best_yes_bid: {quote.best_yes_bid_cents}")
            typer.echo(f"best_yes_ask: {quote.best_yes_ask_cents}")
            typer.echo(f"best_no_bid:  {quote.best_no_bid_cents}")
            typer.echo(f"best_no_ask:  {quote.best_no_ask_cents}")
            typer.echo(f"yes_spread:   {quote.yes_spread_cents}")

    asyncio.run(run())


if __name__ == "__main__":
    app()
