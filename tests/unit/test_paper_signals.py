"""Paper signal interface: versioning, expiry, provenance hashing, loaders."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from kalshi_weather.paper.signals import (
    PaperSignal,
    SignalError,
    SignalSourceType,
    constant_signals,
    load_intent_file,
    load_probability_file,
    threshold_rule_signals,
    validate_signal,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def make(**kw: object) -> PaperSignal:
    base: dict[str, object] = {
        "source_type": SignalSourceType.CONSTANT,
        "version": "v1",
        "generated_at": NOW,
        "ticker": "KXHIGHTCHI-TEST-B90",
        "confidence": Decimal("0.9"),
        "expires_at": NOW + timedelta(hours=1),
        "probability": Decimal("0.6"),
    }
    base.update(kw)
    return PaperSignal(**base).with_hash()  # type: ignore[arg-type]


def test_valid_signal_passes_and_hash_is_stable() -> None:
    s = make()
    assert validate_signal(s, now=NOW) is None
    assert s.provenance_hash == make().provenance_hash  # deterministic content hash


def test_unversioned_and_expired_signals_rejected() -> None:
    assert validate_signal(make(version="  "), now=NOW) == "unversioned_signal"
    assert validate_signal(make(), now=NOW + timedelta(hours=2)) == "expired_signal"


def test_tampered_signal_rejected_by_hash() -> None:
    s = make()
    tampered = PaperSignal(**{**s.__dict__, "probability": Decimal("0.99")})
    assert validate_signal(tampered, now=NOW) == "provenance_hash_mismatch"
    missing = PaperSignal(**{**s.__dict__, "provenance_hash": ""})
    assert validate_signal(missing, now=NOW) == "missing_provenance_hash"


def test_range_checks() -> None:
    assert validate_signal(make(probability=Decimal("1.5")), now=NOW) == "probability_out_of_range"
    assert validate_signal(make(confidence=Decimal("2")), now=NOW) == "confidence_out_of_range"
    assert (
        validate_signal(make(probability=None, side=None), now=NOW) == "no_probability_or_side"
    )


def test_constant_and_rule_sources() -> None:
    consts = constant_signals(tickers=["A", "B"], probability=Decimal("0.55"), generated_at=NOW)
    assert [s.ticker for s in consts] == ["A", "B"]
    assert all(validate_signal(s, now=NOW) is None for s in consts)
    rules = threshold_rule_signals(
        quotes=[("A", 40, 44), ("B", None, 50)], offset_cents=2, generated_at=NOW
    )
    assert len(rules) == 1  # one-sided quote skipped
    assert rules[0].probability == Decimal("0.44")  # (42 mid + 2) / 100


def test_probability_file_loader_requires_fields(tmp_path: Path) -> None:
    good = [
        {
            "version": "manual-1",
            "generated_at": NOW.isoformat(),
            "ticker": "T",
            "probability": "0.6",
            "confidence": "0.8",
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        }
    ]
    p = tmp_path / "sig.json"
    p.write_text(json.dumps(good))
    loaded = load_probability_file(p)
    assert loaded[0].probability == Decimal("0.6") and loaded[0].provenance_hash
    bad = [dict(good[0])]
    del bad[0]["version"]
    p.write_text(json.dumps(bad))
    with pytest.raises(SignalError, match="version"):
        load_probability_file(p)


def test_intent_file_loader(tmp_path: Path) -> None:
    entries = [
        {
            "version": "manual-1",
            "generated_at": NOW.isoformat(),
            "ticker": "T",
            "side": "yes",
            "quantity": 2,
            "limit_price_cents": 45,
            "confidence": "1",
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        }
    ]
    p = tmp_path / "intents.json"
    p.write_text(json.dumps(entries))
    loaded = load_intent_file(p)
    assert loaded[0].side == "yes" and loaded[0].quantity == 2
    entries[0]["side"] = "maybe"
    p.write_text(json.dumps(entries))
    with pytest.raises(SignalError, match="side"):
        load_intent_file(p)
