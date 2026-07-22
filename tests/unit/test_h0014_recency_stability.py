"""Tests for scripts/exp_h0014_recency_stability.py.

Implements PREREG-20260722-H0014-recency-stability.md Sec 11's
implementation-integrity checklist. All data is synthetic; nothing here
touches the real pinned dataset or computes a real cell.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import exp_h0014_recency_stability as h0014

SOURCE_SCHEMA = {
    "station_id": pl.Utf8,
    "variable": pl.Utf8,
    "value": pl.Float64,
    "observation_date": pl.Date,
    "issuance_time": pl.Datetime("us"),
    "raw_payload_id": pl.Int64,
}


def _frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=SOURCE_SCHEMA, orient="row")  # type: ignore[arg-type]


# --- Windows (PREREG Sec 3/11) -----------------------------------------------


def test_sm_window_boundaries() -> None:
    w = h0014.sm_window(2025)
    assert h0014.in_window(date(2025, 1, 1), w) is True
    assert h0014.in_window(date(2025, 7, 21), w) is True
    assert h0014.in_window(date(2025, 7, 22), w) is False
    assert h0014.in_window(date(2024, 12, 31), w) is False


def test_fy_window_boundaries() -> None:
    w = h0014.fy_window(2024)
    assert h0014.in_window(date(2024, 1, 1), w) is True
    assert h0014.in_window(date(2024, 12, 31), w) is True
    assert h0014.in_window(date(2025, 1, 1), w) is False


def test_calendar_day_counts_including_leap_year() -> None:
    assert h0014.calendar_days(h0014.sm_window(2023)) == 202
    assert h0014.calendar_days(h0014.sm_window(2024)) == 203  # leap year
    assert h0014.calendar_days(h0014.sm_window(2025)) == 202
    assert h0014.calendar_days(h0014.sm_window(2026)) == 202
    assert h0014.calendar_days(h0014.fy_window(2024)) == 366


def test_2022_12_31_falls_in_no_window() -> None:
    d = date(2022, 12, 31)
    all_windows = [h0014.sm_window(y) for y in h0014.SM_YEARS] + [
        h0014.fy_window(y) for y in h0014.FY_YEARS
    ]
    assert not any(h0014.in_window(d, w) for w in all_windows)


# --- Frozen z literals (PREREG Sec 5/11) -------------------------------------


def test_decision_z_rederivation_by_bisection() -> None:
    assert h0014.rederive_decision_z() is True


def test_decision_z_rederivation_detects_wrong_literal() -> None:
    with patch.object(h0014, "Z_DECISION", Decimal("2.5")):
        assert h0014.rederive_decision_z() is False


def test_verify_config_matches_prereg_true_and_detects_drift() -> None:
    assert h0014.verify_config_matches_prereg() is True
    with patch.object(h0014, "G3_MIN_CELL_N", 149):
        assert h0014.verify_config_matches_prereg() is False
    with patch.object(h0014, "OI1_CADENCE_THRESHOLD", Decimal("0.20")):
        assert h0014.verify_config_matches_prereg() is False
    with patch.object(h0014, "Z_DECISION", Decimal("1.959963984540054")):
        assert h0014.verify_config_matches_prereg() is False


# --- Statistical methods (PREREG Sec 5/11) -----------------------------------


def test_newcombe_unequal_n_matches_independent_arrangement() -> None:
    """Newcombe at (n1=200, n2=600) against the same formula assembled
    independently from Wilson bounds, at the frozen precision."""
    x1, n1, x2, n2 = 50, 200, 84, 600
    lower, upper = h0014.newcombe_interval(x1=x1, n1=n1, x2=x2, n2=n2)
    with h0014.localcontext() as ctx:
        ctx.prec = h0014.DECIMAL_CONTEXT_PRECISION
        l1, u1 = h0014.wilson_interval(x1, n1)
        l2, u2 = h0014.wilson_interval(x2, n2)
        p1 = Decimal(x1) / Decimal(n1)
        p2 = Decimal(x2) / Decimal(n2)
        exp_lower = (p1 - p2) - ((p1 - l1) ** 2 + (u2 - p2) ** 2).sqrt()
        exp_upper = (p1 - p2) + ((u1 - p1) ** 2 + (p2 - l2) ** 2).sqrt()
    assert abs(lower - exp_lower) < Decimal("1E-30")
    assert abs(upper - exp_upper) < Decimal("1E-30")


def test_newcombe_equal_rates_ci_contains_zero() -> None:
    lower, upper = h0014.newcombe_interval(x1=30, n1=200, x2=90, n2=600)  # both 15%
    assert lower < 0 < upper


def test_newcombe_reproduces_h0011_published_interval_as_spot_check() -> None:
    lower, upper = h0014.newcombe_interval(x1=311, n1=1282, x2=170, n2=1282)
    assert h0014.serialize_decimal(lower) == "0.079970196795"
    assert h0014.serialize_decimal(upper) == "0.139841273431"


def test_decision_ci_is_wider_than_reporting_ci() -> None:
    l95, u95 = h0014.newcombe_interval(x1=50, n1=200, x2=84, n2=600)
    l98, u98 = h0014.newcombe_interval(x1=50, n1=200, x2=84, n2=600, z=h0014.Z_DECISION)
    assert l98 < l95
    assert u98 > u95


# --- Shift events (PREREG Sec 5/11) ------------------------------------------


def _contrast_with_ci(ci: list[str], delta: str = "0.100000000000") -> dict[str, Any]:
    lower, upper = Decimal(ci[0]), Decimal(ci[1])
    shift = h0014.strictly_greater(lower, h0014.ZERO) or h0014.strictly_less(upper, h0014.ZERO)
    if h0014.strictly_greater(Decimal(delta), h0014.ZERO):
        direction = "up"
    elif h0014.strictly_less(Decimal(delta), h0014.ZERO):
        direction = "down"
    else:
        direction = "zero"
    return {
        "delta_hat": delta,
        "ci9875_newcombe": ci,
        "shift_event": shift,
        "direction": direction,
    }


def test_shift_event_fires_on_both_sides_and_not_on_touch() -> None:
    up = _contrast_with_ci(["0.010000000000", "0.200000000000"])
    down = _contrast_with_ci(["-0.200000000000", "-0.010000000000"], delta="-0.100000000000")
    touch_low = _contrast_with_ci(["0.000000000000", "0.200000000000"])
    touch_high = _contrast_with_ci(["-0.200000000000", "0.000000000000"])
    straddle = _contrast_with_ci(["-0.050000000000", "0.050000000000"])
    assert up["shift_event"] is True and up["direction"] == "up"
    assert down["shift_event"] is True and down["direction"] == "down"
    assert touch_low["shift_event"] is False  # touching 0 is NOT a shift
    assert touch_high["shift_event"] is False
    assert straddle["shift_event"] is False


# --- Operational indicators (PREREG Sec 6/11) --------------------------------


def _diag(cadence: str, missing: str, single: str) -> dict[str, Any]:
    return {
        "mean_issuances_per_day": cadence,
        "missing_fraction": missing,
        "single_issuance_fraction": single,
    }


def _diagnostics(chi_2026: dict[str, Any], chi_hist: dict[str, Any]) -> dict[str, Any]:
    return {"CHI": {"sm_2026": chi_2026, "hist": chi_hist}}


def test_oi_exactly_at_threshold_fires() -> None:
    diags = _diagnostics(
        _diag("2.150000000000", "0.010000000000", "0.010000000000"),
        _diag("2.000000000000", "0.010000000000", "0.010000000000"),
    )
    ois = h0014.compute_operational_indicators(diags, ["CHI"])
    assert isinstance(ois, dict)
    assert ois["CHI"]["oi1_cadence"]["fired"] is True  # |diff| == 0.15 exactly -> fires
    assert ois["CHI"]["oi2_missing_fraction"]["fired"] is False
    assert ois["CHI"]["oi3_single_issuance_fraction"]["fired"] is False


def test_oi_just_under_threshold_does_not_fire() -> None:
    diags = _diagnostics(
        _diag("2.149999000000", "0.059000000000", "0.049000000000"),
        _diag("2.000000000000", "0.010000000000", "0.000000000000"),
    )
    ois = h0014.compute_operational_indicators(diags, ["CHI"])
    assert isinstance(ois, dict)
    assert ois["CHI"]["oi1_cadence"]["fired"] is False  # 0.149999 < 0.15
    assert ois["CHI"]["oi2_missing_fraction"]["fired"] is False  # 0.049 < 0.05
    assert ois["CHI"]["oi3_single_issuance_fraction"]["fired"] is False  # 0.049 < 0.05


def test_oi_not_evaluated_when_no_shifted_stations() -> None:
    assert h0014.compute_operational_indicators({}, []) == h0014.NOT_EVALUATED


# --- Decision table (PREREG Sec 7.1/11) --------------------------------------


def _passed_gates() -> h0014.GateOutcome:
    return h0014.GateOutcome(gates={}, all_gates_passed=True, records=[])


def _contrasts(
    chi_tmax: tuple[bool, str] = (False, "zero"),
    chi_tmin: tuple[bool, str] = (False, "zero"),
    nyc_tmax: tuple[bool, str] = (False, "zero"),
    nyc_tmin: tuple[bool, str] = (False, "zero"),
) -> dict[str, dict[str, dict[str, Any]]]:
    def c(spec: tuple[bool, str]) -> dict[str, Any]:
        return {"shift_event": spec[0], "direction": spec[1]}

    return {
        "CHI": {"tmax_f": c(chi_tmax), "tmin_f": c(chi_tmin)},
        "NYC": {"tmax_f": c(nyc_tmax), "tmin_f": c(nyc_tmin)},
    }


def _ois_fired(station: str = "CHI", fired: bool = True) -> dict[str, Any]:
    return {
        station: {
            "oi1_cadence": {"fired": fired},
            "oi2_missing_fraction": {"fired": False},
            "oi3_single_issuance_fraction": {"fired": False},
        }
    }


def test_decision_blocked_when_gates_fail() -> None:
    gates = h0014.GateOutcome(gates={}, all_gates_passed=False, records=None)
    verdict = h0014.evaluate(gates, None, None)
    assert verdict.outcome == "blocked"
    assert verdict.evaluation_step == "step_0"


def test_decision_A_no_shifts() -> None:
    verdict = h0014.evaluate(_passed_gates(), _contrasts(), h0014.NOT_EVALUATED)
    assert verdict.outcome == "A"
    assert verdict.evaluation_step == "step_1"


def test_decision_C_shift_with_fired_oi_at_shifted_station() -> None:
    contrasts = _contrasts(chi_tmax=(True, "up"))
    verdict = h0014.evaluate(_passed_gates(), contrasts, _ois_fired("CHI"))
    assert verdict.outcome == "C"
    assert verdict.evaluation_step == "step_2"
    assert "oi1_cadence" in verdict.reason


def test_decision_C_beats_D_in_evaluation_order() -> None:
    contrasts = _contrasts(chi_tmax=(True, "up"), nyc_tmax=(True, "up"))
    verdict = h0014.evaluate(_passed_gates(), contrasts, _ois_fired("CHI"))
    assert verdict.outcome == "C"  # step 2 is terminal before step 3


def test_decision_oi_fired_at_non_shifted_station_does_not_give_C() -> None:
    contrasts = _contrasts(chi_tmax=(True, "up"))  # NYC has no shift
    operational = _ois_fired("NYC")  # fabricated: OI fired at NYC only
    verdict = h0014.evaluate(_passed_gates(), contrasts, operational)
    assert verdict.outcome == "B"  # not C: the fired OI is not at a shifted station


def test_decision_D_same_variable_same_direction_both_stations() -> None:
    contrasts = _contrasts(chi_tmax=(True, "up"), nyc_tmax=(True, "up"))
    verdict = h0014.evaluate(_passed_gates(), contrasts, _ois_fired("CHI", fired=False))
    assert verdict.outcome == "D"
    assert verdict.evaluation_step == "step_3"


def test_decision_B_single_station_shift() -> None:
    contrasts = _contrasts(chi_tmax=(True, "up"))
    verdict = h0014.evaluate(_passed_gates(), contrasts, _ois_fired("CHI", fired=False))
    assert verdict.outcome == "B"
    assert verdict.evaluation_step == "step_4"


def test_decision_B_different_variables_across_stations() -> None:
    contrasts = _contrasts(chi_tmax=(True, "up"), nyc_tmin=(True, "up"))
    verdict = h0014.evaluate(
        _passed_gates(), contrasts, {**_ois_fired("CHI", False), **_ois_fired("NYC", False)}
    )
    assert verdict.outcome == "B"


def test_decision_B_same_variable_opposite_directions() -> None:
    contrasts = _contrasts(chi_tmax=(True, "up"), nyc_tmax=(True, "down"))
    verdict = h0014.evaluate(
        _passed_gates(), contrasts, {**_ois_fired("CHI", False), **_ois_fired("NYC", False)}
    )
    assert verdict.outcome == "B"


# --- Synthetic frame builder -------------------------------------------------


def _build_synthetic_frame(
    *,
    revision_period: dict[tuple[str, str, str], int] | None = None,
    include_den: bool = True,
    issuances_2026: int = 2,
) -> pl.DataFrame:
    """Days 2023-01-01 .. 2026-07-21 for CHI+NYC (+1 DEN row). Revision
    every `period`-th day, configurable per (station, variable, era) where
    era in {'hist', '2026'}. Finals are next-day 06:25 UTC. `issuances_2026`
    lets tests change the 2026 cadence (extra intermediate issuances)."""
    revision_period = revision_period or {}
    rows: list[dict[str, Any]] = []
    payload = 0
    day = date(2023, 1, 1)
    end = date(2026, 7, 21)
    i = 0
    while day <= end:
        era = "2026" if day.year == 2026 else "hist"
        for station in ("CHI", "NYC"):
            for variable, base in (("tmax_f", 60.0), ("tmin_f", 40.0)):
                period = revision_period.get((station, variable, era), 10)
                revised = i % period == 0
                first_time = datetime(day.year, day.month, day.day, 21, 35)
                final_time = datetime(day.year, day.month, day.day, 6, 25) + timedelta(days=1)
                payload += 1
                rows.append(
                    {
                        "station_id": station,
                        "variable": variable,
                        "value": base,
                        "observation_date": day,
                        "issuance_time": first_time,
                        "raw_payload_id": payload,
                    }
                )
                n_extra = (issuances_2026 - 2) if era == "2026" else 0
                for k in range(n_extra):
                    payload += 1
                    rows.append(
                        {
                            "station_id": station,
                            "variable": variable,
                            "value": base,
                            "observation_date": day,
                            "issuance_time": first_time + timedelta(minutes=30 + k),
                            "raw_payload_id": payload,
                        }
                    )
                payload += 1
                rows.append(
                    {
                        "station_id": station,
                        "variable": variable,
                        "value": base + (1.0 if revised else 0.0),
                        "observation_date": day,
                        "issuance_time": final_time,
                        "raw_payload_id": payload,
                    }
                )
        day += timedelta(days=1)
        i += 1
    if include_den:
        payload += 1
        rows.append(
            {
                "station_id": "DEN",
                "variable": "tmax_f",
                "value": 80.0,
                "observation_date": date(2023, 6, 1),
                "issuance_time": datetime(2023, 6, 1, 21, 0),
                "raw_payload_id": payload,
            }
        )
    return _frame(rows)


def _pin_hash(frame: pl.DataFrame) -> Any:
    return patch.object(h0014, "FROZEN_CONTENT_HASH", h0014.frame_content_hash(frame))


# --- Gates (PREREG Sec 8/11) -------------------------------------------------


def test_gates_pass_on_valid_synthetic_frame() -> None:
    frame = _build_synthetic_frame()
    with _pin_hash(frame):
        outcome = h0014.run_gates(frame)
    assert outcome.all_gates_passed is True
    assert outcome.gates["g1_frame_hash_matches"] is True
    assert all(outcome.gates["g2_invariants"].values())
    for station in h0014.STATIONS:
        for variable in h0014.VARIABLES:
            for year in h0014.SM_YEARS:
                assert outcome.gates["g3_sm_cell_min_n"][station][variable][str(year)] >= 150
            assert outcome.gates["g4_hist_min_r"][station][variable] >= 30


def test_g1_fails_on_hash_mismatch_and_short_circuits() -> None:
    frame = _build_synthetic_frame()
    outcome = h0014.run_gates(frame)  # frozen hash is the real pin, not this frame's
    assert outcome.gates["g1_frame_hash_matches"] is False
    assert outcome.gates["g3_sm_cell_min_n"] == h0014.NOT_EVALUATED
    assert outcome.all_gates_passed is False


def test_filter_drops_den_and_g2_scopes_to_two_stations() -> None:
    frame = _build_synthetic_frame(include_den=True)
    with _pin_hash(frame):
        outcome = h0014.run_gates(frame)
    assert outcome.gates["g2_invariants"]["station_set_exact"] is True


def test_g2_fails_on_duplicate_inside_the_two_stations() -> None:
    frame = _build_synthetic_frame()
    nyc_row = frame.filter(pl.col("station_id") == "NYC").row(0, named=True)
    frame2 = pl.concat([frame, _frame([nyc_row])])
    with _pin_hash(frame2):
        outcome = h0014.run_gates(frame2)
    assert outcome.gates["g2_invariants"]["natural_key_unique"] is False
    assert outcome.all_gates_passed is False


def test_g2_ignores_duplicate_outside_the_two_stations() -> None:
    frame = _build_synthetic_frame(include_den=True)
    den_row = frame.filter(pl.col("station_id") == "DEN").row(0, named=True)
    frame2 = pl.concat([frame, _frame([den_row])])
    with _pin_hash(frame2):
        outcome = h0014.run_gates(frame2)
    assert outcome.gates["g2_invariants"]["natural_key_unique"] is True
    assert outcome.all_gates_passed is True


def test_g4_fails_when_hist_revisions_scarce() -> None:
    # revise every 100th day -> ~6 hist SM revisions, under the 30 floor
    period = {
        (s, v, e): 100
        for s in ("CHI", "NYC")
        for v in ("tmax_f", "tmin_f")
        for e in ("hist", "2026")
    }
    frame = _build_synthetic_frame(revision_period=period)
    with _pin_hash(frame):
        outcome = h0014.run_gates(frame)
    assert outcome.all_gates_passed is False
    assert any(r < 30 for s in h0014.STATIONS for r in outcome.gates["g4_hist_min_r"][s].values())


# --- End-to-end run() (PREREG Sec 10/11) -------------------------------------


def _write_dataset_dir(frame: pl.DataFrame, tmp_path: Path) -> Path:
    frame.write_parquet(tmp_path / "observation_issuances.parquet")
    return tmp_path


def test_run_end_to_end_A_when_2026_matches_history(tmp_path: Path) -> None:
    frame = _build_synthetic_frame()  # identical periods everywhere
    with _pin_hash(frame):
        results = h0014.run(_write_dataset_dir(frame, tmp_path))
    assert results["primary_results"]["decision"]["outcome"] == "A"
    assert results["primary_results"]["shift_sets"] == {"CHI": [], "NYC": []}
    assert results["primary_results"]["operational_indicators"] == h0014.NOT_EVALUATED
    assert results["descriptive_results"]["excluded_days"] == 0  # builder starts 2023-01-01
    h0014.validate_manifest(results)


def test_run_end_to_end_D_when_both_stations_shift_same_direction(tmp_path: Path) -> None:
    period = {
        ("CHI", "tmax_f", "2026"): 2,  # 2026 tmax revises every 2nd day vs 10th historically
        ("NYC", "tmax_f", "2026"): 2,
    }
    frame = _build_synthetic_frame(revision_period=period)
    with _pin_hash(frame):
        results = h0014.run(_write_dataset_dir(frame, tmp_path))
    assert results["primary_results"]["shift_sets"] == {"CHI": ["tmax_f"], "NYC": ["tmax_f"]}
    assert results["primary_results"]["decision"]["outcome"] == "D"
    ois = results["primary_results"]["operational_indicators"]
    assert isinstance(ois, dict)  # evaluated (shifts exist), nothing fired
    assert not any(oi["fired"] for s in ois.values() for oi in s.values())


def test_run_end_to_end_B_when_only_chi_shifts(tmp_path: Path) -> None:
    period = {("CHI", "tmax_f", "2026"): 2}
    frame = _build_synthetic_frame(revision_period=period)
    with _pin_hash(frame):
        results = h0014.run(_write_dataset_dir(frame, tmp_path))
    assert results["primary_results"]["decision"]["outcome"] == "B"


def test_run_end_to_end_C_when_shift_cooccurs_with_cadence_change(tmp_path: Path) -> None:
    period = {("CHI", "tmax_f", "2026"): 2, ("NYC", "tmax_f", "2026"): 2}
    frame = _build_synthetic_frame(revision_period=period, issuances_2026=3)  # cadence 3 vs 2
    with _pin_hash(frame):
        results = h0014.run(_write_dataset_dir(frame, tmp_path))
    assert results["primary_results"]["decision"]["outcome"] == "C"  # step 2 before step 3
    ois = results["primary_results"]["operational_indicators"]
    assert ois["CHI"]["oi1_cadence"]["fired"] is True


def test_run_twice_produces_byte_identical_json(tmp_path: Path) -> None:
    frame = _build_synthetic_frame()
    dataset_dir = _write_dataset_dir(frame, tmp_path)
    with _pin_hash(frame):
        first = h0014.run(dataset_dir)
        second = h0014.run(dataset_dir)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first == second


def test_run_blocked_produces_schema_complete_manifest(tmp_path: Path) -> None:
    frame = _build_synthetic_frame()
    dataset_dir = _write_dataset_dir(frame, tmp_path)
    results = h0014.run(dataset_dir)  # hash not pinned -> G1 fails -> blocked
    assert results["primary_results"]["decision"]["outcome"] == "blocked"
    h0014.validate_manifest(results)


def test_manifest_validation_raises_on_missing_keys(tmp_path: Path) -> None:
    frame = _build_synthetic_frame()
    with _pin_hash(frame):
        results = h0014.run(_write_dataset_dir(frame, tmp_path))
    del results["primary_results"]["decision"]["interpretation"]
    with pytest.raises(h0014.ManifestValidationError, match="decision"):
        h0014.validate_manifest(results)


def test_render_markdown_contains_decision_and_tables(tmp_path: Path) -> None:
    frame = _build_synthetic_frame()
    with _pin_hash(frame):
        results = h0014.run(_write_dataset_dir(frame, tmp_path))
    md = h0014.render_markdown(results)
    assert "## Decision" in md
    assert "**A**" in md
    assert "| station | variable |" in md
    assert "shift_event = False" in md


# --- Diagnostics (PREREG Sec 6/11) -------------------------------------------


def test_station_window_diagnostics_counts_missing_days() -> None:
    frame = _build_synthetic_frame()
    filtered = frame.filter(pl.col("station_id").is_in(h0014.STATIONS))
    records = h0014.build_day_records(filtered)
    # remove ten CHI days from SM(2026) by filtering records
    removed_dates = {date(2026, 3, d) for d in range(1, 11)}
    records = [
        r for r in records if not (r.station == "CHI" and r.observation_date in removed_dates)
    ]
    diags = h0014.station_window_diagnostics(records, "CHI", {"sm_2026": [h0014.sm_window(2026)]})
    assert diags["sm_2026"]["expected_days"] == 202
    assert diags["sm_2026"]["observed_days"] == 192
    assert diags["sm_2026"]["missing_days"] == 10


def test_single_issuance_days_counted_and_unrevised() -> None:
    rows = [
        {
            "station_id": "CHI",
            "variable": "tmax_f",
            "value": 60.0,
            "observation_date": date(2026, 1, 5),
            "issuance_time": datetime(2026, 1, 5, 21, 0),
            "raw_payload_id": 1,
        }
    ]
    records = h0014.build_day_records(_frame(rows))
    assert records[0].n_issuances == 1
    assert records[0].revised is False
    diags = h0014.station_window_diagnostics(records, "CHI", {"sm_2026": [h0014.sm_window(2026)]})
    assert diags["sm_2026"]["single_issuance_days"] == 1
