"""Regression tests for CLAUDE.md's non-negotiable live-trading safety rules."""

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from kalshi_weather.config import Environment, Settings

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "kalshi_weather"

FORBIDDEN_SYMBOLS = {"submit_order", "place_order", "create_order", "send_order"}


def _all_defined_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
    return names


def test_no_order_submission_code_exists_yet() -> None:
    """Milestone 1 is read-only: no order-submission function may exist anywhere."""
    for path in SRC_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        defined = _all_defined_names(tree)
        overlap = defined & FORBIDDEN_SYMBOLS
        assert not overlap, f"{path} defines forbidden order-submission symbol(s): {overlap}"


def test_default_environment_is_demo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KALSHI_ENV", raising=False)
    monkeypatch.delenv("ENABLE_LIVE_TRADING", raising=False)
    monkeypatch.delenv("LIVE_TRADING_CONFIRM", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.kalshi_env == Environment.DEMO


def test_live_trading_flags_default_false() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.enable_live_trading is False
    assert settings.live_trading_confirm is False


@pytest.mark.parametrize(
    "env",
    [Environment.DEVELOPMENT, Environment.DEMO, Environment.BACKTEST],
)
def test_live_trading_flags_rejected_outside_production(env: Environment) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, KALSHI_ENV=env, ENABLE_LIVE_TRADING=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, KALSHI_ENV=env, LIVE_TRADING_CONFIRM=True)  # type: ignore[call-arg]


def test_live_trading_flags_allowed_in_production() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        KALSHI_ENV=Environment.PRODUCTION,
        ENABLE_LIVE_TRADING=True,
        LIVE_TRADING_CONFIRM=True,
    )
    assert settings.enable_live_trading is True
    assert settings.live_trading_confirm is True
