"""Tests for scripts/exp_h0015_late_max_mechanism.py.

Implements PREREG-20260722-H0015-late-max-mechanism.md Sec 11's
checklist. All data is synthetic; nothing touches the pinned inputs or
computes a real stratum statistic.
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
import exp_h0015_late_max_mechanism as h0015

# --- Synthetic input builders ------------------------------------------------

CUTOFFS = {"CHI": 16, "DEN": 6, "LAX": 17, "NYC": 16}


def _build_inputs(
    *,
    n_days: int = 400,
    late_fraction: dict[str, float] | None = None,
    p_late: dict[str, float] | None = None,
    p_early: dict[str, float] | None = None,
    start: date = date(2023, 1, 1),
    lax_asof_noise: int = 0,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Issuance + extract frames for all four stations with controllable
    per-station late fractions and conditional revision rates
    (deterministic assignment via day-index modulo -- no randomness)."""
    late_fraction = late_fraction or {"CHI": 0.3, "DEN": 0.9, "LAX": 0.2, "NYC": 0.3}
    p_late = p_late or {"CHI": 0.5, "DEN": 0.4, "LAX": 0.5, "NYC": 0.5}
    p_early = p_early or {"CHI": 0.05, "DEN": 0.05, "LAX": 0.05, "NYC": 0.05}

    issuance_rows: list[dict[str, Any]] = []
    extract_rows: list[dict[str, Any]] = []
    payload = 0
    for station in ("CHI", "DEN", "LAX", "NYC"):
        lf = late_fraction[station]
        stratum_seen = {"late": 0, "early": 0}
        for i in range(n_days):
            d = start + timedelta(days=i)
            is_late_day = (i % 100) < int(lf * 100)
            stratum = "late" if is_late_day else "early"
            conv = p_late[station] if is_late_day else p_early[station]
            # per-stratum Bresenham spread: exact conditional rate for any n
            c = int(conv * 100)
            k = stratum_seen[stratum]
            revised = ((k + 1) * c) // 100 > (k * c) // 100
            stratum_seen[stratum] += 1
            base = 60.0
            final_v = base + (1.0 if revised else 0.0)
            first_t = datetime(d.year, d.month, d.day, 21, 35)
            final_t = datetime(d.year, d.month, d.day, 6, 25) + timedelta(days=1)
            payload += 2
            issuance_rows.append(
                {
                    "station_id": station,
                    "variable": "tmax_f",
                    "value": base,
                    "observation_date": d,
                    "issuance_time": first_t,
                    "raw_payload_id": payload - 1,
                }
            )
            issuance_rows.append(
                {
                    "station_id": station,
                    "variable": "tmax_f",
                    "value": final_v,
                    "observation_date": d,
                    "issuance_time": final_t,
                    "raw_payload_id": payload,
                }
            )
            cutoff = CUTOFFS[station]
            occ = cutoff + 1.5 if is_late_day else max(cutoff - 3, 0) + 0.5
            asof = cutoff
            if lax_asof_noise and station == "LAX" and i < lax_asof_noise:
                asof = 15
            extract_rows.append(
                {
                    "station_id": station,
                    "observation_date": d,
                    "n_products": 2,
                    "first_issuance_time": first_t,
                    "final_issuance_time": final_t,
                    "first_asof_hour": asof,
                    "first_corrected": False,
                    "final_max_value": int(final_v),
                    "final_max_occurrence_hour": float(occ),
                    "final_min_value": 40,
                    "final_min_occurrence_hour": 5.0,
                    "final_corrected": False,
                    "first_raw_payload_id": payload - 1,
                    "final_raw_payload_id": payload,
                }
            )
    return pl.DataFrame(issuance_rows), pl.DataFrame(extract_rows)


def _pin_hashes(issuance: pl.DataFrame, extract: pl.DataFrame) -> Any:
    class _Pins:
        def __enter__(self) -> None:
            self.p1 = patch.object(h0015, "ISSUANCE_HASH", h0015.frame_content_hash(issuance))
            self.p2 = patch.object(h0015, "EXTRACT_HASH", h0015.frame_content_hash(extract))
            self.p1.__enter__()
            self.p2.__enter__()

        def __exit__(self, *exc: object) -> None:
            self.p2.__exit__(*exc)
            self.p1.__exit__(*exc)

    return _Pins()


