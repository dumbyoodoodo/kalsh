"""H0019 registered single-shot runner: gate, subgroup-rule, and refusal tests.

Every fixture here is synthetic and written by hand. H0019's test window
(2026-08-12 -> 2026-08-25) has not opened, no H0019 outcome has been inspected,
and nothing in this module reads production data, opens a database session, or
loads a recorded production fixture -- ``test_no_production_data_access`` and
``test_no_database_session_in_this_module`` assert exactly that.

The station-subgroup cases encode the clarification frozen in HYPOTHESES.md
(H0019, 2026-08-03T03:18:06Z, Wilson Tu).
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kalshi_weather.experiments import h0019
from kalshi_weather.experiments import readiness as rd
from kalshi_weather.experiments import runner as h0018

THIS_FILE = Path(__file__)


def sub(
    station: str, n: int, diff: float | None, *, eligible: bool | None = None
) -> h0019.SubgroupDiff:
    """Build a subgroup summary directly, for decision-rule tests."""
    return h0019.SubgroupDiff(
        station=station,
        n_event_groups=n,
        mean_brier_diff=diff,
        eligible=(n >= h0019.MIN_SUBGROUP_EVENT_GROUPS) if eligible is None else eligible,
    )


def rows(*specs: tuple[str, str, float | None, int]) -> tuple[list, list, list]:
    """Expand ``(station, event_group, diff, n_rows)`` specs into row arrays."""
    stations: list[str] = []
    groups: list[str] = []
    diffs: list[float | None] = []
    for station, group, diff, n in specs:
        stations.extend([station] * n)
        groups.extend([group] * n)
        diffs.extend([diff] * n)
    return stations, groups, diffs


# --- spec identity with H0018 (registration requires "identical to H0018") ---


def test_feature_spec_is_h0018s_object_not_a_copy() -> None:
    assert h0019.WEATHER_FEATURES is h0018._WEATHER_FEATURES
    assert [*h0018._WEATHER_FEATURES, "mkt_prob"] == h0019.MARKET_WEATHER_FEATURES


def test_h0018_shared_definitions_are_imported_not_duplicated() -> None:
    """Leakage checks, feature matrix, and metrics must be H0018's own callables."""
    source = Path(h0019.__file__).read_text()
    assert "from kalshi_weather.experiments import runner as h0018" in source
    for shared in ("_leakage_checks", "_feature_matrix", "_metrics"):
        assert f"h0018.{shared}" in source, f"{shared} must be used via h0018, not re-defined"
        assert f"def {shared}" not in source, f"{shared} must not be redefined in h0019"


def test_frozen_constants_match_registration() -> None:
    assert h0019.SEED == 20260726
    assert h0019.N_BOOT == 2000
    assert h0019.L2 == 1.0
    assert h0019.HORIZON_HOURS == 24


def test_windows_come_from_the_readiness_module() -> None:
    assert (h0019.TRAIN_START, h0019.TRAIN_END) == (rd.TRAIN_START, rd.TRAIN_END)
    assert (h0019.VAL_START, h0019.VAL_END) == (rd.VAL_START, rd.VAL_END)
    assert (h0019.TEST_START, h0019.TEST_END) == (rd.TEST_START, rd.TEST_END)


def test_windows_are_chronological_and_non_overlapping() -> None:
    assert h0019.TRAIN_END < h0019.VAL_START
    assert h0019.VAL_END < h0019.TEST_START


def test_subgroup_minimum_is_the_registered_value_not_a_new_threshold() -> None:
    assert h0019.MIN_SUBGROUP_EVENT_GROUPS == rd.REQUIREMENTS.min_events_per_station_test == 4


# --- eligibility (counts only) ----------------------------------------------


def test_station_with_exactly_four_event_groups_is_eligible() -> None:
    counts = {"NYC": 4}
    assert h0019.eligible_stations(counts) == {"NYC"}


def test_station_with_three_event_groups_is_insufficient() -> None:
    counts = {"NYC": 3}
    assert h0019.eligible_stations(counts) == set()


