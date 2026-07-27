"""H0020 registration: the frozen revision rule, windows, extension rule,
readiness gates, ledger bootstrap, and H0018/H0019 immutability.

Synthetic fixtures only -- no database, no production data, no metrics.
"""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from kalshi_weather.experiments import h0020
from kalshi_weather.experiments.h0020 import (
    ForecastRow,
    H0020SpecError,
    ReadinessCounts,
    failing_gates,
    registered_test_end,
    rev_dir,
    select_revision_pair,
    threshold_side_sign,
    window_for_target_date,
)
from kalshi_weather.research.ledger import read_ledger

REPO = Path(__file__).resolve().parents[2]
EXP = REPO / "docs" / "research" / "experiments"
CONFIG = json.loads((EXP / "EXP-FUTURE-H0020" / "config.json").read_text())

DECISION = datetime(2026, 9, 10, 15, 0, tzinfo=UTC)


def row(rid: int, issue_h: float, obs_h: float, val: float) -> ForecastRow:
    """Row with issue/observed given as hours BEFORE the decision time."""
    return ForecastRow(
        row_id=rid,
        issue_time=DECISION - timedelta(hours=issue_h),
        observed_at=DECISION - timedelta(hours=obs_h),
        point_estimate=val,
    )


# --- availability: observed_at, never issue_time ----------------------------


def test_observed_at_is_the_availability_timestamp() -> None:
    # Issued before the decision but ingested AFTER it: must be invisible.
    ingested_late = ForecastRow(
        row_id=9,
        issue_time=DECISION - timedelta(hours=1),
        observed_at=DECISION + timedelta(minutes=5),
        point_estimate=99.0,
    )
    early = row(1, issue_h=6, obs_h=5, val=90.0)
    prior = row(2, issue_h=12, obs_h=11, val=88.0)
    res = select_revision_pair([ingested_late, early, prior], DECISION)
    assert res.exclusion_reason is None
    assert res.latest is early  # the late-ingested row was never a candidate
    assert res.revision == pytest.approx(2.0)


def test_issue_time_cannot_substitute_for_availability() -> None:
    # Only rows ingested after the decision exist: issue_time alone would
    # (wrongly) admit them; the frozen rule excludes the market row instead.
    res = select_revision_pair(
        [
            ForecastRow(1, DECISION - timedelta(hours=8), DECISION + timedelta(hours=1), 90.0),
            ForecastRow(2, DECISION - timedelta(hours=4), DECISION + timedelta(hours=2), 92.0),
        ],
        DECISION,
    )
    assert res.exclusion_reason == h0020.REASON_NO_FORECAST
    # and the config freezes the same rule
    assert CONFIG["availability"]["forecast_availability_timestamp"] == "observed_at"
    assert "FORBIDDEN" in CONFIG["availability"]["issue_time_as_availability"]


# --- separation, staleness, missing prior, unchanged reissue ----------------


def test_minimum_one_hour_separation_excludes_near_simultaneous_prior() -> None:
    latest = row(1, issue_h=2, obs_h=1.5, val=91.0)
    too_close = row(2, issue_h=2.5, obs_h=2.2, val=90.0)  # 30min before latest
    res = select_revision_pair([latest, too_close], DECISION)
    assert res.exclusion_reason == h0020.REASON_NO_PRIOR


def test_maximum_thirty_hour_separation_excludes_ancient_prior() -> None:
    latest = row(1, issue_h=2, obs_h=1.5, val=91.0)
    ancient = row(2, issue_h=2 + 31, obs_h=32, val=85.0)  # 31h before latest issue
    res = select_revision_pair([latest, ancient], DECISION)
    assert res.exclusion_reason == h0020.REASON_NO_PRIOR
    # exactly 30h separation is still eligible (closed bound)
    boundary = row(3, issue_h=2 + 30, obs_h=31, val=86.0)
    ok = select_revision_pair([latest, boundary], DECISION)
    assert ok.exclusion_reason is None and ok.revision == pytest.approx(5.0)