def _write_dirs(issuance: pl.DataFrame, extract: pl.DataFrame, tmp_path: Path) -> tuple[Path, Path]:
    i_dir = tmp_path / "issuance"
    e_dir = tmp_path / "extract"
    i_dir.mkdir()
    e_dir.mkdir()
    issuance.write_parquet(i_dir / "observation_issuances.parquet")
    extract.write_parquet(e_dir / "cli_occurrences.parquet")
    return i_dir, e_dir


# --- Config drift ------------------------------------------------------------


def test_verify_config_matches_prereg_true_and_detects_drift() -> None:
    assert h0015.verify_config_matches_prereg() is True
    with patch.object(h0015, "SEP_STRONG_MARGIN", Decimal("0.05")):
        assert h0015.verify_config_matches_prereg() is False
    with patch.object(h0015, "G4_MIN_STRATUM_N", 10):
        assert h0015.verify_config_matches_prereg() is False


# --- Statistical spot checks -------------------------------------------------


def test_newcombe_reproduces_h0011_published_interval() -> None:
    lower, upper = h0015.newcombe_interval(x1=311, n1=1282, x2=170, n2=1282)
    assert h0015.serialize_decimal(lower) == "0.079970196795"
    assert h0015.serialize_decimal(upper) == "0.139841273431"


def test_wilson_zero_events_at_gate_minimum_is_decidable() -> None:
    """PREREG Sec 8: at n=30, x=0, the upper bound is ~0.114 < 0.15."""
    _, upper = h0015.wilson_interval(0, 30)
    assert h0015.strictly_less(upper, h0015.EARLY_FLOOR_UPPER) is True


# --- Cutoff procedure and late classification (PREREG Sec 2/11) --------------


def test_modal_cutoff_selection_and_late_at_cutoff() -> None:
    issuance, extract = _build_inputs()
    records, cutoffs, _ = h0015.build_day_records(issuance, extract)
    assert cutoffs == CUTOFFS
    rec = next(r for r in records if r.station == "DEN" and r.occurrence_hour is not None)
    at_cutoff = h0015.DayRecord(
        station="DEN",
        observation_date=date(2024, 1, 1),
        revised=True,
        occurrence_hour=Decimal(6),
        value_agrees=True,
        n_products=2,
    )
    assert h0015.is_late(at_cutoff, 6) is True  # at-or-after
    just_before = h0015.DayRecord(
        station="DEN",
        observation_date=date(2024, 1, 1),
        revised=True,
        occurrence_hour=Decimal("5.983333333333"),
        value_agrees=True,
        n_products=2,
    )
    assert h0015.is_late(just_before, 6) is False
    assert rec is not None


def test_modal_cutoff_with_minority_noise_still_modal() -> None:
    issuance, extract = _build_inputs(lax_asof_noise=50)
    _, cutoffs, asof = h0015.build_day_records(issuance, extract)
    assert cutoffs["LAX"] == 17
    assert asof["LAX"]["15"] == 50


# --- Join / strata / value agreement -----------------------------------------


def test_parse_fail_days_excluded_from_strata_and_counted() -> None:
    issuance, extract = _build_inputs(n_days=100)
    extract = extract.with_columns(
        pl.when(
            (pl.col("station_id") == "DEN") & (pl.col("observation_date") < date(2023, 1, 11))
        )
        .then(None)
        .otherwise(pl.col("final_max_occurrence_hour"))
        .alias("final_max_occurrence_hour")
    )
    records, cutoffs, _ = h0015.build_day_records(issuance, extract)
    den = [r for r in records if r.station == "DEN"]
    missing = [r for r in den if r.occurrence_hour is None]
    assert len(missing) > 0
    stats = h0015.station_stats(records, "DEN", cutoffs["DEN"])
    assert stats["n"] == len(den) - len(missing)


