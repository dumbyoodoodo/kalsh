"""H0020 frozen registration spec: forecast-revision underreaction at close-24h.

This module is the executable half of the H0020 registration
(``docs/research/experiments/EXP-FUTURE-H0020/``): the revision-pair
selection rule, directional sign convention, window boundaries, the
deterministic test-extension rule, and the readiness gates -- all frozen at
registration time, before any validation or test data exists.

Everything here is pure and deterministic: no database access, no model
fitting, no metric computation, no wall clock. The final-run experiment
code will consume these functions; changing them after registration (other
than to fix a reproducibility-breaking defect, documented in HYPOTHESES.md
before any test access) invalidates the experiment.

Anti-leakage invariants encoded here:

- **Availability is ``observed_at`` (ingestion), never ``issue_time``.**
  Measured ingestion delays (p50 46m, p99 ~9.6h) make issue_time-as-of
  joins forward-looking. ``select_revision_pair`` only ever compares
  ``observed_at`` against the decision time; ``issue_time`` is used solely
  to order versions and enforce separation *among already-available rows*.
- The excluded gap (H0019's test window) is a first-class window state --
  a target date in it belongs to no H0020 split.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

HYPOTHESIS_ID = "H0020"
SEED = 20260909
L2 = 1.0
CLIP = (0.02, 0.98)
N_BOOT = 2000
HORIZON_HOURS = 24
#: Exactly one primary comparison (multiple-testing family of one).
PRIMARY_COMPARISON = (
    "Brier(MR_market_plus_revision) - Brier(M1_raw_market) on the single "
    "untouched test window, (station, variable, target_date)-grouped "
    "bootstrap, 2000 resamples, 95% CI, seed 20260909"
)

# --- frozen windows (chronological, by station-local target_date) -----------
TRAIN_START = date(2026, 7, 21)
TRAIN_END = date(2026, 8, 11)
EXCLUDED_GAP_START = date(2026, 8, 12)  # H0019's registered test window --
EXCLUDED_GAP_END = date(2026, 8, 25)  # excluded from H0020 entirely.
VAL_START = date(2026, 8, 26)
VAL_END = date(2026, 9, 8)
TEST_START = date(2026, 9, 9)
TEST_END_INITIAL = date(2026, 9, 22)
#: Deterministic extension rule: at most two 7-day extensions, hard cap.
TEST_EXTENSION_DAYS = 7
MAX_TEST_EXTENSIONS = 2
ABSOLUTE_TEST_END = date(2026, 10, 6)

# --- revision-definition constants ------------------------------------------
MIN_PRIOR_SEPARATION = timedelta(minutes=60)
MAX_PRIOR_SEPARATION = timedelta(hours=30)
MAX_LATEST_STALENESS = timedelta(hours=30)

#: Deterministic exclusion reason codes (recorded, never silent).
REASON_NO_FORECAST = "no_forecast_available_at_decision"
REASON_STALE_LATEST = "latest_forecast_stale"
REASON_NO_PRIOR = "no_eligible_prior_forecast"


class H0020SpecError(ValueError):
    """A request outside the frozen H0020 specification."""


def window_for_target_date(target_date: date) -> str:
    """Which frozen window a station-local target date belongs to.

    ``excluded_gap`` is H0019's registered test window: H0020 never trains,
    validates, or tests on it. ``out_of_scope`` dates belong to no split.
    """
    if TRAIN_START <= target_date <= TRAIN_END:
        return "train"
    if EXCLUDED_GAP_START <= target_date <= EXCLUDED_GAP_END:
        return "excluded_gap"
    if VAL_START <= target_date <= VAL_END:
        return "validation"
    if TEST_START <= target_date <= ABSOLUTE_TEST_END:
        return "test"
    return "out_of_scope"


def registered_test_end(n_extensions: int) -> date:
    """Registered test end after ``n_extensions`` weekly extensions.

    0 -> 2026-09-22, 1 -> 2026-09-29, 2 -> 2026-10-06 (absolute cap). More
    than two extensions is outside the registration: the experiment becomes
    DEFERRED_INSUFFICIENT_DATA instead of extending further.
    """
    if not 0 <= n_extensions <= MAX_TEST_EXTENSIONS:
        raise H0020SpecError(
            f"{n_extensions} extensions outside the registered maximum of {MAX_TEST_EXTENSIONS}"
        )
    end = TEST_END_INITIAL + timedelta(days=TEST_EXTENSION_DAYS * n_extensions)
    if end > ABSOLUTE_TEST_END:  # pragma: no cover - arithmetic guard
        raise H0020SpecError(f"extension past absolute end {ABSOLUTE_TEST_END}")
    return end


# --- revision definition ----------------------------------------------------


@dataclass(frozen=True)
class ForecastRow:
    """One stored forecast version for a (station, variable, target-period).

    ``observed_at`` is the ingestion timestamp -- the moment this row became
    knowable to us. ``issue_time`` is NWS's claimed issuance, used only to
    order versions among rows already available at the decision time.
    """

    row_id: int
    issue_time: datetime
    observed_at: datetime
    point_estimate: float


@dataclass(frozen=True)
class RevisionResult:
    """Outcome of revision construction at a decision time: either a
    revision (with its endpoints) or a deterministic exclusion reason."""

    revision: float | None
    latest: ForecastRow | None
    prior: ForecastRow | None
    exclusion_reason: str | None


def _pick(rows: list[ForecastRow]) -> ForecastRow:
    """Deterministic selection: greatest issue_time; corrected same-issue
    rows resolve to the latest observed_at; exact ties to max row id."""
    return max(rows, key=lambda r: (r.issue_time, r.observed_at, r.row_id))


def select_revision_pair(rows: list[ForecastRow], decision_time: datetime) -> RevisionResult:
    """Apply the frozen H0020 revision definition at ``decision_time``.

    revision_24h = F_latest.point_estimate - F_prev.point_estimate
    (degrees Fahrenheit; positive = warming). Missing prior is an exclusion,
    never an imputed zero; a stored unchanged reissue yields an explicit
    zero revision (the control stratum).
    """
    available = [r for r in rows if r.observed_at <= decision_time]
    if not available:
        return RevisionResult(None, None, None, REASON_NO_FORECAST)

    latest = _pick(available)
    if latest.observed_at < decision_time - MAX_LATEST_STALENESS:
        return RevisionResult(None, latest, None, REASON_STALE_LATEST)

    prior_pool = [
        r
        for r in available
        if r.issue_time <= latest.issue_time - MIN_PRIOR_SEPARATION
        and r.issue_time >= latest.issue_time - MAX_PRIOR_SEPARATION
    ]
    if not prior_pool:
        return RevisionResult(None, latest, None, REASON_NO_PRIOR)

    prior = _pick(prior_pool)
    return RevisionResult(latest.point_estimate - prior.point_estimate, latest, prior, None)


def threshold_side_sign(*, yes_pays_above: bool) -> int:
    """+1 when YES pays above the threshold, -1 when YES pays below."""
    return 1 if yes_pays_above else -1


def rev_dir(revision: float, *, yes_pays_above: bool) -> float:
    """The single frozen directional feature: revision x threshold side."""
    return revision * threshold_side_sign(yes_pays_above=yes_pays_above)


# --- readiness gates (counts and integrity only -- never predictive) --------


@dataclass(frozen=True)
class ReadinessCounts:
    """Inputs to the readiness decision. Counts and integrity flags only;
    nothing here is, or derives from, a predictive metric."""

    test_window_elapsed: bool
    test_event_groups: int
    stations_in_test: int
    pos_labels_test: int
    neg_labels_test: int
    station_concentration: float  # max share of test event-groups on one station
    bootstrap_units: int
    hashes_valid: bool
    no_group_leakage: bool
    availability_uses_observed_at: bool
    exclusions_reported_by_split: bool


#: Frozen numeric gates (operational minimums, not a power analysis).
MIN_TEST_EVENT_GROUPS = 25
MIN_STATIONS = 3
MIN_POS_LABELS = 5
MIN_NEG_LABELS = 5
MAX_STATION_CONCENTRATION = 0.40
MIN_BOOTSTRAP_UNITS = 25


def failing_gates(c: ReadinessCounts) -> list[str]:
    """Names of unmet readiness gates; empty list means READY_FOR_FINAL_TEST."""
    failures = []
    if not c.test_window_elapsed:
        failures.append("test_window_elapsed")
    if c.test_event_groups < MIN_TEST_EVENT_GROUPS:
        failures.append("min_test_event_groups")
    if c.stations_in_test < MIN_STATIONS:
        failures.append("min_stations")
    if c.pos_labels_test < MIN_POS_LABELS:
        failures.append("min_pos_labels")
    if c.neg_labels_test < MIN_NEG_LABELS:
        failures.append("min_neg_labels")
    if c.station_concentration > MAX_STATION_CONCENTRATION:
        failures.append("station_concentration")
    if c.bootstrap_units < MIN_BOOTSTRAP_UNITS:
        failures.append("min_bootstrap_units")
    if not c.hashes_valid:
        failures.append("frozen_hashes_valid")
    if not c.no_group_leakage:
        failures.append("no_group_leakage")
    if not c.availability_uses_observed_at:
        failures.append("availability_uses_observed_at")
    if not c.exclusions_reported_by_split:
        failures.append("exclusions_reported_by_split")
    return failures
