"""H0020 counts-only readiness: calendar states, extension rule, gates,
exclusions, integrity fail-closed, and metric-isolation. Synthetic fixtures
and an injected clock throughout -- no database, no production access."""

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from kalshi_weather.experiments import h0020_readiness as h20r
from kalshi_weather.experiments.h0020 import ForecastRow
from kalshi_weather.settlement.labels import LabelStatus, SettlementLabel

STATIONS = ("CHI", "DEN", "LAX", "NYC")
INTEGRITY_OK = {"ledger_chain": "verified (4 records)", "hash_config.json": "match"}


def label(
    ticker: str,
    station: str,
    target: date,
    *,
    variable: str = "tmax_f",
    result: str = "yes",
    close_hour: int = 6,
) -> SettlementLabel:
    close = datetime(target.year, target.month, target.day, close_hour, tzinfo=UTC) + timedelta(
        days=1
    )
    return SettlementLabel(
        market_ticker=ticker,
        station_id=station,
        variable=variable,
        target_date=target,
        status=LabelStatus.RESOLVED,
        reconstruction_version="test",
        close_time=close,
        kalshi_result=result,
    )


def series_for(lbl: SettlementLabel, *, delta: float = 2.0) -> list[ForecastRow]:
    """Two ingested issues before the decision: a valid revision pair."""
    assert lbl.close_time is not None
    decision = (lbl.close_time - timedelta(hours=24)).astimezone(UTC).replace(tzinfo=None)
    return [
        ForecastRow(1, decision - timedelta(hours=8), decision - timedelta(hours=7), 88.0),
        ForecastRow(2, decision - timedelta(hours=3), decision - timedelta(hours=2), 88.0 + delta),
    ]


def make_day(
    target: date, stations: tuple[str, ...] = STATIONS, per_station: int = 2
) -> tuple[list[SettlementLabel], dict, dict, dict]:
    """per_station markets per station for one target day, fully evidenced.
    Alternates YES/NO results and tmax/tmin variables for balance."""
    labels, strikes, candles, series = [], {}, {}, {}
    for st in stations:
        for k in range(per_station):
            var = "tmax_f" if k % 2 == 0 else "tmin_f"
            fam = "KXHIGHT" if var == "tmax_f" else "KXLOWT"
            ticker = f"{fam}{st}-{target.isoformat()}-B{90 + k}"
            lbl = label(ticker, st, target, variable=var, result="yes" if k % 2 == 0 else "no")
            labels.append(lbl)
            strikes[ticker] = (90.0 + k, None, "floor")
            candles[ticker] = True
            series[(st, var, target)] = series_for(lbl)
    return labels, strikes, candles, series


def merge(*days):  # type: ignore[no-untyped-def]
    labels, strikes, candles, series = [], {}, {}, {}
    for dl, ds, dc, dsr in days:
        labels += dl
        strikes.update(ds)
        candles.update(dc)
        series.update(dsr)
    return labels, strikes, candles, series


def report(labels, strikes, candles, series, *, now, integrity_ok=True):  # type: ignore[no-untyped-def]
    return h20r.compute_report(
        labels,
        strikes,
        candles,
        series,
        now=now,
        integrity_ok=integrity_ok,
        integrity_detail=dict(INTEGRITY_OK),
    )


def full_test_window_data():  # type: ignore[no-untyped-def]
    """Enough test-window data to satisfy every count gate: 14 days x 4
    stations x 2 markets = 112 groups, balanced labels, 4 stations."""
    days = [make_day(date(2026, 9, 9) + timedelta(days=i)) for i in range(14)]
    return merge(*days)


# --- calendar states ---------------------------------------------------------


def test_pre_train_state() -> None:
    r = report([], {}, {}, {}, now=date(2026, 7, 20))
    assert r.state == h20r.REGISTERED_NOT_READY