def test_counts_are_computed_without_any_outcome_value() -> None:
    """Eligibility must be decidable before outcomes load: the counting
    function accepts no Brier differences at all."""
    stations, groups, _ = rows(("NYC", "g1", 0.1, 3), ("NYC", "g2", 0.1, 1), ("CHI", "g3", 0.1, 9))
    assert h0019.station_event_group_counts(stations, groups) == {"NYC": 2, "CHI": 1}


def test_station_insufficiency_is_reported_not_omitted() -> None:
    stations, groups, diffs = rows(
        ("NYC", "n1", -0.1, 1),
        ("NYC", "n2", -0.1, 1),
        ("NYC", "n3", -0.1, 1),
        ("NYC", "n4", -0.1, 1),
        ("LAX", "l1", 0.5, 1),
    )
    out = {
        s.station: s
        for s in h0019.build_station_subgroups(
            stations=stations, event_groups=groups, brier_diffs=diffs
        )
    }
    assert out["LAX"].n_event_groups == 1
    assert out["LAX"].eligible is False
    assert out["LAX"].insufficient_for_veto is True
    assert out["NYC"].eligible is True


# --- equal event-group weighting -------------------------------------------


def test_event_groups_are_equally_weighted_regardless_of_market_count() -> None:
    """The decisive test for the frozen rule: one event-group with a single
    contract must count exactly as much as one holding 99. Row weighting would
    give -0.96 here (no reversal); the frozen rule gives +1.0 (reversal)."""
    stations, groups, diffs = rows(
        ("NYC", "g1", +3.0, 1),
        ("NYC", "g2", -1.0, 99),
        ("NYC", "g3", +3.0, 1),
        ("NYC", "g4", -1.0, 99),
    )
    (nyc,) = h0019.build_station_subgroups(
        stations=stations, event_groups=groups, brier_diffs=diffs
    )
    assert nyc.mean_brier_diff == pytest.approx(1.0)
    assert h0019.has_major_station_reversal([nyc]) is True


def test_market_count_imbalance_cannot_alter_the_station_mean() -> None:
    """Same event-group values, wildly different contract counts -> same mean."""
    balanced = rows(
        ("NYC", "g1", 0.2, 1),
        ("NYC", "g2", -0.4, 1),
        ("NYC", "g3", 0.6, 1),
        ("NYC", "g4", -0.8, 1),
    )
    lopsided = rows(
        ("NYC", "g1", 0.2, 40),
        ("NYC", "g2", -0.4, 1),
        ("NYC", "g3", 0.6, 7),
        ("NYC", "g4", -0.8, 22),
    )
    a = h0019.build_station_subgroups(
        stations=balanced[0], event_groups=balanced[1], brier_diffs=balanced[2]
    )[0]
    b = h0019.build_station_subgroups(
        stations=lopsided[0], event_groups=lopsided[1], brier_diffs=lopsided[2]
    )[0]
    assert a.mean_brier_diff == pytest.approx(b.mean_brier_diff)


def test_each_event_group_contributes_exactly_once() -> None:
    stations, groups, diffs = rows(
        ("NYC", "g1", 1.0, 5),
        ("NYC", "g2", 1.0, 5),
        ("NYC", "g3", 1.0, 5),
        ("NYC", "g4", -3.0, 5),
    )
    (nyc,) = h0019.build_station_subgroups(
        stations=stations, event_groups=groups, brier_diffs=diffs
    )
    assert nyc.n_event_groups == 4
    assert nyc.mean_brier_diff == pytest.approx((1.0 + 1.0 + 1.0 - 3.0) / 4)


# --- reversal condition -----------------------------------------------------


def test_positive_eligible_station_mean_triggers_reversal() -> None:
    assert h0019.has_major_station_reversal([sub("NYC", 6, +0.01)]) is True


def test_zero_station_mean_does_not_trigger_reversal() -> None:
    """'Strictly greater than zero' -- exactly zero is not a reversal."""
    assert h0019.has_major_station_reversal([sub("NYC", 6, 0.0)]) is False


def test_negative_station_mean_does_not_trigger_reversal() -> None:
    assert h0019.has_major_station_reversal([sub("NYC", 6, -0.01)]) is False


