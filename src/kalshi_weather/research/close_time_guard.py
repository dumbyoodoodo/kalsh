"""Close-time revision guard: the approval-gated check from the 2026-07-27
data-lineage audit's one AMBIGUOUS finding.

Some research designs derive ``decision_time = close_time - horizon``. Kalshi
*can* revise a market's close_time, and the revised value lands as a later
append-only snapshot -- so a decision_time computed from the final close may
differ from what was knowable earlier. This guard reconstructs, from the
append-only snapshot history ONLY, whether every eligible ticker's close_time
was stable, and fails closed to human review when it was not.

A revision exists **only** when the same exact ticker carries more than one
distinct non-null close_time across its snapshots. Duplicate snapshots
repeating the same close_time are NOT revisions. The guard never chooses
between revised values -- selecting the earliest or latest close for a frozen
experiment is a scientific decision a human must make.

READ-ONLY and pure: this module never touches outcome, pricing, or model
fields of any kind; it inspects (ticker, snapshot id, observed_at,
close_time, environment) metadata and nothing else.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# Statuses (frozen vocabulary).
PASS = "PASS"
PASS_WITH_NULLS = "PASS_WITH_NULLS"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
BLOCKED_INSUFFICIENT_HISTORY = "BLOCKED_INSUFFICIENT_HISTORY"

#: Environments with unambiguous provenance (ADR 0013).
DEFAULT_ALLOWED_ENVIRONMENTS = ("production",)


class GuardInputError(ValueError):
    """Malformed guard input -- fail closed."""


@dataclass(frozen=True)
class SnapshotLite:
    """The only metadata the guard may see (never outcome or pricing fields)."""

    ticker: str
    snapshot_id: int
    observed_at: datetime  # UTC (naive accepted as naive-UTC, normalized)
    close_time: datetime | None  # UTC
    environment: str | None


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


@dataclass(frozen=True)
class CloseTimeVersion:
    """One distinct close_time value and the earliest snapshot carrying it."""

    close_time: datetime
    first_snapshot_id: int
    first_observed_at: datetime


@dataclass(frozen=True)
class TickerRevision:
    ticker: str
    versions: tuple[CloseTimeVersion, ...]  # ordered by first observation
    max_shift_seconds: float
    direction: str  # forward | backward | mixed
    decision_time_changes: tuple[tuple[str, str], ...]  # (old, new) derived pairs


@dataclass
class GuardReport:
    name: str
    as_of: datetime
    horizon_hours: float
    status: str = PASS
    tickers_inspected: int = 0
    tickers_stable: int = 0
    tickers_revised: int = 0
    tickers_null_close: int = 0
    tickers_blocked: int = 0
    revisions: list[TickerRevision] = field(default_factory=list)
    null_tickers: list[str] = field(default_factory=list)
    blocked_tickers: list[str] = field(default_factory=list)
    max_revision_seconds: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": "R013-close-time-stability",
            "name": self.name,
            "as_of": self.as_of.isoformat(),
            "horizon_hours": self.horizon_hours,
            "status": self.status,
            "tickers_inspected": self.tickers_inspected,
            "tickers_stable": self.tickers_stable,
            "tickers_revised": self.tickers_revised,
            "tickers_null_close": self.tickers_null_close,
            "tickers_blocked": self.tickers_blocked,
            "max_revision_seconds": self.max_revision_seconds,
            "revisions": [
                {
                    "ticker": r.ticker,
                    "versions": [
                        {
                            "close_time": v.close_time.isoformat(),
                            "first_snapshot_id": v.first_snapshot_id,
                            "first_observed_at": v.first_observed_at.isoformat(),
                        }
                        for v in r.versions
                    ],
                    "max_shift_seconds": r.max_shift_seconds,
                    "direction": r.direction,
                    "decision_time_changes": [list(p) for p in r.decision_time_changes],
                }
                for r in self.revisions
            ],
            "null_tickers": self.null_tickers[:20],
            "blocked_tickers": self.blocked_tickers[:20],
            "detail": self.detail,
        }


def analyze_close_times(
    snapshots: Sequence[SnapshotLite],
    *,
    name: str,
    as_of: datetime,
    horizon_hours: float,
    nulls_excluded_by_frozen_rules: bool = True,
    allowed_environments: Sequence[str] = DEFAULT_ALLOWED_ENVIRONMENTS,
    expected_tickers: Sequence[str] | None = None,
    eligible: Callable[[str], bool] | None = None,
) -> GuardReport:
    """Pure analysis of append-only snapshot metadata.

    - deterministic ordering: (observed_at, snapshot_id) -- same-timestamp
      snapshots resolve by primary key;
    - environment filtering: rows outside ``allowed_environments`` are
      ignored; a ticker whose rows are ALL ambiguous is BLOCKED (its
      stability cannot be established from unambiguous provenance);
    - ``expected_tickers``: scope reconstruction check -- an expected ticker
      with no history at all is BLOCKED (never silently skipped).
    """
    report = GuardReport(name=name, as_of=_utc(as_of), horizon_hours=horizon_hours)

    by_ticker: dict[str, list[SnapshotLite]] = defaultdict(list)
    ambiguous_only: dict[str, bool] = {}
    for s in snapshots:
        if not s.ticker:
            raise GuardInputError("snapshot with empty ticker")
        if eligible is not None and not eligible(s.ticker):
            continue
        if s.environment in allowed_environments:
            by_ticker[s.ticker].append(s)
            ambiguous_only[s.ticker] = False
        else:
            ambiguous_only.setdefault(s.ticker, True)

    for ticker, is_ambiguous in sorted(ambiguous_only.items()):
        if is_ambiguous:
            report.blocked_tickers.append(ticker)
    if expected_tickers is not None:
        seen = set(by_ticker) | {t for t, a in ambiguous_only.items() if a}
        for t in sorted(set(expected_tickers) - seen):
            report.blocked_tickers.append(t)
    report.tickers_blocked = len(report.blocked_tickers)

    for ticker in sorted(by_ticker):
        rows = sorted(by_ticker[ticker], key=lambda s: (_utc(s.observed_at), s.snapshot_id))
        report.tickers_inspected += 1
        versions: list[CloseTimeVersion] = []
        seen_values: set[datetime] = set()
        for s in rows:
            if s.close_time is None:
                continue
            ct = _utc(s.close_time)
            if ct in seen_values:
                continue  # duplicate snapshot of the same close_time: NOT a revision
            seen_values.add(ct)
            versions.append(
                CloseTimeVersion(
                    close_time=ct,
                    first_snapshot_id=s.snapshot_id,
                    first_observed_at=_utc(s.observed_at),
                )
            )
        if not versions:
            report.tickers_null_close += 1
            report.null_tickers.append(ticker)
            continue
        if len(versions) == 1:
            report.tickers_stable += 1
            continue
        # a genuine revision
        report.tickers_revised += 1
        shifts = [
            (b.close_time - a.close_time).total_seconds()
            for a, b in itertools.pairwise(versions)
        ]
        direction = (
            "forward"
            if all(s > 0 for s in shifts)
            else "backward"
            if all(s < 0 for s in shifts)
            else "mixed"
        )
        max_shift = max(abs(s) for s in shifts)
        report.max_revision_seconds = max(report.max_revision_seconds, max_shift)
        from datetime import timedelta

        horizon = timedelta(hours=horizon_hours)
        decision_changes = tuple(
            ((a.close_time - horizon).isoformat(), (b.close_time - horizon).isoformat())
            for a, b in itertools.pairwise(versions)
        )
        report.revisions.append(
            TickerRevision(
                ticker=ticker,
                versions=tuple(versions),
                max_shift_seconds=max_shift,
                direction=direction,
                decision_time_changes=decision_changes,
            )
        )

    # --- status policy (conservative; never auto-selects a close_time) ------
    if report.tickers_blocked > 0:
        report.status = BLOCKED_INSUFFICIENT_HISTORY
    elif report.tickers_revised > 0:
        report.status = REVIEW_REQUIRED
    elif report.tickers_null_close > 0:
        report.status = (
            PASS_WITH_NULLS if nulls_excluded_by_frozen_rules else REVIEW_REQUIRED
        )
    else:
        report.status = PASS
    report.detail["policy"] = (
        "revisions and unexplained nulls require a separate human scientific "
        "decision; the guard never selects a close_time"
    )
    return report