def test_collecting_train_state_with_zero_val_test_counts() -> None:
    data = make_day(date(2026, 7, 27))
    r = report(*data, now=date(2026, 7, 28))
    assert r.state == h20r.COLLECTING_TRAIN
    assert r.split_counts["train"]["event_groups"] == 8
    assert r.split_counts["validation"]["event_groups"] == 0  # zero, no error
    assert r.split_counts["test"]["event_groups"] == 0


def test_collecting_validation_and_test_states() -> None:
    assert report([], {}, {}, {}, now=date(2026, 8, 30)).state == h20r.COLLECTING_VALIDATION
    r = report([], {}, {}, {}, now=date(2026, 9, 15))
    assert r.state == h20r.COLLECTING_TEST
    assert r.effective_test_end == date(2026, 9, 22)
    assert r.detail["window_kind"] == "initial"


def test_initial_test_readiness_when_all_gates_pass() -> None:
    data = full_test_window_data()
    r = report(*data, now=date(2026, 9, 23))  # window elapsed
    assert r.state == h20r.READY_FOR_FINAL_TEST
    assert r.detail["failing_gates"] == []
    te = r.split_counts["test"]
    assert te["event_groups"] == 112 and te["stations"] == 4
    assert te["station_concentration"] == pytest.approx(0.25)


def test_first_and_second_extension_then_defer() -> None:
    empty = ([], {}, {}, {})
    # initial end elapsed, gates unmet -> extension 1 collecting
    r1 = report(*empty, now=date(2026, 9, 25))
    assert r1.state == h20r.EXTENDED_TEST_COLLECTING
    assert r1.effective_test_end == date(2026, 9, 29)
    assert r1.extensions_active == 1
    # second extension window
    r2 = report(*empty, now=date(2026, 10, 2))
    assert r2.state == h20r.EXTENDED_TEST_COLLECTING
    assert r2.effective_test_end == date(2026, 10, 6)
    assert r2.extensions_active == 2
    # past the absolute end with gates still unmet -> DEFERRED, forever
    r3 = report(*empty, now=date(2026, 10, 7))
    assert r3.state == h20r.DEFERRED_INSUFFICIENT_DATA
    r4 = report(*empty, now=date(2026, 12, 1))
    assert r4.state == h20r.DEFERRED_INSUFFICIENT_DATA


def test_extension_window_can_still_reach_ready() -> None:
    data = full_test_window_data()
    r = report(*data, now=date(2026, 9, 30))  # first extension elapsed, data fine
    assert r.state == h20r.READY_FOR_FINAL_TEST


def test_exact_window_boundaries() -> None:
    # 08-11 is train; 08-12 gap; 08-25 gap; 08-26 validation; 09-08 validation;
    # 09-09 test -- verified through split assignment of eligible rows
    for d, split in [
        (date(2026, 8, 11), "train"),
        (date(2026, 8, 26), "validation"),
        (date(2026, 9, 8), "validation"),
        (date(2026, 9, 9), "test"),
    ]:
        data = make_day(d, stations=("CHI",), per_station=1)
        r = report(*data, now=date(2026, 10, 20))
        assert r.split_counts[split]["event_groups"] == 1, d


def test_excluded_h0019_gap_rows_never_enter_any_split() -> None:
    data = make_day(date(2026, 8, 15))  # inside the excluded gap
    r = report(*data, now=date(2026, 9, 1))
    for split in ("train", "validation", "test"):
        assert r.split_counts[split]["event_groups"] == 0
    assert r.exclusions_by_split["excluded_gap"]["excluded_registered_gap"] == 8


# --- exclusion reporting -----------------------------------------------------


