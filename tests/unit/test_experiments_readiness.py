"""H0019 readiness system (read-only): window policy, state machine, grouping,
anti-peeking, and H0018 immutability.
"""

import json
from datetime import date, datetime
from pathlib import Path

import polars as pl

from kalshi_weather.experiments import readiness as rd
from kalshi_weather.experiments.decision_grain import DecisionGrain
from kalshi_weather.settlement.labels import LabelStatus, SettlementLabel


def _labels_for(grain: DecisionGrain) -> list[SettlementLabel]:
    """One eligible label per grain row, so compute_readiness's window-scoped
    completion denominator (eligible-in-window) is populated in tests."""
    out = []
    for i, row in enumerate(grain.frame.to_dicts()):
        out.append(
            SettlementLabel(
                market_ticker=f"M{i}",
                station_id=row["station"],
                variable=row["variable"],
                target_date=row["target_date"],
                status=LabelStatus.BOUNDED,
                reconstruction_version="test",
                close_time=datetime(2026, 1, 1, 12, 0),
                kalshi_result="yes" if row["y"] else "no",
            )
        )
    return out


def test_windows_chronological_and_future() -> None:
    # train < val < test, no overlap, and anchored at/after the forecast archive
    assert rd.TRAIN_START >= rd.FORECAST_ARCHIVE_START
    assert rd.TRAIN_END < rd.VAL_START <= rd.VAL_END < rd.TEST_START <= rd.TEST_END
    # test window is future relative to the 2026-07-26 registration date
    assert date(2026, 7, 26) < rd.TEST_START


def test_split_of_boundaries() -> None:
    assert rd._split_of(rd.TRAIN_START) == "train"
    assert rd._split_of(rd.TRAIN_END) == "train"
    assert rd._split_of(rd.VAL_START) == "val"
    assert rd._split_of(rd.TEST_END) == "test"
    assert rd._split_of(date(2026, 7, 1)) is None  # before all windows
    assert rd._split_of(date(2027, 1, 1)) is None  # after all windows


def _grain(rows: list[dict]) -> DecisionGrain:
    frame = pl.DataFrame(
        rows,
        schema={
            "target_date": pl.Date,
            "station": pl.Utf8,
            "variable": pl.Utf8,
            "event_group": pl.Utf8,
            "y": pl.Int64,
        },
    )
    return DecisionGrain(frame=frame, exclusions={}, horizon_hours=24)


def _events(split_start: date, station_var: list[tuple[str, str]], days: int, y_pattern):  # type: ignore[no-untyped-def]
    """Build grain rows: `days` consecutive target-dates from split_start, each
    with the given (station, variable) event-groups; y_pattern(i)->label."""
    from datetime import timedelta

    rows = []
    for d in range(days):
        td = split_start + timedelta(days=d)
        for i, (st, var) in enumerate(station_var):
            eg = f"{st}|{var}|{td}"
            rows.append(
                {
                    "target_date": td,
                    "station": st,
                    "variable": var,
                    "event_group": eg,
                    "y": y_pattern(d, i),
                }
            )
    return rows


def _run(monkeypatch, grain: DecisionGrain, *, now: date, completed: bool = False):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(rd, "build_decision_grain", lambda *a, **k: grain)
    labels = _labels_for(grain)
    return rd.compute_readiness(sources=None, labels=labels, now=now, completed=completed)  # type: ignore[arg-type]