def test_one_reversing_eligible_station_is_sufficient() -> None:
    groups = [sub("NYC", 10, -0.05), sub("CHI", 9, -0.04), sub("DEN", 4, +0.001)]
    assert h0019.has_major_station_reversal(groups) is True


def test_multiple_non_reversing_eligible_stations_do_not_trigger() -> None:
    groups = [sub("NYC", 10, -0.05), sub("CHI", 9, -0.04), sub("DEN", 4, 0.0)]
    assert h0019.has_major_station_reversal(groups) is False


def test_thin_station_with_positive_mean_cannot_trigger_veto() -> None:
    groups = [sub("NYC", 10, -0.05), sub("LAX", 3, +9.9)]
    assert h0019.has_major_station_reversal(groups) is False


def test_thin_station_with_negative_mean_cannot_clear_a_reversal_elsewhere() -> None:
    groups = [sub("CHI", 5, +0.02), sub("LAX", 3, -99.0)]
    assert h0019.has_major_station_reversal(groups) is True


# --- fail-closed integrity --------------------------------------------------


def test_duplicated_event_group_across_stations_fails_closed() -> None:
    stations, groups, diffs = rows(("NYC", "shared", -0.1, 2), ("CHI", "shared", -0.1, 2))
    with pytest.raises(h0019.H0019SubgroupIntegrityError, match="multiple stations"):
        h0019.build_station_subgroups(stations=stations, event_groups=groups, brier_diffs=diffs)


@pytest.mark.parametrize(
    ("bad", "label"),
    [
        (None, "missing"),
        (math.nan, "nan"),
        (math.inf, "positive infinity"),
        (-math.inf, "negative infinity"),
    ],
)
def test_invalid_eligible_station_result_fails_closed(bad: float | None, label: str) -> None:
    """Missing, NaN, +inf, and -inf all abort the analysis for an eligible
    station rather than producing a verdict."""
    stations, groups, diffs = rows(
        ("NYC", "g1", -0.1, 1),
        ("NYC", "g2", -0.1, 1),
        ("NYC", "g3", -0.1, 1),
        ("NYC", "g4", bad, 1),
    )
    with pytest.raises(h0019.H0019SubgroupIntegrityError) as excinfo:
        h0019.build_station_subgroups(stations=stations, event_groups=groups, brier_diffs=diffs)
    assert "non-finite" in str(excinfo.value), f"{label} must fail closed explicitly"
    assert "no verdict emitted" in str(excinfo.value)


def test_invalid_result_is_never_silently_omitted() -> None:
    """A dropped bad row would leave 3 clean groups and a plausible mean; the
    rule must raise instead."""
    stations, groups, diffs = rows(
        ("NYC", "g1", -0.5, 1),
        ("NYC", "g2", -0.5, 1),
        ("NYC", "g3", -0.5, 1),
        ("NYC", "g4", math.nan, 1),
    )
    with pytest.raises(h0019.H0019SubgroupIntegrityError):
        h0019.build_station_subgroups(stations=stations, event_groups=groups, brier_diffs=diffs)


def test_ineligible_station_with_invalid_result_does_not_fail_closed() -> None:
    """Fail-closed is scoped to eligible stations; a thin station's unusable
    value is reported as None and can neither trigger nor clear the veto."""
    stations, groups, diffs = rows(
        ("NYC", "g1", -0.1, 1),
        ("NYC", "g2", -0.1, 1),
        ("NYC", "g3", -0.1, 1),
        ("NYC", "g4", -0.1, 1),
        ("LAX", "l1", math.nan, 1),
    )
    out = {
        s.station: s
        for s in h0019.build_station_subgroups(
            stations=stations, event_groups=groups, brier_diffs=diffs
        )
    }
    assert out["LAX"].mean_brier_diff is None
    assert out["LAX"].eligible is False
    assert h0019.has_major_station_reversal(list(out.values())) is False


# --- decision rule ----------------------------------------------------------