def test_stale_latest_is_excluded() -> None:
    stale = row(1, issue_h=35, obs_h=31, val=90.0)  # observed 31h before decision
    prior = row(2, issue_h=40, obs_h=39, val=88.0)
    res = select_revision_pair([stale, prior], DECISION)
    assert res.exclusion_reason == h0020.REASON_STALE_LATEST


def test_missing_prior_is_excluded_never_imputed_zero() -> None:
    res = select_revision_pair([row(1, issue_h=2, obs_h=1, val=90.0)], DECISION)
    assert res.exclusion_reason == h0020.REASON_NO_PRIOR
    assert res.revision is None  # not 0.0


def test_unchanged_reissue_yields_explicit_zero_revision() -> None:
    latest = row(1, issue_h=2, obs_h=1, val=90.0)
    prior = row(2, issue_h=5, obs_h=4, val=90.0)  # same value, distinct issue
    res = select_revision_pair([latest, prior], DECISION)
    assert res.exclusion_reason is None
    assert res.revision == 0.0  # control stratum, retained


def test_deterministic_tie_breaks_corrected_row_and_max_id() -> None:
    # corrected same-issue: latest observed_at wins
    a = ForecastRow(1, DECISION - timedelta(hours=2), DECISION - timedelta(hours=1.5), 90.0)
    b = ForecastRow(2, DECISION - timedelta(hours=2), DECISION - timedelta(hours=1.0), 91.0)
    prior = row(3, issue_h=6, obs_h=5, val=88.0)
    res = select_revision_pair([a, b, prior], DECISION)
    assert res.latest is b and res.revision == pytest.approx(3.0)
    # exact tie on both timestamps: max(id) wins
    c = ForecastRow(7, b.issue_time, b.observed_at, 92.0)
    res2 = select_revision_pair([a, b, c, prior], DECISION)
    assert res2.latest is c


# --- sign convention --------------------------------------------------------


def test_threshold_side_sign_convention() -> None:
    assert threshold_side_sign(yes_pays_above=True) == 1
    assert threshold_side_sign(yes_pays_above=False) == -1
    # warming revision helps YES on an above-threshold market...
    assert rev_dir(2.0, yes_pays_above=True) == pytest.approx(2.0)
    # ...and hurts YES on a below-threshold market
    assert rev_dir(2.0, yes_pays_above=False) == pytest.approx(-2.0)
    assert rev_dir(-3.0, yes_pays_above=False) == pytest.approx(3.0)


# --- windows, excluded gap, extension rule ----------------------------------


def test_registered_window_boundaries() -> None:
    assert window_for_target_date(date(2026, 7, 21)) == "train"
    assert window_for_target_date(date(2026, 8, 11)) == "train"
    assert window_for_target_date(date(2026, 8, 26)) == "validation"
    assert window_for_target_date(date(2026, 9, 8)) == "validation"
    assert window_for_target_date(date(2026, 9, 9)) == "test"
    assert window_for_target_date(date(2026, 9, 22)) == "test"
    assert window_for_target_date(date(2026, 7, 20)) == "out_of_scope"
    assert window_for_target_date(date(2026, 10, 7)) == "out_of_scope"
    # config agrees
    assert CONFIG["windows"]["train"] == ["2026-07-21", "2026-08-11"]
    assert CONFIG["windows"]["test_initial"] == ["2026-09-09", "2026-09-22"]


def test_h0019_test_gap_is_excluded_from_every_split() -> None:
    for d in (date(2026, 8, 12), date(2026, 8, 20), date(2026, 8, 25)):
        assert window_for_target_date(d) == "excluded_gap"
    assert CONFIG["windows"]["excluded_gap_h0019_test"] == ["2026-08-12", "2026-08-25"]


