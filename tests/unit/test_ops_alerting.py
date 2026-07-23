import httpx
import pytest

from kalshi_weather.ops.alerting import (
    TelegramConfig,
    send_alert,
    send_telegram_alert,
)


def _client(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_send_telegram_alert_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/bottest-token/sendMessage"
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as client:
        result = await send_telegram_alert(
            TelegramConfig(bot_token="test-token", chat_id="12345"),
            "hello",
            client=client,
        )
    assert result.delivered is True
    assert "delivered" in result.detail


async def test_send_telegram_alert_sends_correct_payload() -> None:
    import json as jsonlib

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = jsonlib.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as client:
        await send_telegram_alert(
            TelegramConfig(bot_token="test-token", chat_id="12345"),
            "hello world",
            client=client,
        )
    assert captured["body"] == {"chat_id": "12345", "text": "hello world"}


async def test_send_telegram_alert_non_200_is_not_delivered() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    async with _client(handler) as client:
        result = await send_telegram_alert(
            TelegramConfig(bot_token="bad-token", chat_id="12345"), "hi", client=client
        )
    assert result.delivered is False
    assert "401" in result.detail
    assert "bad-token" not in result.detail  # never leak the token


async def test_send_telegram_alert_network_error_is_not_delivered() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _client(handler) as client:
        result = await send_telegram_alert(
            TelegramConfig(bot_token="test-token", chat_id="12345"), "hi", client=client
        )
    assert result.delivered is False
    assert "ConnectError" in result.detail


async def test_send_alert_dispatches_to_telegram() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as client:
        result = await send_alert(
            "telegram",
            "hi",
            telegram=TelegramConfig(bot_token="t", chat_id="c"),
            client=client,
        )
    assert result.delivered is True


async def test_send_alert_telegram_selected_but_not_configured() -> None:
    result = await send_alert("telegram", "hi", telegram=None)
    assert result.delivered is False
    assert "not configured" in result.detail


async def test_send_alert_none_transport_is_a_noop() -> None:
    result = await send_alert("none", "hi")
    assert result.delivered is False
    assert "no transport configured" in result.detail


async def test_send_alert_unknown_transport_is_reported_not_raised() -> None:
    result = await send_alert("carrier-pigeon", "hi")
    assert result.delivered is False
    assert "carrier-pigeon" in result.detail


@pytest.mark.parametrize("transport", ["none", "telegram", "carrier-pigeon"])
async def test_send_alert_never_raises(transport: str) -> None:
    # No transport misconfiguration should ever propagate an exception --
    # the monitor run and its history logging must never be blocked by it.
    result = await send_alert(transport, "hi")
    assert result.delivered is False
