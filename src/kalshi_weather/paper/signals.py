"""Versioned, expiring, provenance-hashed paper-trading signals.

Only four source types exist, none predictive: a constant probability, a
deterministic threshold rule over current market data (an operational test
pattern, explicitly not a strategy), a manually supplied probability file,
and a manually supplied order-intent file. Every signal carries a version,
a generation timestamp, an expiry, and a content hash; unversioned or
expired signals are rejected, never silently accepted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any


class SignalError(ValueError):
    """A signal that cannot be accepted (with a specific reason)."""


class SignalSourceType(StrEnum):
    CONSTANT = "constant"
    THRESHOLD_RULE = "threshold_rule"
    PROBABILITY_FILE = "probability_file"
    INTENT_FILE = "intent_file"


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class PaperSignal:
    """One signal for one market. ``probability`` drives translation-based
    intents; ``side``/``quantity``/``limit_price_cents`` are set only for
    manual INTENT_FILE signals (which bypass translation, not risk)."""

    source_type: SignalSourceType
    version: str
    generated_at: datetime
    ticker: str
    confidence: Decimal
    expires_at: datetime
    probability: Decimal | None = None
    side: str | None = None  # "yes" | "no" (intent-file only)
    quantity: int | None = None  # intent-file only
    limit_price_cents: int | None = None  # intent-file only
    provenance_hash: str = field(default="")

    def content_payload(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type.value,
            "version": self.version,
            "generated_at": _utc(self.generated_at).isoformat(),
            "ticker": self.ticker,
            "confidence": str(self.confidence),
            "expires_at": _utc(self.expires_at).isoformat(),
            "probability": None if self.probability is None else str(self.probability),
            "side": self.side,
            "quantity": self.quantity,
            "limit_price_cents": self.limit_price_cents,
        }

    def with_hash(self) -> PaperSignal:
        digest = hashlib.sha256(_canonical(self.content_payload()).encode()).hexdigest()
        return PaperSignal(**{**self.__dict__, "provenance_hash": digest})


def validate_signal(signal: PaperSignal, *, now: datetime) -> str | None:
    """Reason the signal must be rejected, or None if acceptable."""
    if not signal.version.strip():
        return "unversioned_signal"
    if not signal.ticker.strip():
        return "missing_ticker"
    if not signal.provenance_hash:
        return "missing_provenance_hash"
    expected = hashlib.sha256(_canonical(signal.content_payload()).encode()).hexdigest()
    if signal.provenance_hash != expected:
        return "provenance_hash_mismatch"
    if _utc(now) > _utc(signal.expires_at):
        return "expired_signal"
    if signal.probability is not None and not Decimal(0) <= signal.probability <= Decimal(1):
        return "probability_out_of_range"
    if signal.probability is None and signal.side not in ("yes", "no"):
        return "no_probability_or_side"
    if not Decimal(0) <= signal.confidence <= Decimal(1):
        return "confidence_out_of_range"
    return None


# --- source builders ---------------------------------------------------------


def constant_signals(
    *,
    tickers: list[str],
    probability: Decimal,
    generated_at: datetime,
    ttl_minutes: int = 60,
    confidence: Decimal = Decimal("1.0"),
    version: str = "constant-v1",
) -> list[PaperSignal]:
    """The simplest synthetic source: one fixed probability for each ticker."""
    return [
        PaperSignal(
            source_type=SignalSourceType.CONSTANT,
            version=version,
            generated_at=generated_at,
            ticker=t,
            confidence=confidence,
            expires_at=generated_at + timedelta(minutes=ttl_minutes),
            probability=probability,
        ).with_hash()
        for t in tickers
    ]


def threshold_rule_signals(
    *,
    quotes: list[tuple[str, int | None, int | None]],  # (ticker, yes_bid, yes_ask)
    offset_cents: int,
    generated_at: datetime,
    ttl_minutes: int = 60,
    version: str = "threshold-rule-v1",
) -> list[PaperSignal]:
    """Deterministic rule over CURRENT market data: probability = mid + offset.

    An operational test pattern (exercises the disagree-with-market path
    deterministically); explicitly NOT a strategy and not predictive.
    """
    out: list[PaperSignal] = []
    for ticker, bid, ask in quotes:
        if bid is None or ask is None:
            continue  # rule requires a two-sided quote; skip silently is fine pre-signal
        mid_cents = Decimal(bid + ask) / 2
        p = (mid_cents + offset_cents) / Decimal(100)
        p = max(Decimal("0.01"), min(Decimal("0.99"), p))
        out.append(
            PaperSignal(
                source_type=SignalSourceType.THRESHOLD_RULE,
                version=version,
                generated_at=generated_at,
                ticker=ticker,
                confidence=Decimal("1.0"),
                expires_at=generated_at + timedelta(minutes=ttl_minutes),
                probability=p,
            ).with_hash()
        )
    return out


def _require(entry: dict[str, Any], key: str, path: Path) -> Any:
    if key not in entry or entry[key] in (None, ""):
        raise SignalError(f"{path.name}: signal entry missing required field {key!r}")
    return entry[key]


def load_probability_file(path: Path) -> list[PaperSignal]:
    """Manual probability file: JSON list of objects with required fields
    version, generated_at, ticker, probability, confidence, expires_at."""
    entries = json.loads(path.read_text())
    if not isinstance(entries, list):
        raise SignalError(f"{path.name}: expected a JSON list of signals")
    out: list[PaperSignal] = []
    for e in entries:
        out.append(
            PaperSignal(
                source_type=SignalSourceType.PROBABILITY_FILE,
                version=str(_require(e, "version", path)),
                generated_at=datetime.fromisoformat(_require(e, "generated_at", path)),
                ticker=str(_require(e, "ticker", path)),
                confidence=Decimal(str(_require(e, "confidence", path))),
                expires_at=datetime.fromisoformat(_require(e, "expires_at", path)),
                probability=Decimal(str(_require(e, "probability", path))),
            ).with_hash()
        )
    return out


def load_intent_file(path: Path) -> list[PaperSignal]:
    """Manual order-intent file: JSON list with version, generated_at, ticker,
    side, quantity, limit_price_cents, confidence, expires_at. These bypass
    probability translation but never bypass risk checks."""
    entries = json.loads(path.read_text())
    if not isinstance(entries, list):
        raise SignalError(f"{path.name}: expected a JSON list of intents")
    out: list[PaperSignal] = []
    for e in entries:
        side = str(_require(e, "side", path))
        if side not in ("yes", "no"):
            raise SignalError(f"{path.name}: side must be 'yes' or 'no', got {side!r}")
        out.append(
            PaperSignal(
                source_type=SignalSourceType.INTENT_FILE,
                version=str(_require(e, "version", path)),
                generated_at=datetime.fromisoformat(_require(e, "generated_at", path)),
                ticker=str(_require(e, "ticker", path)),
                confidence=Decimal(str(_require(e, "confidence", path))),
                expires_at=datetime.fromisoformat(_require(e, "expires_at", path)),
                side=side,
                quantity=int(_require(e, "quantity", path)),
                limit_price_cents=int(_require(e, "limit_price_cents", path)),
            ).with_hash()
        )
    return out