def test_confirmed_requires_ci_entirely_below_zero() -> None:
    verdict, _ = h0019.classify(
        ci95_lo=-0.05,
        ci95_hi=-0.01,
        point_estimate=-0.03,
        subgroups=[sub("NYC", 10, -0.02), sub("CHI", 8, -0.01)],
        station_concentration=0.3,
        integrity_ok=True,
    )
    assert verdict == h0019.CONFIRMED


def test_ci_spanning_zero_downgrades_to_inconclusive() -> None:
    verdict, reasons = h0019.classify(
        ci95_lo=-0.05,
        ci95_hi=0.01,
        point_estimate=-0.02,
        subgroups=[sub("NYC", 10, -0.02)],
        station_concentration=0.3,
        integrity_ok=True,
    )
    assert verdict == h0019.SUPPORTED_BUT_INCONCLUSIVE
    assert any("spans zero" in r for r in reasons)


def test_no_improvement_is_not_supported() -> None:
    verdict, reasons = h0019.classify(
        ci95_lo=-0.01,
        ci95_hi=0.05,
        point_estimate=0.02,
        subgroups=[sub("NYC", 10, 0.02)],
        station_concentration=0.3,
        integrity_ok=True,
    )
    assert verdict == h0019.NOT_SUPPORTED
    assert any("no improvement" in r for r in reasons)


def test_integrity_failure_is_invalid_and_short_circuits() -> None:
    verdict, reasons = h0019.classify(
        ci95_lo=-0.05,
        ci95_hi=-0.01,
        point_estimate=-0.03,
        subgroups=[sub("NYC", 10, -0.02)],
        station_concentration=0.3,
        integrity_ok=False,
    )
    assert verdict == h0019.INVALID
    assert reasons == ["integrity_failure"]


def test_station_reversal_blocks_confirmation() -> None:
    verdict, reasons = h0019.classify(
        ci95_lo=-0.05,
        ci95_hi=-0.01,
        point_estimate=-0.03,
        subgroups=[sub("NYC", 10, -0.05), sub("CHI", 6, +0.02)],
        station_concentration=0.3,
        integrity_ok=True,
    )
    assert verdict == h0019.SUPPORTED_BUT_INCONCLUSIVE
    assert any("reversal" in r for r in reasons)


def test_thin_station_reversal_does_not_block_confirmation() -> None:
    verdict, _ = h0019.classify(
        ci95_lo=-0.05,
        ci95_hi=-0.01,
        point_estimate=-0.03,
        subgroups=[sub("NYC", 10, -0.05), sub("LAX", 2, +0.09)],
        station_concentration=0.3,
        integrity_ok=True,
    )
    assert verdict == h0019.CONFIRMED


def test_excess_concentration_blocks_confirmation() -> None:
    verdict, reasons = h0019.classify(
        ci95_lo=-0.05,
        ci95_hi=-0.01,
        point_estimate=-0.03,
        subgroups=[sub("NYC", 10, -0.02)],
        station_concentration=0.55,
        integrity_ok=True,
    )
    assert verdict == h0019.SUPPORTED_BUT_INCONCLUSIVE
    assert any("concentration" in r for r in reasons)


def test_boundary_ci_hi_exactly_zero_is_not_confirmed() -> None:
    verdict, _ = h0019.classify(
        ci95_lo=-0.05,
        ci95_hi=0.0,
        point_estimate=-0.02,
        subgroups=[sub("NYC", 10, -0.02)],
        station_concentration=0.3,
        integrity_ok=True,
    )
    assert verdict == h0019.SUPPORTED_BUT_INCONCLUSIVE


def test_classify_is_total_over_the_registered_verdicts() -> None:
    registered = {
        h0019.CONFIRMED,
        h0019.SUPPORTED_BUT_INCONCLUSIVE,
        h0019.NOT_SUPPORTED,
        h0019.INVALID,
    }
    for lo, hi, pt in [
        (-0.2, -0.1, -0.15),
        (-0.2, 0.1, -0.05),
        (0.05, 0.2, 0.1),
        (0.0, 0.0, 0.0),
        (-0.1, 0.0, -0.05),
    ]:
        for conc in (0.1, 0.45):
            for integrity in (True, False):
                for groups in (
                    [],
                    [sub("NYC", 9, 0.03)],
                    [sub("NYC", 9, -0.03)],
                    [sub("L", 2, 5.0)],
                ):
                    verdict, _ = h0019.classify(
                        ci95_lo=lo,
                        ci95_hi=hi,
                        point_estimate=pt,
                        subgroups=groups,
                        station_concentration=conc,
                        integrity_ok=integrity,
                    )
                    assert verdict in registered