def test_value_agreement_detects_mismatch() -> None:
    issuance, extract = _build_inputs(n_days=50)
    extract = extract.with_columns(
        pl.when(
            (pl.col("station_id") == "LAX") & (pl.col("observation_date") == date(2023, 1, 20))
        )
        .then(pl.col("final_max_value") + 5)
        .otherwise(pl.col("final_max_value"))
        .alias("final_max_value")
    )
    records, _, _ = h0015.build_day_records(issuance, extract)
    lax = [r for r in records if r.station == "LAX" and r.value_agrees is False]
    assert len(lax) == 1


# --- AF and Kitagawa identities (PREREG Sec 11) ------------------------------


def test_station_stats_composition_identity_and_af() -> None:
    issuance, extract = _build_inputs()
    records, cutoffs, _ = h0015.build_day_records(issuance, extract)
    st = h0015.station_stats(records, "DEN", cutoffs["DEN"])
    lhs = Decimal(st["L"]) * Decimal(st["late"]["rate"]) + (1 - Decimal(st["L"])) * Decimal(
        st["early"]["rate"]
    )
    assert abs(lhs - Decimal(st["R"])) < Decimal("1E-11")  # exact identity to quantization
    af_expected = (Decimal(st["R"]) - Decimal(st["early"]["rate"])) / Decimal(st["R"])
    assert abs(Decimal(st["af"]) - af_expected) < Decimal("1E-11")


def test_kitagawa_components_sum_to_raw_difference() -> None:
    issuance, extract = _build_inputs()
    records, cutoffs, _ = h0015.build_day_records(issuance, extract)
    stats = {s: h0015.station_stats(records, s, cutoffs[s]) for s in h0015.ALL_STATIONS}
    kit = h0015.kitagawa_pairs(stats)
    for _pair, cell in kit.items():
        total = Decimal(cell["composition_component"]) + Decimal(cell["rate_component"])
        assert abs(total - Decimal(cell["raw_diff"])) < Decimal("1E-11")


# --- Sep states and thresholds (PREREG Sec 5/11) -----------------------------


def _fake_station(
    sep_state: str, early_floor: bool = True, af_pass: bool = True, full: bool | None = None
) -> dict[str, Any]:
    return {
        "sep_state": sep_state,
        "early_floor_pass": early_floor,
        "af_pass": af_pass,
        "full_pass": (
            full if full is not None else (sep_state == "strong" and early_floor and af_pass)
        ),
    }


def test_af_exactly_half_passes_and_sep_touching_thresholds_resolve_unfavorably() -> None:
    assert h0015.at_least(Decimal("0.50"), h0015.AF_MIN) is True  # exactly 0.50 passes
    # touching 0.10 is NOT strong; touching 0 is NOT positive:
    assert h0015.strictly_greater(Decimal("0.10"), h0015.SEP_STRONG_MARGIN) is False
    assert h0015.strictly_greater(Decimal("0"), h0015.ZERO) is False


# --- Rank agreement ----------------------------------------------------------


def _rank_stats(l_vals: dict[str, str], r_vals: dict[str, str]) -> dict[str, dict[str, Any]]:
    return {s: {"L": l_vals[s], "R": r_vals[s]} for s in h0015.ALL_STATIONS}


def test_rank_agreement_exact_match() -> None:
    stats = _rank_stats(
        {"DEN": "0.9", "CHI": "0.3", "NYC": "0.28", "LAX": "0.2"},
        {"DEN": "0.4", "CHI": "0.17", "NYC": "0.16", "LAX": "0.1"},
    )
    rank = h0015.compute_rank_agreement(stats)
    assert rank["agree"] is True
    assert rank["order_by_L"] == ["DEN", "CHI", "NYC", "LAX"]


