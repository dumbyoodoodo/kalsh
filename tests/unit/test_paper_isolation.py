"""The paper package must be incapable of exchange orders and blind to
H0019/H0020: enforced by static source scans, not convention."""

import re
from pathlib import Path

PAPER_DIR = Path(__file__).resolve().parents[2] / "src" / "kalshi_weather" / "paper"


def paper_sources() -> list[tuple[Path, str]]:
    files = sorted(PAPER_DIR.glob("*.py"))
    assert files, f"no paper sources under {PAPER_DIR}"
    return [(p, p.read_text()) for p in files]


def test_no_experiment_or_hypothesis_imports() -> None:
    banned = re.compile(r"kalshi_weather\.experiments|h0019|h0020|EXP-FUTURE|EXP-2026", re.I)
    for path, text in paper_sources():
        match = banned.search(text)
        assert match is None, f"{path.name} references experiments: {match.group(0)!r}"


def test_no_exchange_client_auth_or_websocket_imports() -> None:
    # No import of the Kalshi HTTP client, auth, or websocket layers means no
    # code path in this package can reach the exchange at all.
    banned = re.compile(
        r"from kalshi_weather\.kalshi|import kalshi_weather\.kalshi|httpx|websocket", re.I
    )
    for path, text in paper_sources():
        match = banned.search(text)
        assert match is None, f"{path.name} could reach the exchange: {match.group(0)!r}"


def test_no_live_order_submission_symbols() -> None:
    """Fails if the paper package ever references an order-submission function
    or a live-trading flag -- the explicit guard the task requires."""
    banned = re.compile(
        r"create_order|submit_order|place_order|cancel_order|batch_order"
        r"|enable_live_trading|ENABLE_LIVE_TRADING|/portfolio/orders",
    )
    for path, text in paper_sources():
        match = banned.search(text)
        assert match is None, f"{path.name} references live-order machinery: {match.group(0)!r}"


def test_no_writes_to_research_tables() -> None:
    # The paper package may SELECT research tables (evidence) but must never
    # add/update/delete them: assert no research model is ever constructed.
    banned = re.compile(
        r"MarketSnapshot\(|OrderbookSnapshot\(|Trade\(|RawApiPayload\(|CollectorRun\(|"
        r"MarketPollAttempt\(|WeatherForecast\(|WeatherObservation\("
    )
    for path, text in paper_sources():
        match = banned.search(text)
        assert match is None, f"{path.name} constructs a research row: {match.group(0)!r}"


def test_reports_are_labelled_synthetic_operational() -> None:
    text = (PAPER_DIR / "reporting.py").read_text()
    assert "NOT STRATEGY PERFORMANCE" in text
    engine_text = (PAPER_DIR / "engine.py").read_text()
    assert "DRAFT — NOT VALIDATED FOR LIVE TRADING" in engine_text


def test_store_has_no_update_or_delete() -> None:
    for path, text in paper_sources():
        for token in (".update(", "delete(", "DELETE FROM", "UPDATE "):
            if token == ".update(" and path.name in ("signals.py", "engine.py", "runner.py"):
                continue  # dict.update on plain dicts is fine outside the store
            assert token not in text, f"{path.name} contains {token!r} (append-only violation)"
