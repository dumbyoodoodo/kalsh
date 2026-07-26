"""Append-only accounting ledger and the portfolio state derived from it.

Every balance change is a ledger entry; the portfolio is a *pure function* of
the ledger (replay it from empty and you get the identical state). Each entry
records the cash delta it caused AND enough payload to recompute that delta on
replay -- the two are asserted equal, which is the per-event reconciliation
invariant. Money is integer cents throughout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from kalshi_weather.execution.models import (
    AccountingError,
    Action,
    Position,
    Side,
)


class EntryKind(StrEnum):
    DEPOSIT = "deposit"
    RESERVE = "reserve"  # move cash -> reserved
    RELEASE = "release"  # move reserved -> cash
    FILL = "fill"
    SETTLEMENT = "settlement"
    MARK = "mark"  # mark-to-market snapshot (no cash move)


@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    at: datetime
    kind: EntryKind
    cash_delta_cents: int
    reserved_delta_cents: int
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Portfolio:
    """Derived, replayable accounting state. Never mutate its balances except
    via ``apply`` of a ledger entry."""

    initial_cash_cents: int
    cash_cents: int = 0
    reserved_cents: int = 0
    positions: dict[str, Position] = field(default_factory=dict)
    fees_cents: int = 0
    total_deposits_cents: int = 0

    def position(self, ticker: str) -> Position:
        return self.positions.setdefault(ticker, Position(ticker=ticker))

    def apply(self, e: LedgerEntry) -> None:
        """Apply one entry, recomputing its cash delta from the payload and
        asserting it matches the recorded delta (reconciliation)."""
        computed_cash = 0
        computed_reserved = 0
        if e.kind is EntryKind.DEPOSIT:
            computed_cash = int(e.payload["amount_cents"])
            self.total_deposits_cents += computed_cash
        elif e.kind is EntryKind.RESERVE:
            amt = int(e.payload["amount_cents"])
            computed_cash, computed_reserved = -amt, amt
        elif e.kind is EntryKind.RELEASE:
            amt = int(e.payload["amount_cents"])
            computed_cash, computed_reserved = amt, -amt
        elif e.kind is EntryKind.FILL:
            pos = self.position(e.payload["ticker"])
            fee = int(e.payload["fee_cents"])
            computed_cash = pos.apply_fill(
                Side(e.payload["side"]),
                Action(e.payload["action"]),
                int(e.payload["quantity"]),
                int(e.payload["price_cents"]),
                fee,
            )
            self.fees_cents += fee
        elif e.kind is EntryKind.SETTLEMENT:
            pos = self.position(e.payload["ticker"])
            computed_cash = pos.settle(Side(e.payload["result"]))
        elif e.kind is EntryKind.MARK:
            pass  # bookkeeping only
        if computed_cash != e.cash_delta_cents or computed_reserved != e.reserved_delta_cents:
            raise AccountingError(
                f"reconciliation failed at seq {e.seq} ({e.kind}): "
                f"cash {computed_cash} != {e.cash_delta_cents} or "
                f"reserved {computed_reserved} != {e.reserved_delta_cents}"
            )
        self.cash_cents += computed_cash
        self.reserved_cents += computed_reserved

    # --- derived metrics ---------------------------------------------------

    def realized_pnl_cents(self) -> int:
        return sum(p.realized_pnl_cents for p in self.positions.values())

    def unrealized_pnl_cents(self, marks: dict[str, int]) -> int:
        return sum(p.unrealized_pnl_cents(marks.get(p.ticker)) for p in self.positions.values())

    def gross_exposure_cents(self) -> int:
        return sum(p.yes_cost_cents + p.no_cost_cents for p in self.positions.values())

    def net_exposure_cents(self) -> int:
        return sum(p.yes_cost_cents - p.no_cost_cents for p in self.positions.values())

    def max_possible_loss_cents(self) -> int:
        return sum(p.max_loss_cents() for p in self.positions.values())

    def total_equity_cents(self, marks: dict[str, int]) -> int:
        return self.cash_cents + self.reserved_cents + self._position_value_cents(marks)

    def _position_value_cents(self, marks: dict[str, int]) -> int:
        total = 0
        for p in self.positions.values():
            if p.settled:
                continue
            m = marks.get(p.ticker)
            if m is None:
                total += p.yes_cost_cents + p.no_cost_cents  # mark at cost if no price
            else:
                total += p.yes_qty * m + p.no_qty * (100 - m)
        return total

    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.is_open()]


class Ledger:
    """The append-only log. The only writer of balance changes."""

    def __init__(self, portfolio: Portfolio) -> None:
        self._entries: list[LedgerEntry] = []
        self._portfolio = portfolio
        self._seq = 0

    @property
    def entries(self) -> list[LedgerEntry]:
        return list(self._entries)

    @property
    def portfolio(self) -> Portfolio:
        return self._portfolio

    def record(
        self,
        at: datetime,
        kind: EntryKind,
        cash_delta_cents: int,
        reserved_delta_cents: int,
        payload: dict[str, Any],
    ) -> LedgerEntry:
        entry = LedgerEntry(
            seq=self._seq,
            at=at,
            kind=kind,
            cash_delta_cents=cash_delta_cents,
            reserved_delta_cents=reserved_delta_cents,
            payload=payload,
        )
        self._portfolio.apply(entry)  # invariants checked here
        self._check_invariants()
        self._entries.append(entry)
        self._seq += 1
        return entry

    def _check_invariants(self) -> None:
        p = self._portfolio
        if p.cash_cents + p.reserved_cents < 0:
            raise AccountingError(f"cash+reserved went negative: {p.cash_cents}+{p.reserved_cents}")
        if p.reserved_cents < 0:
            raise AccountingError(f"reserved went negative: {p.reserved_cents}")


def replay(initial_cash_cents: int, entries: list[LedgerEntry]) -> Portfolio:
    """Rebuild a portfolio purely from a ledger. Deterministic; duplicate
    settlement entries are idempotent (Position.settle no-ops after the first)."""
    p = Portfolio(initial_cash_cents=initial_cash_cents)
    for e in entries:
        # A settlement replayed twice must reconcile: on the 2nd, settle() -> 0,
        # so the entry's recorded delta must also be 0 to reconcile. The engine
        # only ever records a real settlement once; a re-applied identical entry
        # therefore only reconciles if it was a 0-delta no-op, which is safe.
        p.apply(e)
    return p
