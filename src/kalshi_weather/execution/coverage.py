"""Coverage gates and execution-confidence classification for historical replay.

A deterministic, reconciled replay can still be a poor representation of real
execution when the source market data is sparse. This module judges the quality
of an exported historical interval BEFORE any order intent is processed, and
classifies per-market and per-order execution confidence. Accounting correctness
(the ledger reconciles) and execution-data quality (the book was fresh, trades
were present, direction was known) are separate concerns -- a fill is never
graded HIGH merely because the ledger balances.

See docs/adr/0018-replay-coverage-gates.md.
"""

from __future__ import annotations

import itertools
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from kalshi_weather.execution.history import HistoricalReplayData


class GateSeverity(StrEnum):
    PASS = "PASS"
    WARNING = "WARNING"
    FAIL = "FAIL"


class ReplayConfidence(StrEnum):
    HIGH_CONFIDENCE = "HIGH_CONFIDENCE"
    LIMITED_CONFIDENCE = "LIMITED_CONFIDENCE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class FillConfidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class ReplayCoveragePolicy:
    """Versioned, explicit coverage thresholds. Every threshold is recorded in
    the run manifest -- none is a hidden code constant."""

    version: str = "coverage-policy-v1"
    #: Built-in preset name (or "custom"). Recorded in every run artifact.
    preset_name: str = "custom"
    # --- global coverage (percent of requested tickers) --------------------
    min_ticker_book_coverage_pct: float = 100.0  # required
    min_ticker_trade_coverage_pct: float = 0.0  # advisory
    min_ticker_settlement_coverage_pct: float = 0.0  # advisory unless fatal below
    missing_settlement_fatal: bool = False
    # --- staleness / gaps (seconds) ----------------------------------------
    max_median_book_age_seconds: float = 300.0  # advisory
    max_individual_book_age_seconds: float = 900.0  # required (freshness at fill)
    max_book_gap_seconds: float = 1800.0  # advisory
    max_trade_gap_seconds: float = 3600.0  # advisory
    # --- per-market minimums -----------------------------------------------
    min_book_snapshots_per_market: int = 1  # required
    min_trades_per_market: int = 0  # advisory
    # --- integrity ---------------------------------------------------------
    max_malformed_rate: float = 0.5  # advisory (malformed / admitted+malformed)
    max_conflicts: int = 0  # required
    required_production_pct: float = 100.0  # required
    # --- strategy requirements ---------------------------------------------
    marketable_requires_book_coverage: bool = True
    passive_requires_trade_coverage: bool = True
    #: strict-marketable disallows passive execution (marks passive intents
    #: unsupported rather than filling them from sparse historical trades).
    disallow_passive: bool = False
    # --- observed-availability gates (Phase 8; applied when a timeline is given) -
    require_availability_continuity: bool = False  # passive-research
    min_observed_pct: float = 0.0  # advisory unless required flag below
    min_observed_pct_required: bool = False
    max_unknown_pct: float = 100.0  # advisory
    max_collector_unavailable_pct: float = 100.0  # advisory
    max_availability_continuity_breaks: int = 1_000_000  # advisory

    def to_manifest(self) -> dict[str, object]:
        return {k: v for k, v in self.__dict__.items()}


# --- strategy-specific presets (Phase 7; versioned, no hidden thresholds) ---


def strict_marketable_preset() -> ReplayCoveragePolicy:
    """Aggressive / marketable-limit replay: strong fresh books, settlement, and
    production provenance; passive execution disabled."""
    return ReplayCoveragePolicy(
        version="coverage-preset-strict-marketable-v1",
        preset_name="strict-marketable",
        min_ticker_book_coverage_pct=100.0,
        min_ticker_settlement_coverage_pct=100.0,
        missing_settlement_fatal=True,
        max_median_book_age_seconds=180.0,
        max_individual_book_age_seconds=600.0,
        max_book_gap_seconds=900.0,
        min_book_snapshots_per_market=3,
        max_conflicts=0,
        required_production_pct=100.0,
        marketable_requires_book_coverage=True,
        passive_requires_trade_coverage=False,
        disallow_passive=True,
    )