def test_not_ready_when_train_thin(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    g = _grain(_events(rd.TRAIN_START, [("CHI", "tmax")], 3, lambda d, i: d % 2))
    r = _run(monkeypatch, g, now=date(2026, 7, 26))
    assert r.state == rd.NOT_READY


def test_train_then_validation_then_final(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    stations = [("CHI", "tmax"), ("DEN", "tmax"), ("LAX", "tmax"), ("NYC", "tmax")]
    yb = lambda d, i: 1 if (d + i) % 3 == 0 else 0  # noqa: E731  (mixed labels)

    # train only (14 days x 4 = 56 >= 40 train events; val/test empty)
    g_train = _grain(_events(rd.TRAIN_START, stations, 14, yb))
    assert _run(monkeypatch, g_train, now=rd.TRAIN_END).state == rd.TRAIN_READY

    # train + val
    g_tv = _grain(
        _events(rd.TRAIN_START, stations, 14, yb) + _events(rd.VAL_START, stations, 7, yb)
    )
    assert _run(monkeypatch, g_tv, now=rd.VAL_END).state == rd.VALIDATION_READY

    # train + val + test, all conditions met, test window elapsed
    g_full = _grain(
        _events(rd.TRAIN_START, stations, 14, yb)
        + _events(rd.VAL_START, stations, 7, yb)
        + _events(rd.TEST_START, stations, 14, yb)
    )
    r = _run(monkeypatch, g_full, now=date(2026, 8, 26))  # after TEST_END
    assert r.conditions["test_window_elapsed"] is True
    assert r.state == rd.READY_FOR_FINAL_TEST
    # completed flag overrides
    assert _run(monkeypatch, g_full, now=date(2026, 8, 26), completed=True).state == rd.COMPLETED


def test_station_concentration_blocks_final(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # a single station dominating the test set must fail the concentration gate
    yb = lambda d, i: d % 2  # noqa: E731
    stations = [("CHI", "tmax"), ("DEN", "tmax"), ("LAX", "tmax"), ("NYC", "tmax")]
    g = _grain(
        _events(rd.TRAIN_START, stations, 14, yb)
        + _events(rd.VAL_START, stations, 7, yb)
        + _events(rd.TEST_START, [("DEN", "tmax")], 30, yb)  # test = all Denver
    )
    r = _run(monkeypatch, g, now=date(2026, 8, 26))
    assert r.conditions["station_concentration_ok"] is False
    assert r.state != rd.READY_FOR_FINAL_TEST


def test_no_event_group_crosses_a_split(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # event_group contains target_date and splits are by date -> a group is
    # wholly within one split. Verify on a mixed grain.
    stations = [("CHI", "tmax"), ("NYC", "tmax")]
    yb = lambda d, i: 0  # noqa: E731
    g = _grain(
        _events(rd.TRAIN_START, stations, 3, yb)
        + _events(rd.VAL_START, stations, 3, yb)
        + _events(rd.TEST_START, stations, 3, yb)
    )
    frame = g.frame.with_columns(
        pl.col("target_date").map_elements(rd._split_of, return_dtype=pl.Utf8).alias("split")
    )
    per_group_splits = frame.group_by("event_group").agg(pl.col("split").n_unique().alias("n"))
    assert (per_group_splits["n"] == 1).all()


def test_readiness_is_deterministic(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    g = _grain(_events(rd.TRAIN_START, [("CHI", "tmax"), ("DEN", "tmax")], 5, lambda d, i: d % 2))
    a = _run(monkeypatch, g, now=date(2026, 7, 30)).to_dict()
    b = _run(monkeypatch, g, now=date(2026, 7, 30)).to_dict()
    assert a == b


def test_readiness_reveals_no_model_performance(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # anti-peeking: the report must not contain any model score / prediction
    g = _grain(_events(rd.TRAIN_START, [("CHI", "tmax")], 3, lambda d, i: 0))
    blob = json.dumps(_run(monkeypatch, g, now=date(2026, 7, 26)).to_dict()).lower()
    for banned in ("brier", "log_loss", "prediction", "coef", "m3", "test_score"):
        assert banned not in blob


def test_h0018_artifact_is_frozen_and_unchanged() -> None:
    # the committed H0018 record keeps its frozen verdict and grain hash
    p = Path("docs/research/experiments/EXP-20260726-H0018/results.json")
    d = json.loads(p.read_text())
    assert d["hypothesis"] == "H0018"
    assert d["held_out_test_evaluable"] is False
    assert len(d["grain_sha256"]) == 64
    assert d["seed"] == 20260726