def test_exclusion_reason_codes_by_split() -> None:
    target = date(2026, 7, 27)
    good = make_day(target, stations=("CHI",), per_station=1)
    labels, strikes, candles, series = merge(good)
    # ineligible family
    labels.append(label("KXTEMPDC-X-B90", "CHI", target))
    # unsupported station
    labels.append(label("KXHIGHTSEA-X-B90", "SEA", target))
    # range contract
    rng = label("KXHIGHTDEN-X-R1", "DEN", target)
    labels.append(rng)
    strikes["KXHIGHTDEN-X-R1"] = (85.0, 95.0, "between")
    candles["KXHIGHTDEN-X-R1"] = True
    # no market evidence
    nme = label("KXHIGHTLAX-X-B90", "LAX", target)
    labels.append(nme)
    strikes["KXHIGHTLAX-X-B90"] = (90.0, None, "floor")
    candles["KXHIGHTLAX-X-B90"] = False
    # collection gap: zero forecast rows for NYC that day
    cg = label("KXHIGHTNYC-X-B90", "NYC", target)
    labels.append(cg)
    strikes["KXHIGHTNYC-X-B90"] = (90.0, None, "floor")
    candles["KXHIGHTNYC-X-B90"] = True
    # missing prior: one ingested issue only for DEN tmin
    mp = label("KXLOWTDEN-X-B70", "DEN", target, variable="tmin_f")
    labels.append(mp)
    strikes["KXLOWTDEN-X-B70"] = (70.0, None, "floor")
    candles["KXLOWTDEN-X-B70"] = True
    assert mp.close_time is not None
    dec = (mp.close_time - timedelta(hours=24)).astimezone(UTC).replace(tzinfo=None)
    series[("DEN", "tmin_f", target)] = [
        ForecastRow(9, dec - timedelta(hours=2), dec - timedelta(hours=1), 70.0)
    ]

    r = report(labels, strikes, candles, series, now=date(2026, 7, 28))
    ex = r.exclusions_by_split["train"]
    assert ex["ineligible_market_family"] == 1
    assert ex["unsupported_station"] == 1
    assert ex["range_or_between_contract"] == 1
    assert ex["no_market_evidence_at_decision"] == 1
    assert ex["collection_gap_no_forecast_rows"] == 1
    assert ex["no_eligible_prior_forecast"] == 1
    assert r.split_counts["train"]["event_groups"] == 1  # the good row survives


def test_unchanged_reissue_is_valid_zero_revision() -> None:
    target = date(2026, 7, 27)
    labels, strikes, candles, series = make_day(target, stations=("CHI",), per_station=1)
    key = ("CHI", "tmax_f", target)
    series[key] = series_for(labels[0], delta=0.0)  # unchanged reissue
    r = report(labels, strikes, candles, series, now=date(2026, 7, 28))
    assert r.split_counts["train"]["event_groups"] == 1
    assert r.split_counts["train"]["zero_revision_rows"] == 1  # retained control


def test_observed_at_only_availability() -> None:
    # issued before decision but INGESTED after: not available -> no pair
    target = date(2026, 7, 27)
    labels, strikes, candles, series = make_day(target, stations=("CHI",), per_station=1)
    assert labels[0].close_time is not None
    dec = (labels[0].close_time - timedelta(hours=24)).astimezone(UTC).replace(tzinfo=None)
    series[("CHI", "tmax_f", target)] = [
        ForecastRow(1, dec - timedelta(hours=8), dec + timedelta(hours=1), 88.0),
        ForecastRow(2, dec - timedelta(hours=3), dec + timedelta(hours=2), 90.0),
    ]
    r = report(labels, strikes, candles, series, now=date(2026, 7, 28))
    assert r.split_counts["train"]["event_groups"] == 0
    assert r.exclusions_by_split["train"]["no_forecast_available_at_decision"] == 1


def test_issue_time_availability_violation_is_invalid() -> None:
    data = make_day(date(2026, 7, 27))
    r = h20r.compute_report(
        *data,
        now=date(2026, 7, 28),
        integrity_ok=True,
        integrity_detail=dict(INTEGRITY_OK),
        availability_field="issue_time",
    )
    assert r.state == h20r.INVALID
    assert "issue_time_availability_violation" in str(r.integrity)