def passive_research_preset() -> ReplayCoveragePolicy:
    """Conservative passive-fill research: requires trades, observed-availability
    continuity, settlement, production, and low unknown-interval tolerance."""
    return ReplayCoveragePolicy(
        version="coverage-preset-passive-research-v1",
        preset_name="passive-research",
        min_ticker_book_coverage_pct=100.0,
        min_ticker_trade_coverage_pct=100.0,
        min_trades_per_market=5,
        min_ticker_settlement_coverage_pct=100.0,
        missing_settlement_fatal=True,
        max_individual_book_age_seconds=900.0,
        max_trade_gap_seconds=1800.0,
        max_conflicts=0,
        required_production_pct=100.0,
        passive_requires_trade_coverage=True,
        require_availability_continuity=True,
        min_observed_pct=50.0,
        min_observed_pct_required=True,
        max_unknown_pct=10.0,
        max_collector_unavailable_pct=5.0,
        max_availability_continuity_breaks=2,
    )


def exploratory_preset() -> ReplayCoveragePolicy:
    """LOW-FIDELITY exploratory replay: permits warnings but never overrides the
    fatal provenance or conflict gates. Not for strategy evaluation."""
    return ReplayCoveragePolicy(
        version="coverage-preset-exploratory-v1",
        preset_name="exploratory (LOW FIDELITY -- not for strategy evaluation)",
        min_ticker_book_coverage_pct=1.0,
        max_median_book_age_seconds=100_000.0,
        max_individual_book_age_seconds=100_000.0,
        max_book_gap_seconds=100_000.0,
        max_trade_gap_seconds=100_000.0,
        min_book_snapshots_per_market=1,
        max_conflicts=0,  # fatal gate NOT relaxed
        required_production_pct=100.0,  # fatal gate NOT relaxed
    )


_PRESETS = {
    "strict-marketable": strict_marketable_preset,
    "passive-research": passive_research_preset,
    "exploratory": exploratory_preset,
}


def get_preset(name: str) -> ReplayCoveragePolicy:
    if name not in _PRESETS:
        raise ValueError(f"unknown coverage preset {name!r}; choose from {sorted(_PRESETS)}")
    return _PRESETS[name]()


@dataclass(frozen=True)
class GateResult:
    name: str
    severity: GateSeverity
    actual: float
    threshold: float
    required: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "severity": self.severity.value,
            "actual": self.actual,
            "threshold": self.threshold,
            "required": self.required,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MarketQuality:
    ticker: str
    book_count: int
    trade_count: int
    has_settlement: bool
    median_book_age_seconds: float | None
    max_book_age_seconds: float | None
    max_book_gap_seconds: float | None
    max_trade_gap_seconds: float | None
    malformed_rows: int
    conflicts: int
    production_pct: float
    marketable_eligible: bool
    passive_eligible: bool
    confidence: ReplayConfidence
    reasons: list[str] = field(default_factory=list)
    # --- observed-availability metrics (Phase 8; None when no timeline given) ---
    observed_pct: float | None = None
    likely_observed_pct: float | None = None
    unknown_pct: float | None = None
    collector_unavailable_pct: float | None = None
    max_unknown_gap_seconds: float | None = None
    max_collector_unavailable_gap_seconds: float | None = None
    availability_continuity_breaks: int | None = None
    availability_policy_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "book_count": self.book_count,
            "trade_count": self.trade_count,
            "has_settlement": self.has_settlement,
            "median_book_age_seconds": self.median_book_age_seconds,
            "max_book_age_seconds": self.max_book_age_seconds,
            "max_book_gap_seconds": self.max_book_gap_seconds,
            "max_trade_gap_seconds": self.max_trade_gap_seconds,
            "malformed_rows": self.malformed_rows,
            "conflicts": self.conflicts,
            "production_pct": self.production_pct,
            "marketable_eligible": self.marketable_eligible,
            "passive_eligible": self.passive_eligible,
            "confidence": self.confidence.value,
            "reasons": self.reasons,
            "observed_pct": self.observed_pct,
            "likely_observed_pct": self.likely_observed_pct,
            "unknown_pct": self.unknown_pct,
            "collector_unavailable_pct": self.collector_unavailable_pct,
            "max_unknown_gap_seconds": self.max_unknown_gap_seconds,
            "max_collector_unavailable_gap_seconds": self.max_collector_unavailable_gap_seconds,
            "availability_continuity_breaks": self.availability_continuity_breaks,
            "availability_policy_version": self.availability_policy_version,
        }