def test_rank_agreement_mismatch_and_tie_fail_safe() -> None:
    mismatch = _rank_stats(
        {"DEN": "0.9", "CHI": "0.3", "NYC": "0.28", "LAX": "0.2"},
        {"DEN": "0.4", "CHI": "0.16", "NYC": "0.17", "LAX": "0.1"},
    )
    assert h0015.compute_rank_agreement(mismatch)["agree"] is False
    tie = _rank_stats(
        {"DEN": "0.9", "CHI": "0.3", "NYC": "0.3", "LAX": "0.2"},
        {"DEN": "0.4", "CHI": "0.17", "NYC": "0.16", "LAX": "0.1"},
    )
    rank = h0015.compute_rank_agreement(tie)
    assert rank["tie_present"] is True
    assert rank["agree"] is False


# --- Decision table (PREREG Sec 7/11) ----------------------------------------


def _passed_gates() -> h0015.GateOutcome:
    return h0015.GateOutcome(gates={}, all_gates_passed=True, records=[], cutoffs={}, asof_dist={})


def _rank(agree: bool) -> dict[str, Any]:
    return {"order_by_L": [], "order_by_R": [], "tie_present": False, "agree": agree}


def test_decision_blocked() -> None:
    gates = h0015.GateOutcome(
        gates={}, all_gates_passed=False, records=None, cutoffs=None, asof_dist=None
    )
    assert h0015.evaluate(gates, None, None).outcome == "blocked"


def test_decision_D_on_any_inversion_takes_precedence() -> None:
    stats = {"DEN": _fake_station("inverted"), "LAX": _fake_station("strong")}
    verdict = h0015.evaluate(_passed_gates(), stats, _rank(True))
    assert verdict.outcome == "D"
    assert verdict.evaluation_step == "step_1"


def test_decision_A_requires_both_full_pass_and_rank_agreement() -> None:
    stats = {"DEN": _fake_station("strong"), "LAX": _fake_station("strong")}
    assert h0015.evaluate(_passed_gates(), stats, _rank(True)).outcome == "A"
    assert h0015.evaluate(_passed_gates(), stats, _rank(False)).outcome == "B"
    stats2 = {"DEN": _fake_station("strong"), "LAX": _fake_station("strong", early_floor=False)}
    assert h0015.evaluate(_passed_gates(), stats2, _rank(True)).outcome == "B"


def test_decision_B_on_single_positive() -> None:
    stats = {"DEN": _fake_station("positive"), "LAX": _fake_station("straddle")}
    verdict = h0015.evaluate(_passed_gates(), stats, _rank(False))
    assert verdict.outcome == "B"
    assert verdict.evaluation_step == "step_3"


def test_decision_C_when_both_straddle() -> None:
    stats = {"DEN": _fake_station("straddle"), "LAX": _fake_station("straddle")}
    verdict = h0015.evaluate(_passed_gates(), stats, _rank(False))
    assert verdict.outcome == "C"
    assert verdict.evaluation_step == "step_4"


# --- Gates -------------------------------------------------------------------


def test_gates_pass_on_valid_synthetic_inputs() -> None:
    issuance, extract = _build_inputs()
    with _pin_hashes(issuance, extract):
        outcome = h0015.run_gates(issuance, extract)
    assert outcome.all_gates_passed is True
    assert outcome.gates["g3_quality"]["DEN"]["pass"] is True
    assert outcome.gates["g4_counts"]["LAX"]["pass"] is True


def test_g1_hash_tamper_detection_both_inputs() -> None:
    issuance, extract = _build_inputs()
    outcome = h0015.run_gates(issuance, extract)  # real pins, synthetic data
    assert outcome.gates["g1a_issuance_hash_matches"] is False
    assert outcome.gates["g1b_extract_hash_matches"] == h0015.NOT_EVALUATED
    with patch.object(h0015, "ISSUANCE_HASH", h0015.frame_content_hash(issuance)):
        outcome2 = h0015.run_gates(issuance, extract)
    assert outcome2.gates["g1a_issuance_hash_matches"] is True
    assert outcome2.gates["g1b_extract_hash_matches"] is False