def test_group_isolation_no_date_in_two_windows() -> None:
    # every date across the registered span maps to exactly one window state
    d = date(2026, 7, 15)
    seen = []
    while d <= date(2026, 10, 10):
        seen.append(window_for_target_date(d))
        d += timedelta(days=1)
    assert set(seen) <= {"train", "excluded_gap", "validation", "test", "out_of_scope"}
    # windows are contiguous, ordered, non-overlapping
    order = [s for s in seen if s != "out_of_scope"]
    boundaries = [s for i, s in enumerate(order) if i == 0 or order[i - 1] != s]
    assert boundaries == ["train", "excluded_gap", "validation", "test"]


def test_extension_rule_deterministic_and_capped() -> None:
    assert registered_test_end(0) == date(2026, 9, 22)
    assert registered_test_end(1) == date(2026, 9, 29)
    assert registered_test_end(2) == date(2026, 10, 6)  # absolute end
    with pytest.raises(H0020SpecError):
        registered_test_end(3)
    with pytest.raises(H0020SpecError):
        registered_test_end(-1)
    assert CONFIG["test_extension_rule"]["absolute_final_end"] == "2026-10-06"
    assert CONFIG["test_extension_rule"]["max_extensions"] == 2


# --- readiness gates: counts only -------------------------------------------


def ready_counts(**overrides: object) -> ReadinessCounts:
    base: dict[str, object] = {
        "test_window_elapsed": True,
        "test_event_groups": 60,
        "stations_in_test": 4,
        "pos_labels_test": 20,
        "neg_labels_test": 40,
        "station_concentration": 0.30,
        "bootstrap_units": 60,
        "hashes_valid": True,
        "no_group_leakage": True,
        "availability_uses_observed_at": True,
        "exclusions_reported_by_split": True,
    }
    base.update(overrides)
    return ReadinessCounts(**base)  # type: ignore[arg-type]


def test_readiness_gates_pass_and_fail_named() -> None:
    assert failing_gates(ready_counts()) == []
    assert failing_gates(ready_counts(test_event_groups=24)) == ["min_test_event_groups"]
    assert failing_gates(ready_counts(availability_uses_observed_at=False)) == [
        "availability_uses_observed_at"
    ]
    assert "station_concentration" in failing_gates(ready_counts(station_concentration=0.41))
    assert "test_window_elapsed" in failing_gates(ready_counts(test_window_elapsed=False))


def test_readiness_inputs_carry_no_predictive_metric() -> None:
    # the readiness contract is counts/flags only -- no field can hold a
    # Brier score, log loss, probability, or prediction
    fields = set(ReadinessCounts.__dataclass_fields__)
    banned = {"brier", "log_loss", "prediction", "probability", "auc", "pnl", "edge"}
    assert not {f for f in fields for b in banned if b in f.lower()}
    # and the spec module never imports the metrics machinery
    src = (REPO / "src/kalshi_weather/experiments/h0020.py").read_text()
    for token in ("baseline", "brier", "numpy", "np.", "polars"):
        assert token not in src, f"spec module unexpectedly references {token!r}"


# --- frozen config: single primary comparison, success rule, seed -----------


def test_single_primary_comparison_and_frozen_constants() -> None:
    assert CONFIG["model_spec"]["seed"] == 20260909 == h0020.SEED
    assert CONFIG["model_spec"]["l2"] == 1.0 == h0020.L2
    assert CONFIG["model_spec"]["clip"] == [0.02, 0.98] == list(h0020.CLIP)
    assert CONFIG["model_spec"]["n_boot"] == 2000 == h0020.N_BOOT
    assert CONFIG["model_spec"]["features"] == ["logit_market_probability", "rev_dir"]
    assert "exactly one primary comparison" in CONFIG["model_spec"]["primary_comparison"]


