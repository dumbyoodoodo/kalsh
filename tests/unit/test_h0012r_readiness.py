"""H0012r counts-only readiness: calendar-gate boundary, fail-closed
integrity precedence, injected-clock determinism, single-pass state vocabulary,
and no-outcome/no-model isolation. Synthetic fixtures only."""

import re
from datetime import date
from pathlib import Path

from kalshi_weather.experiments.h0012r_readiness import (
    CALENDAR_BOUNDARY,
    MIN_VARIABLE_DATES,
    IntegrityFlags,
    ReadinessCounts,
    ReadinessState,
    classify_readiness,
    earliest_ready_estimate,
    integrity_blockers,
)


def counts(n: int = 200, **kw: object) -> ReadinessCounts:
    base = dict(
        variable_dates=n,
        exact_settlement=n,
        bounded_settlement=0,
        stations=1,
        tmax_dates=n // 2,
        tmin_dates=n - n // 2,
        missing_source_data=0,
        target_date_min=date(2026, 5, 15),
        target_date_max=date(2026, 8, 20),
        reserved_window_overlap={"H0019_test": 0, "H0020_validation_test": 0},
        environment_counts={"production": 10},
    )
    base.update(kw)
    return ReadinessCounts(**base)  # type: ignore[arg-type]


def clean_flags(**kw: object) -> IntegrityFlags:
    base = dict(
        spec_recovered=True,
        leakage_status="PASS",
        payout_agreement_ok=True,
        payout_matches=200,
        payout_total=200,
        close_time_status="PASS",
        close_time_revised=0,
        close_time_null=0,
        partition_consistent=True,
        partition_inconsistent_groups=0,
        provenance_ok=True,
    )
    base.update(kw)
    return IntegrityFlags(**base)  # type: ignore[arg-type]


AFTER = date(2026, 8, 19)
BEFORE = date(2026, 8, 18)


def test_authoritative_constants() -> None:
    assert CALENDAR_BOUNDARY.isoformat() == "2026-08-19"
    assert MIN_VARIABLE_DATES == 189


def test_calendar_gated_before_boundary_even_when_counts_met() -> None:
    r = classify_readiness(counts(200), clean_flags(), now=BEFORE)
    assert r.state is ReadinessState.CALENDAR_GATED
    assert "calendar_boundary_not_reached" in r.failing_gates


def test_one_day_before_boundary_is_gated() -> None:
    r = classify_readiness(counts(200), clean_flags(), now=date(2026, 8, 18))
    assert r.state is ReadinessState.CALENDAR_GATED


def test_exact_boundary_with_counts_and_clean_is_ready() -> None:
    r = classify_readiness(counts(189), clean_flags(), now=AFTER)
    assert r.state is ReadinessState.READY
    assert r.failing_gates == []


def test_after_boundary_but_short_counts_is_collecting() -> None:
    r = classify_readiness(counts(188), clean_flags(), now=date(2026, 8, 20))
    assert r.state is ReadinessState.COLLECTING
    assert f"variable_dates<{MIN_VARIABLE_DATES}" in r.failing_gates


def test_never_ready_before_boundary_property() -> None:
    for n in (0, 189, 500):
        r = classify_readiness(counts(n), clean_flags(), now=BEFORE)
        assert r.state is not ReadinessState.READY


def test_integrity_blocks_even_after_boundary_with_counts() -> None:
    for bad in (
        dict(leakage_status="FAIL"),
        dict(payout_agreement_ok=False),
        dict(close_time_status="REVIEW_REQUIRED"),
        dict(close_time_status="BLOCKED_INSUFFICIENT"),
        dict(partition_consistent=False),
        dict(provenance_ok=False),
    ):
        r = classify_readiness(counts(200), clean_flags(**bad), now=AFTER)
        assert r.state is ReadinessState.INTEGRITY_BLOCKED, bad


def test_integrity_blocks_before_boundary_too() -> None:
    r = classify_readiness(counts(50), clean_flags(payout_agreement_ok=False), now=BEFORE)
    assert r.state is ReadinessState.INTEGRITY_BLOCKED


def test_specification_block_has_highest_precedence() -> None:
    r = classify_readiness(counts(200), clean_flags(spec_recovered=False), now=AFTER)
    assert r.state is ReadinessState.SPECIFICATION_BLOCKED


def test_integrity_blockers_enumerates_all() -> None:
    b = integrity_blockers(
        clean_flags(
            leakage_status="FAIL",
            payout_agreement_ok=False,
            close_time_status="REVIEW_REQUIRED",
            partition_consistent=False,
            provenance_ok=False,
        )
    )
    assert set(b) == {
        "leakage_lint_error",
        "payout_agreement_gate_failed",
        "close_time_review_required",
        "partition_inconsistent_strikes",
        "provenance_incomplete",
    }


def test_injected_clock_determinism() -> None:
    a = classify_readiness(counts(189), clean_flags(), now=AFTER).to_dict()
    b = classify_readiness(counts(189), clean_flags(), now=AFTER).to_dict()
    assert a == b


def test_earliest_ready_never_before_boundary() -> None:
    # counts already met, but before boundary -> boundary is the floor
    assert earliest_ready_estimate(300, as_of=BEFORE) == CALENDAR_BOUNDARY
    # short counts, projected accrual beyond boundary
    est = earliest_ready_estimate(100, as_of=date(2026, 8, 1), accrual_per_day=2.0)
    assert est >= CALENDAR_BOUNDARY


def test_no_train_val_test_states_exist() -> None:
    names = {s.value for s in ReadinessState}
    assert names == {
        "SPECIFICATION_BLOCKED",
        "INTEGRITY_BLOCKED",
        "CALENDAR_GATED",
        "COLLECTING",
        "READY",
    }
    assert not any("TRAIN" in n or "VALIDATION" in n or "TEST" in n for n in names)


def test_reserved_window_overlap_is_reported_not_excluded() -> None:
    # overlap present but does not change readiness (labels, not prices)
    c = counts(200, reserved_window_overlap={"H0019_test": 5, "H0020_validation_test": 3})
    r = classify_readiness(c, clean_flags(), now=AFTER)
    assert r.state is ReadinessState.READY
    assert r.to_dict()["counts"]["reserved_window_overlap"] == {
        "H0019_test": 5,
        "H0020_validation_test": 3,
    }


def test_to_dict_has_counts_only_banner_and_no_outcome_fields() -> None:
    d = classify_readiness(counts(189), clean_flags(), now=AFTER).to_dict()
    assert d["banner"] == "COUNTS ONLY — NOT AN EXPERIMENT RESULT"
    blob = str(d).lower()
    for forbidden in ("brier", "p(value_at_close", "correction_rate", "ci95", "log_loss", "edge"):
        assert forbidden not in blob


def test_module_isolation_counts_only() -> None:
    src = (
        Path(__file__).resolve().parents[2]
        / "src/kalshi_weather/experiments/h0012r_readiness.py"
    ).read_text()
    # no fitting/scoring/ledger-write imports; no reserved-frozen artifact edits
    banned = re.compile(
        r"append_record|brier|log_loss|wilson|newcombe|\.fit\(|predict|expiration_value",
        re.IGNORECASE,
    )
    m = banned.search(src)
    assert m is None, f"h0012r_readiness references outcome/model machinery: {m and m.group(0)!r}"
    # no wall clock in the pure module
    assert "utc_now" not in src
    assert re.search(r"datetime\.now|date\.today|utcnow", src) is None