def test_g4_fails_on_thin_stratum_with_sentinel_preserved() -> None:
    issuance, extract = _build_inputs(
        late_fraction={"CHI": 0.3, "DEN": 0.98, "LAX": 0.2, "NYC": 0.3}
    )
    with _pin_hashes(issuance, extract):
        outcome = h0015.run_gates(issuance, extract)
    assert outcome.gates["g4_counts"]["DEN"]["early_n"] < h0015.G4_MIN_STRATUM_N
    assert outcome.all_gates_passed is False


# --- End-to-end run() --------------------------------------------------------


def test_run_end_to_end_A_when_mechanism_holds(tmp_path: Path) -> None:
    issuance, extract = _build_inputs(
        late_fraction={"CHI": 0.3, "DEN": 0.85, "LAX": 0.2, "NYC": 0.28},
    )
    i_dir, e_dir = _write_dirs(issuance, extract, tmp_path)
    with _pin_hashes(issuance, extract):
        results = h0015.run(i_dir, e_dir)
    assert results["primary_results"]["decision"]["outcome"] == "A"
    assert results["primary_results"]["rank_agreement"]["agree"] is True
    for s in ("DEN", "LAX"):
        assert results["primary_results"]["stations"][s]["full_pass"] is True
    h0015.validate_manifest(results)


def test_run_end_to_end_C_when_no_separation(tmp_path: Path) -> None:
    issuance, extract = _build_inputs(
        p_late={"CHI": 0.5, "DEN": 0.2, "LAX": 0.2, "NYC": 0.5},
        p_early={"CHI": 0.05, "DEN": 0.2, "LAX": 0.2, "NYC": 0.05},
    )
    i_dir, e_dir = _write_dirs(issuance, extract, tmp_path)
    with _pin_hashes(issuance, extract):
        results = h0015.run(i_dir, e_dir)
    assert results["primary_results"]["decision"]["outcome"] == "C"


def test_run_end_to_end_D_when_inverted(tmp_path: Path) -> None:
    issuance, extract = _build_inputs(
        p_late={"CHI": 0.5, "DEN": 0.1, "LAX": 0.1, "NYC": 0.5},
        p_early={"CHI": 0.05, "DEN": 0.6, "LAX": 0.6, "NYC": 0.05},
    )
    i_dir, e_dir = _write_dirs(issuance, extract, tmp_path)
    with _pin_hashes(issuance, extract):
        results = h0015.run(i_dir, e_dir)
    assert results["primary_results"]["decision"]["outcome"] == "D"


def test_run_twice_byte_identical(tmp_path: Path) -> None:
    issuance, extract = _build_inputs()
    i_dir, e_dir = _write_dirs(issuance, extract, tmp_path)
    with _pin_hashes(issuance, extract):
        first = h0015.run(i_dir, e_dir)
        second = h0015.run(i_dir, e_dir)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_run_blocked_produces_schema_complete_manifest(tmp_path: Path) -> None:
    issuance, extract = _build_inputs()
    i_dir, e_dir = _write_dirs(issuance, extract, tmp_path)
    results = h0015.run(i_dir, e_dir)  # hashes not pinned -> blocked
    assert results["primary_results"]["decision"]["outcome"] == "blocked"
    h0015.validate_manifest(results)


def test_manifest_validation_raises_on_missing_key(tmp_path: Path) -> None:
    issuance, extract = _build_inputs()
    i_dir, e_dir = _write_dirs(issuance, extract, tmp_path)
    with _pin_hashes(issuance, extract):
        results = h0015.run(i_dir, e_dir)
    del results["primary_results"]["rank_agreement"]
    with pytest.raises(h0015.ManifestValidationError, match="rank_agreement"):
        h0015.validate_manifest(results)


def test_render_markdown_contains_decision_and_table(tmp_path: Path) -> None:
    issuance, extract = _build_inputs()
    i_dir, e_dir = _write_dirs(issuance, extract, tmp_path)
    with _pin_hashes(issuance, extract):
        results = h0015.run(i_dir, e_dir)
    md = h0015.render_markdown(results)
    assert "## Decision" in md
    assert "| DEN |" in md
    assert "CHI (ref)" in md