@dataclass
class CoverageResults:
    policy: ReplayCoveragePolicy
    verdict: ReplayConfidence
    gates: list[GateResult]
    markets: list[MarketQuality]

    def failed_gates(self) -> list[GateResult]:
        return [g for g in self.gates if g.severity is GateSeverity.FAIL]

    def warning_gates(self) -> list[GateResult]:
        return [g for g in self.gates if g.severity is GateSeverity.WARNING]

    def to_manifest(self) -> dict[str, Any]:
        return {
            "coverage_policy_version": self.policy.version,
            "verdict": self.verdict.value,
            "gates": [g.to_dict() for g in self.gates],
            "markets": [m.to_dict() for m in self.markets],
        }


# --- per-market metrics ----------------------------------------------------


def _gaps(times: list[datetime]) -> list[float]:
    ordered = sorted(times)
    return [(b - a).total_seconds() for a, b in itertools.pairwise(ordered)]


def market_quality(
    data: HistoricalReplayData,
    ticker: str,
    policy: ReplayCoveragePolicy,
    avail: dict[str, Any] | None = None,
) -> MarketQuality:
    books = [b.captured_at for b in data.market_data.order_books if b.ticker == ticker]
    trades = [t.executed_at for t in data.market_data.trades if t.ticker == ticker]
    has_settlement = any(s.ticker == ticker for s in data.settlements)
    pm = data.quality.per_market.get(ticker, {})
    malformed = pm.get("malformed_trades", 0)
    conflicts = pm.get("book_conflicts", 0)
    admitted_nonproduction = pm.get("admitted_nonproduction", 0)

    book_gaps = _gaps(books)
    trade_gaps = _gaps(trades)
    median_book_age = statistics.median(book_gaps) if book_gaps else None
    max_book_age = max(book_gaps) if book_gaps else None
    max_trade_gap = max(trade_gaps) if trade_gaps else None
    admitted = len(books) + len(trades)
    # provenance of the ADMITTED data (correctly-excluded demo/NULL rows do not
    # lower this): 100% under the default production-only policy.
    production_pct = (
        100.0 if admitted == 0 else 100.0 * (admitted - admitted_nonproduction) / admitted
    )

    reasons: list[str] = []
    marketable_eligible = True
    passive_eligible = True

    if len(books) < policy.min_book_snapshots_per_market:
        marketable_eligible = passive_eligible = False
        reasons.append(
            f"only {len(books)} book snapshots (< {policy.min_book_snapshots_per_market})"
        )
    if max_book_age is not None and max_book_age > policy.max_individual_book_age_seconds:
        reasons.append(
            f"max book gap {max_book_age:.0f}s exceeds "
            f"{policy.max_individual_book_age_seconds:.0f}s"
        )
    if policy.passive_requires_trade_coverage and len(trades) < max(
        1, policy.min_trades_per_market
    ):
        passive_eligible = False
        reasons.append("insufficient trade coverage for passive replay")
    if conflicts > policy.max_conflicts:
        marketable_eligible = passive_eligible = False
        reasons.append(f"{conflicts} conflicting records (> {policy.max_conflicts})")
    if production_pct < policy.required_production_pct:
        marketable_eligible = passive_eligible = False
        reasons.append(
            f"production provenance {production_pct:.0f}% (< {policy.required_production_pct:.0f}%)"
        )

    # observed-availability gates (passive-research). When a timeline is present
    # and the policy requires continuity, weak availability makes passive
    # ineligible -- never marketable-ineligible on availability alone.
    if avail is not None and policy.require_availability_continuity:
        obs = float(avail.get("observed_pct", 0.0))
        unk = float(avail.get("unknown_pct", 0.0))
        unavail = float(avail.get("collector_unavailable_pct", 0.0))
        breaks = int(avail.get("continuity_breaks", 0))
        if policy.min_observed_pct_required and obs < policy.min_observed_pct:
            passive_eligible = False
            reasons.append(f"observed {obs:.0f}% (< required {policy.min_observed_pct:.0f}%)")
        if unk > policy.max_unknown_pct:
            passive_eligible = False
            reasons.append(f"unknown-availability {unk:.0f}% (> {policy.max_unknown_pct:.0f}%)")
        if unavail > policy.max_collector_unavailable_pct:
            passive_eligible = False
            reasons.append(
                f"collector-unavailable {unavail:.0f}% "
                f"(> {policy.max_collector_unavailable_pct:.0f}%)"
            )
        if breaks > policy.max_availability_continuity_breaks:
            passive_eligible = False
            reasons.append(
                f"{breaks} continuity breaks (> {policy.max_availability_continuity_breaks})"
            )
    elif avail is None and policy.require_availability_continuity:
        passive_eligible = False
        reasons.append("passive continuity required but no availability timeline supplied")

    # per-market confidence
    if not marketable_eligible and not passive_eligible:
        confidence = ReplayConfidence.INSUFFICIENT_DATA
    elif reasons or (
        median_book_age is not None and median_book_age > policy.max_median_book_age_seconds
    ):
        confidence = ReplayConfidence.LIMITED_CONFIDENCE
        if median_book_age is not None and median_book_age > policy.max_median_book_age_seconds:
            reasons.append(
                f"median book age {median_book_age:.0f}s exceeds "
                f"{policy.max_median_book_age_seconds:.0f}s"
            )
    else:
        confidence = ReplayConfidence.HIGH_CONFIDENCE

    return MarketQuality(
        ticker=ticker,
        book_count=len(books),
        trade_count=len(trades),
        has_settlement=has_settlement,
        median_book_age_seconds=median_book_age,
        max_book_age_seconds=max_book_age,
        max_book_gap_seconds=max(book_gaps) if book_gaps else None,
        max_trade_gap_seconds=max_trade_gap,
        malformed_rows=malformed,
        conflicts=conflicts,
        production_pct=round(production_pct, 2),
        marketable_eligible=marketable_eligible,
        passive_eligible=passive_eligible,
        confidence=confidence,
        reasons=reasons,
        observed_pct=float(avail["observed_pct"]) if avail else None,
        likely_observed_pct=float(avail["likely_observed_pct"]) if avail else None,
        unknown_pct=float(avail["unknown_pct"]) if avail else None,
        collector_unavailable_pct=float(avail["collector_unavailable_pct"]) if avail else None,
        max_unknown_gap_seconds=float(avail["max_unknown_gap_seconds"]) if avail else None,
        max_collector_unavailable_gap_seconds=(
            float(avail["max_collector_unavailable_gap_seconds"]) if avail else None
        ),
        availability_continuity_breaks=int(avail["continuity_breaks"]) if avail else None,
        availability_policy_version=(avail["availability_policy_version"] if avail else None),
    )