def test_frozen_success_rule() -> None:
    rule = CONFIG["success_rule"]
    for fragment in (
        "readiness gates passed before test execution",
        "95% grouped-bootstrap CI entirely below 0",
        "no station has a positive mean Brier difference",
    ):
        assert fragment in rule["SUPPORTED"]
    assert "final" in rule["NOT_SUPPORTED"]
    assert "issue_time used as availability" in rule["INVALID"]
    assert "does not authorize paper or live trading" in rule["on_supported"]


# --- ledger: bootstrap marking, chain, overlap ------------------------------


def latest_by_id() -> dict[str, object]:
    records = read_ledger(REPO / "docs" / "research" / "ledger.jsonl")  # chain-verifies
    latest: dict[str, object] = {}
    for r in records:
        latest[r.hypothesis_id] = r  # later records supersede earlier ones
    return latest


def test_ledger_backfills_marked_and_h0020_native() -> None:
    records = read_ledger(REPO / "docs" / "research" / "ledger.jsonl")  # chain-verifies
    assert [r.hypothesis_id for r in records] == ["H0018", "H0019", "H0020", "H0020"]
    h18, h19, h20_stale, h20 = records
    assert h18.source == "HYPOTHESES.md backfill" and h18.test_data_accessed
    assert h19.source == "HYPOTHESES.md backfill" and not h19.test_data_accessed
    assert h19.test_window == ("2026-08-12", "2026-08-25")
    # seq 4 supersedes seq 3 (pre-commit hash correction); design fields identical
    assert h20.supersedes_sequence == h20_stale.sequence == 3
    assert h20.test_window == h20_stale.test_window
    assert h20.success_rule == h20_stale.success_rule
    assert h20.source is None and not h20.test_data_accessed  # native registration
    assert h20.status == "REGISTERED_NOT_READY"
    assert h20.test_window == ("2026-09-09", "2026-09-22")


def test_h0020_test_window_overlaps_no_recorded_window() -> None:
    latest = latest_by_id()
    h20 = latest["H0020"].test_window  # type: ignore[attr-defined]
    assert h20 is not None
    for hid in ("H0018", "H0019"):
        other = latest[hid].test_window  # type: ignore[attr-defined]
        assert other is not None
        assert not (h20[0] <= other[1] and other[0] <= h20[1]), f"overlap with {hid}"


# --- H0018/H0019 immutability ------------------------------------------------


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_h0019_frozen_files_unchanged() -> None:
    assert (
        _sha(EXP / "EXP-FUTURE-H0019" / "config.json")
        == "5d1f763dcf6b78eabe25add8226f2d2d4f814cf6b191be5dca554a29f840ab0e"
    )
    assert (
        _sha(EXP / "EXP-FUTURE-H0019" / "registration.md")
        == "e8e1200ac3b9d2944f324881d5c8ea755b6da5c4af69fbd2f557d680e78907b5"
    )


def test_h0018_frozen_results_unchanged() -> None:
    assert (
        _sha(EXP / "EXP-20260726-H0018" / "results.json")
        == "b691849e2ce1d5e1cede8a4c9f876c565d0f0bad33503059fdcc40a35d2b6c44"
    )


def test_ledger_records_pin_the_same_frozen_hashes() -> None:
    latest = latest_by_id()
    h19 = dict(latest["H0019"].artifact_hashes)  # type: ignore[attr-defined]
    assert h19["config.json"].startswith("5d1f763dcf6b")
    assert h19["registration.md"].startswith("e8e1200ac3b9")
    h20 = dict(latest["H0020"].artifact_hashes)  # type: ignore[attr-defined]
    # H0020's own frozen artifacts hash-match the working tree
    assert h20["config.json"] == _sha(EXP / "EXP-FUTURE-H0020" / "config.json")
    assert h20["registration.md"] == _sha(EXP / "EXP-FUTURE-H0020" / "registration.md")
    assert h20["h0020.py"] == _sha(REPO / "src/kalshi_weather/experiments/h0020.py")