# --- gates and integrity -----------------------------------------------------


def test_station_concentration_and_label_balance_gates() -> None:
    # all data on ONE station -> concentration 1.0 -> gate fails
    days = [make_day(date(2026, 9, 9) + timedelta(days=i), stations=("CHI",)) for i in range(14)]
    r = report(*merge(*days), now=date(2026, 9, 23))
    assert r.state != h20r.READY_FOR_FINAL_TEST
    failing = r.detail["failing_gates"]
    assert "station_concentration" in failing and "min_stations" in failing
    # all-YES labels -> negative-label gate fails
    from dataclasses import replace

    days2 = []
    for i in range(14):
        d = date(2026, 9, 9) + timedelta(days=i)
        labels, s, c, sr = make_day(d)
        labels = [replace(lb, kalshi_result="yes") for lb in labels]
        days2.append((labels, s, c, sr))
    r2 = report(*merge(*days2), now=date(2026, 9, 23))
    assert "min_neg_labels" in r2.detail["failing_gates"]


def test_bootstrap_units_equal_test_event_groups() -> None:
    days = [make_day(date(2026, 9, 9) + timedelta(days=i)) for i in range(3)]  # 24 groups
    r = report(*merge(*days), now=date(2026, 9, 23))
    assert "min_bootstrap_units" in r.detail["failing_gates"]  # 24 < 25
    assert "min_test_event_groups" in r.detail["failing_gates"]


def test_frozen_hash_mismatch_is_invalid() -> None:
    data = full_test_window_data()
    r = report(*data, now=date(2026, 9, 23), integrity_ok=False)
    assert r.state == h20r.INVALID
    assert not r.gates["frozen_hashes_valid"]


def test_live_frozen_integrity_verifies_against_real_ledger() -> None:
    ok, detail = h20r.verify_frozen_integrity(Path(__file__).resolve().parents[2])
    assert ok, detail
    assert detail["hash_config.json"] == "match"
    assert detail["overlap_H0018"] is False and detail["overlap_H0019"] is False
    assert "verified" in detail["ledger_chain"]


def test_deterministic_injected_clock() -> None:
    data = make_day(date(2026, 7, 27))
    a = report(*data, now=date(2026, 7, 28))
    b = report(*data, now=date(2026, 7, 28))
    assert (a.state, a.split_counts, a.exclusions_by_split, a.gates) == (
        b.state,
        b.split_counts,
        b.exclusions_by_split,
        b.gates,
    )


# --- isolation ---------------------------------------------------------------


def test_no_metric_bearing_imports_or_fields() -> None:
    src = (
        Path(__file__).resolve().parents[2]
        / "src/kalshi_weather/experiments/h0020_readiness.py"
    ).read_text()
    banned_imports = re.compile(
        r"experiments\.baseline|experiments\.runner|research\.benchmark|fit_logistic"
        r"|kalshi_weather\.paper|kalshi_weather\.kalshi|translate_probability|Simulator",
    )
    m = banned_imports.search(src)
    assert m is None, f"readiness imports metric/trading machinery: {m.group(0)!r}"
    banned_fields = re.compile(
        r"score|loss|metric|brier|log_loss|prediction|coefficient|pnl|edge", re.IGNORECASE
    )
    for name in h20r.CoverageReport.__dataclass_fields__:
        assert banned_fields.search(name) is None, name
    for name in h20r.EligibleMarket.__dataclass_fields__:
        assert banned_fields.search(name) is None, name


def test_report_carries_no_probability_like_values() -> None:
    data = full_test_window_data()
    r = report(*data, now=date(2026, 9, 23))
    # the only float in split stats is station_concentration -- a count ratio
    for split_stats in r.split_counts.values():
        floats = [k for k, v in split_stats.items() if isinstance(v, float)]
        assert floats == ["station_concentration"]