# --- global gates + verdict ------------------------------------------------


def _pct(n: int, d: int) -> float:
    return 100.0 * n / d if d else 0.0


def evaluate_coverage(
    data: HistoricalReplayData,
    tickers: tuple[str, ...],
    policy: ReplayCoveragePolicy,
    availability: dict[str, dict[str, Any]] | None = None,
) -> CoverageResults:
    """Judge the coverage of an exported interval. Deterministic: identical data
    + policy (+ availability) -> identical results (markets sorted by ticker)."""
    avail = availability or {}
    markets = sorted(
        (market_quality(data, tk, policy, avail.get(tk)) for tk in tickers),
        key=lambda m: m.ticker,
    )
    n = len(tickers)
    with_books = sum(1 for m in markets if m.book_count > 0)
    with_trades = sum(1 for m in markets if m.trade_count > 0)
    with_settle = sum(1 for m in markets if m.has_settlement)
    total_malformed = sum(m.malformed_rows for m in markets)
    total_admitted = sum(m.book_count + m.trade_count for m in markets)
    malformed_rate = (
        total_malformed / (total_admitted + total_malformed)
        if (total_admitted + total_malformed)
        else 0.0
    )
    total_conflicts = sum(m.conflicts for m in markets)
    min_production = min((m.production_pct for m in markets), default=100.0)
    worst_median_age = max((m.median_book_age_seconds or 0.0 for m in markets), default=0.0)
    worst_book_gap = max((m.max_book_gap_seconds or 0.0 for m in markets), default=0.0)
    worst_trade_gap = max((m.max_trade_gap_seconds or 0.0 for m in markets), default=0.0)

    gates: list[GateResult] = []

    def gate(
        name: str, actual: float, threshold: float, ok: bool, required: bool, reason: str
    ) -> None:
        sev = GateSeverity.PASS if ok else (GateSeverity.FAIL if required else GateSeverity.WARNING)
        gates.append(
            GateResult(
                name,
                sev,
                round(actual, 2),
                round(threshold, 2),
                required,
                reason if not ok else "ok",
            )
        )

    book_pct = _pct(with_books, n)
    gate(
        "ticker_book_coverage_pct",
        book_pct,
        policy.min_ticker_book_coverage_pct,
        book_pct >= policy.min_ticker_book_coverage_pct,
        policy.marketable_requires_book_coverage,
        "insufficient tickers have an order book",
    )
    gate(
        "ticker_trade_coverage_pct",
        _pct(with_trades, n),
        policy.min_ticker_trade_coverage_pct,
        _pct(with_trades, n) >= policy.min_ticker_trade_coverage_pct,
        False,
        "insufficient tickers have trades",
    )
    settle_pct = _pct(with_settle, n)
    gate(
        "ticker_settlement_coverage_pct",
        settle_pct,
        policy.min_ticker_settlement_coverage_pct,
        settle_pct >= policy.min_ticker_settlement_coverage_pct,
        policy.missing_settlement_fatal,
        "insufficient settlement coverage",
    )
    gate(
        "min_book_snapshots_per_market",
        min((m.book_count for m in markets), default=0),
        policy.min_book_snapshots_per_market,
        all(m.book_count >= policy.min_book_snapshots_per_market for m in markets),
        True,
        "a market has too few book snapshots",
    )
    gate(
        "max_individual_book_age_seconds",
        max((m.max_book_age_seconds or 0.0 for m in markets), default=0.0),
        policy.max_individual_book_age_seconds,
        all(
            (m.max_book_age_seconds or 0.0) <= policy.max_individual_book_age_seconds
            for m in markets
        ),
        True,
        "a market's book is staler than the maximum",
    )
    gate(
        "median_book_age_seconds",
        worst_median_age,
        policy.max_median_book_age_seconds,
        worst_median_age <= policy.max_median_book_age_seconds,
        False,
        "median book age too high",
    )
    gate(
        "max_book_gap_seconds",
        worst_book_gap,
        policy.max_book_gap_seconds,
        worst_book_gap <= policy.max_book_gap_seconds,
        False,
        "book gap too large",
    )
    gate(
        "max_trade_gap_seconds",
        worst_trade_gap,
        policy.max_trade_gap_seconds,
        worst_trade_gap <= policy.max_trade_gap_seconds,
        False,
        "trade gap too large",
    )
    gate(
        "malformed_rate",
        malformed_rate,
        policy.max_malformed_rate,
        malformed_rate <= policy.max_malformed_rate,
        False,
        "malformed-row rate too high",
    )
    gate(
        "conflicts",
        total_conflicts,
        policy.max_conflicts,
        total_conflicts <= policy.max_conflicts,
        True,
        "conflicting records present",
    )
    gate(
        "production_pct",
        min_production,
        policy.required_production_pct,
        min_production >= policy.required_production_pct,
        True,
        "non-production data present",
    )

    # observed-availability gates (only when a timeline was supplied and the
    # policy requires continuity, e.g. passive-research).
    if avail and policy.require_availability_continuity:
        worst_obs = min((m.observed_pct or 0.0 for m in markets), default=0.0)
        worst_unknown = max((m.unknown_pct or 0.0 for m in markets), default=0.0)
        worst_unavail = max((m.collector_unavailable_pct or 0.0 for m in markets), default=0.0)
        worst_breaks = max((m.availability_continuity_breaks or 0 for m in markets), default=0)
        gate(
            "min_observed_pct",
            worst_obs,
            policy.min_observed_pct,
            worst_obs >= policy.min_observed_pct,
            policy.min_observed_pct_required,
            "observed-availability coverage too low",
        )
        gate(
            "max_unknown_pct",
            worst_unknown,
            policy.max_unknown_pct,
            worst_unknown <= policy.max_unknown_pct,
            False,
            "too much unknown-availability time",
        )
        gate(
            "max_collector_unavailable_pct",
            worst_unavail,
            policy.max_collector_unavailable_pct,
            worst_unavail <= policy.max_collector_unavailable_pct,
            False,
            "too much collector-unavailable time",
        )
        gate(
            "availability_continuity_breaks",
            worst_breaks,
            policy.max_availability_continuity_breaks,
            worst_breaks <= policy.max_availability_continuity_breaks,
            False,
            "too many observation-continuity breaks",
        )

    if any(g.severity is GateSeverity.FAIL for g in gates):
        verdict = ReplayConfidence.INSUFFICIENT_DATA
    elif any(g.severity is GateSeverity.WARNING for g in gates):
        verdict = ReplayConfidence.LIMITED_CONFIDENCE
    else:
        verdict = ReplayConfidence.HIGH_CONFIDENCE
    return CoverageResults(policy=policy, verdict=verdict, gates=gates, markets=markets)


# --- per-order / per-fill confidence ---------------------------------------


def fill_confidence(
    *,
    fill_mode: str,
    is_passive: bool,
    book_age_seconds: float | None,
    market: MarketQuality | None,
    policy: ReplayCoveragePolicy,
    unsafe_override: bool,
) -> FillConfidence:
    """Grade one fill's execution confidence. Accounting reconciliation is NOT an
    input -- only execution-data quality is."""
    if is_passive:
        # passive fills rest on sparse, direction-inferred historical trades
        return FillConfidence.LOW
    if book_age_seconds is None:
        return FillConfidence.UNSUPPORTED
    if book_age_seconds > policy.max_individual_book_age_seconds:
        return FillConfidence.UNSUPPORTED  # stale beyond policy -> should not have filled
    fresh = book_age_seconds <= policy.max_median_book_age_seconds
    strong_market = market is not None and market.confidence is ReplayConfidence.HIGH_CONFIDENCE
    if fresh and strong_market and not unsafe_override:
        return FillConfidence.HIGH
    return FillConfidence.MEDIUM
