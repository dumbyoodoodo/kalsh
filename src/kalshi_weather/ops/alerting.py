"""Alert delivery: turns a monitor decision into an outbound notification.

Detection lives entirely in ``observatory/``; alert-state/suppression logic
lives in ``ops/monitor.py``. This module's only job is "given a transport is
configured and a message, deliver it" -- one transport today (Telegram's bot
API, a plain authenticated HTTP POST -- no new dependency beyond ``httpx``,
already required stack). Adding a second transport later is a new
``send_*_alert`` function plus one more branch in ``send_alert``, not a
redesign.

Never logs, echoes, or raises an exception containing the bot token -- only
delivery success/failure and a sanitized detail string reach the caller.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

TELEGRAM_API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    bot_token: str
    chat_id: str


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    delivered: bool
    detail: str


async def send_telegram_alert(
    config: TelegramConfig,
    message: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> DeliveryResult:
    """POST to the Telegram bot API's ``sendMessage`` endpoint. ``client`` is
    injectable so tests can supply an ``httpx.MockTransport`` and never make
    a real network call (CLAUDE.md: every external request needs a timeout
    -- ``REQUEST_TIMEOUT_SECONDS`` -- and structured error handling, not a
    bare ``except``)."""
    url = f"{TELEGRAM_API_BASE}/bot{config.bot_token}/sendMessage"
    payload = {"chat_id": config.chat_id, "text": message}
    owns_client = client is None
    http_client = (
        client if client is not None else httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
    )
    try:
        response = await http_client.post(url, json=payload)
    except httpx.HTTPError as exc:
        return DeliveryResult(
            delivered=False, detail=f"telegram request failed: {type(exc).__name__}"
        )
    finally:
        if owns_client:
            await http_client.aclose()

    if response.status_code == 200:
        return DeliveryResult(delivered=True, detail="telegram: delivered")
    return DeliveryResult(delivered=False, detail=f"telegram: HTTP {response.status_code}")


async def send_alert(
    transport: str,
    message: str,
    *,
    telegram: TelegramConfig | None = None,
    client: httpx.AsyncClient | None = None,
) -> DeliveryResult:
    """Dispatch ``message`` via the configured ``transport``. ``"none"`` (the
    default) is a deliberate no-op, not an error -- an unconfigured monitor
    still runs, decides, and logs history; it just never calls out. An
    unrecognized transport name is likewise reported, never raised: an alert
    misconfiguration must never crash the monitor run or block history
    logging (the same "uptime/bookkeeping beats a crash" posture as
    ``scripts/service/launch.py``'s manifest write)."""
    if transport == "telegram":
        if telegram is None:
            return DeliveryResult(
                delivered=False, detail="telegram transport selected but not configured"
            )
        return await send_telegram_alert(telegram, message, client=client)
    if transport == "none":
        return DeliveryResult(delivered=False, detail="no transport configured")
    return DeliveryResult(delivered=False, detail=f"unknown transport {transport!r}")