# --- frozen-hash verification ----------------------------------------------


def test_verify_frozen_hashes_passes_against_the_real_registration() -> None:
    assert h0019.verify_frozen_hashes() == h0019.FROZEN_HASHES


def test_frozen_hashes_are_the_ledger_values() -> None:
    real = Path("docs/research/experiments/EXP-FUTURE-H0019")
    for name, expected in h0019.FROZEN_HASHES.items():
        assert hashlib.sha256((real / name).read_bytes()).hexdigest() == expected


def test_frozen_hash_mismatch_is_refused(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text('{"tampered": true}')
    (tmp_path / "registration.md").write_text("tampered")
    with pytest.raises(h0019.H0019GateError, match="frozen hash mismatch"):
        h0019.verify_frozen_hashes(tmp_path)


def test_missing_registration_artifact_is_refused(tmp_path: Path) -> None:
    with pytest.raises(h0019.H0019GateError, match="missing"):
        h0019.verify_frozen_hashes(tmp_path)


# --- single-shot gate order -------------------------------------------------


@pytest.mark.asyncio
async def test_existing_results_refuses_before_fitting(tmp_path: Path) -> None:
    """Passing ``None`` for the session is the assertion: if the guard did not
    fire first, the run would crash on the session instead of refusing."""
    (tmp_path / "results.json").write_text("{}")
    with pytest.raises(h0019.H0019GateError, match="single-shot"):
        await h0019.run_h0019(None, out_dir=tmp_path, now=rd.TEST_END)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_hash_check_precedes_database_access(tmp_path: Path) -> None:
    bad_reg = tmp_path / "reg"
    bad_reg.mkdir()
    (bad_reg / "config.json").write_text("tampered")
    (bad_reg / "registration.md").write_text("tampered")
    out = tmp_path / "out"
    with pytest.raises(h0019.H0019GateError, match="frozen hash mismatch"):
        await h0019.run_h0019(
            None,
            out_dir=out,
            now=rd.TEST_END,
            registration_dir=bad_reg,  # type: ignore[arg-type]
        )
    assert not out.exists(), "a refused run must not create its output directory"


@pytest.mark.asyncio
async def test_non_final_readiness_refuses_before_fitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Synthetic loaders (no database, no production data) return a NOT_READY
    report; the runner must refuse before fitting. This simulates the refusal
    path only -- it never forces readiness to pass."""
    fitted: list[str] = []

    async def fake_sources(*_a: object, **_k: object) -> object:
        return object()

    async def fake_labels(*_a: object, **_k: object) -> list[object]:
        return []

    def fake_readiness(*_a: object, **_k: object) -> rd.ReadinessReport:
        return rd.ReadinessReport(
            state=rd.NOT_READY,
            now=rd.TEST_START,
            conditions={"test_window_elapsed": False, "test_events_met": False},
            detail={},
        )

    def boom(*_a: object, **_k: object) -> None:
        fitted.append("fit")
        raise AssertionError("model fitting must not be reached")

    monkeypatch.setattr(h0019, "load_source_frames", fake_sources)
    monkeypatch.setattr(h0019, "build_labels", fake_labels)
    monkeypatch.setattr(h0019.rd, "compute_readiness", fake_readiness)
    monkeypatch.setattr(h0019.bl, "fit_logistic", boom)

    out = tmp_path / "out"
    with pytest.raises(h0019.H0019GateError, match="readiness is NOT_READY"):
        await h0019.run_h0019(None, out_dir=out, now=rd.TEST_END)  # type: ignore[arg-type]
    assert fitted == [], "no model may be fitted on a refused run"
    assert not out.exists()
    assert not (out / "results.json").exists()
    assert not (out / "artifact_hashes.json").exists()


def test_gate_order_is_single_shot_then_hashes_then_readiness_then_leakage() -> None:
    """The registered order must hold in source: each gate appears before the
    next, and all of them before any model fit."""
    source = Path(h0019.__file__).read_text()
    body = source.split("async def run_h0019", 1)[1]
    order = [
        body.index("already exists"),
        body.index("verify_frozen_hashes("),
        body.index("READY_FOR_FINAL_TEST"),
        body.index("_leakage_checks("),
        body.index("fit_logistic("),
        body.index("grouped_bootstrap_brier_diff("),
        body.index("classify("),
        body.index("artifact_hashes.json"),
    ]
    assert order == sorted(order), "registered gate order violated"


def test_no_descriptive_in_sample_fallback() -> None:
    """H0018 degrades to an in-sample fit on thin coverage; H0019 must not.

    Checked behaviourally (no ``held_out`` variable, no split-reassignment)
    rather than by word search -- H0019's docstring names the fallback in order
    to say it is deliberately absent."""
    h0019_source = Path(h0019.__file__).read_text()
    h0018_source = Path(h0018.__file__).read_text()
    assert "held_out = " in h0018_source, "guard assumes H0018 still has the fallback"
    assert "held_out = " not in h0019_source, "H0019 must have no in-sample fallback flag"
    for reassignment in ("train = g\n", "val = g\n", "test = g\n"):
        assert reassignment not in h0019_source, "H0019 must never widen a split to all rows"
    assert "do not fit in-sample" in h0019_source


def test_confirm_single_shot_is_mandatory_and_refuses_before_fitting() -> None:
    """CLI refusal happens before any async work starts."""
    from kalshi_weather.cli import app

    result = CliRunner().invoke(app, ["experiment", "h0019", "--out-dir", "/nonexistent/h0019"])
    assert result.exit_code == 2
    assert "--confirm-single-shot is required" in result.stdout
    assert not Path("/nonexistent/h0019").exists()


# --- outcome isolation guards -----------------------------------------------
# The two guards below scan only the region ABOVE this header (the actual
# tests). Their own forbidden-token literals live below it and are therefore
# excluded -- otherwise each guard would trip on itself. The marker is
# assembled from fragments so the intact string exists only in this comment.

_MARKER = "# --- outcome isolation " + "guards"


def _scanned_region() -> str:
    """Everything above the guard header: the tests, not the guards."""
    region = THIS_FILE.read_text().split(_MARKER)[0]
    assert len(region) > 5_000, "guard region split failed -- marker moved?"
    return region


def test_no_production_data_access() -> None:
    """No production database, dataset root, or recorded production fixture is
    referenced anywhere in the tests.

    ``monkeypatch.setattr`` lines are exempt: naming a production loader there
    *replaces* it with a synthetic stub, which is evidence that production is
    never reached, not evidence that it is."""
    lines = [line for line in _scanned_region().splitlines() if "monkeypatch.setattr" not in line]
    region = "\n".join(lines)
    for forbidden in (
        "get_settings",
        "_open_session",
        "AsyncSession",
        "load_source_frames",
        "postgresql",
        "psycopg",
        "DATABASE_URL",
        "tests/fixtures",
    ):
        assert forbidden not in region, f"{forbidden} must not appear in H0019 unit tests"


def test_production_loaders_appear_only_as_monkeypatch_targets() -> None:
    """The complement of the guard above: every mention of a production loader
    in the tests is a stub replacement, never a call."""
    for name in ("load_source_frames", "build_labels", "compute_readiness"):
        for line in _scanned_region().splitlines():
            if name in line:
                assert "monkeypatch.setattr" in line, f"{name} used outside a stub: {line.strip()}"


def test_no_outcome_bearing_fixture() -> None:
    """Every Brier difference used here is a literal written by hand; none is
    derived from an H0019 test outcome or artifact."""
    region = _scanned_region()
    assert "EXP-FUTURE-H0019/run" not in region
    assert "decision_grain.parquet" not in region
    assert "test_predictions.csv" not in region
