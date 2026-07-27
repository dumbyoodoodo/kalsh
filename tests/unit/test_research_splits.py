"""Grouped chronological splits and walk-forward folds: group atomicity,
leakage detection, determinism, and boundary handling."""

from datetime import date

import polars as pl
import pytest

from kalshi_weather.research.splits import (
    GroupedSplit,
    SplitError,
    assert_no_group_leakage,
    grouped_chronological_split,
    walk_forward_folds,
)


def frame(rows: list[tuple[str, date, int]]) -> pl.DataFrame:
    return pl.DataFrame(
        {"grp": [r[0] for r in rows], "d": [r[1] for r in rows], "x": [r[2] for r in rows]}
    )


BASE = frame(
    [
        ("a", date(2026, 7, 1), 1),
        ("a", date(2026, 7, 1), 2),
        ("b", date(2026, 7, 5), 3),
        ("c", date(2026, 7, 10), 4),
        ("d", date(2026, 7, 15), 5),
        ("e", date(2026, 7, 20), 6),
    ]
)


def split_base() -> GroupedSplit:
    return grouped_chronological_split(
        BASE,
        group_col="grp",
        date_col="d",
        train_end=date(2026, 7, 5),
        val_end=date(2026, 7, 12),
    )


def test_split_partitions_groups_chronologically() -> None:
    s = split_base()
    assert set(s.train["grp"]) == {"a", "b"}
    assert set(s.val["grp"]) == {"c"}
    assert set(s.test["grp"]) == {"d", "e"}
    assert s.group_counts() == {"train": 2, "val": 1, "test": 2}
    # all rows of a multi-row group travel together
    assert s.train.filter(pl.col("grp") == "a").height == 2


def test_group_spanning_boundary_lands_wholly_in_later_window() -> None:
    # group "z" has rows on both sides of train_end; its latest date decides.
    f = frame([("z", date(2026, 7, 4), 1), ("z", date(2026, 7, 6), 2), ("w", date(2026, 7, 1), 3)])
    s = grouped_chronological_split(
        f, group_col="grp", date_col="d", train_end=date(2026, 7, 5), val_end=date(2026, 7, 30)
    )
    assert set(s.train["grp"]) == {"w"}
    assert set(s.val["grp"]) == {"z"}  # conservative: held out, not leaked into train
    assert s.val.height == 2


def test_test_end_excludes_later_rows_rather_than_absorbing() -> None:
    s = grouped_chronological_split(
        BASE,
        group_col="grp",
        date_col="d",
        train_end=date(2026, 7, 5),
        val_end=date(2026, 7, 12),
        test_end=date(2026, 7, 16),
    )
    assert set(s.test["grp"]) == {"d"}  # "e" (07-20) excluded, not appended


def test_split_is_deterministic() -> None:
    a, b = split_base(), split_base()
    for part in ("train", "val", "test"):
        assert getattr(a, part).sort("grp", "x").equals(getattr(b, part).sort("grp", "x"))


def test_invalid_boundaries_and_nulls_raise() -> None:
    with pytest.raises(SplitError):
        grouped_chronological_split(
            BASE,
            group_col="grp",
            date_col="d",
            train_end=date(2026, 7, 5),
            val_end=date(2026, 7, 5),
        )
    with pytest.raises(SplitError):
        grouped_chronological_split(
            frame([("a", date(2026, 7, 1), 1)]).with_columns(pl.lit(None).alias("grp")),
            group_col="grp",
            date_col="d",
            train_end=date(2026, 7, 1),
            val_end=date(2026, 7, 2),
        )
    with pytest.raises(SplitError):
        grouped_chronological_split(
            BASE,
            group_col="missing",
            date_col="d",
            train_end=date(2026, 7, 1),
            val_end=date(2026, 7, 2),
        )


def test_leakage_assertion_catches_manufactured_leak() -> None:
    s = split_base()
    leaked = GroupedSplit(
        train=s.train,
        val=s.train,  # same groups in two parts
        test=s.test,
        group_col="grp",
        date_col="d",
        train_end=s.train_end,
        val_end=s.val_end,
        test_end=None,
    )
    with pytest.raises(SplitError, match="appears in both"):
        assert_no_group_leakage(leaked)


# --- walk-forward -----------------------------------------------------------


def test_walk_forward_trains_strictly_before_each_test_segment() -> None:
    folds = walk_forward_folds(BASE, group_col="grp", date_col="d", n_folds=3, min_train_groups=2)
    assert len(folds) == 3
    for f in folds:
        assert f.train.height > 0 and f.test.height > 0
        assert f.train["d"].max() < f.test["d"].min()  # strict ordering: no overlap
        train_groups = set(f.train["grp"])
        assert not train_groups & set(f.test["grp"])
    # expanding window: later folds train on supersets of earlier training groups
    g0 = set(folds[0].train["grp"])
    g2 = set(folds[2].train["grp"])
    assert g0 < g2
    # segments jointly cover the pool exactly once
    tested = [g for f in folds for g in f.test["grp"].unique().to_list()]
    assert sorted(tested) == sorted(set(tested))


def test_walk_forward_deterministic_and_bounded() -> None:
    a = walk_forward_folds(BASE, group_col="grp", date_col="d", n_folds=2, min_train_groups=2)
    b = walk_forward_folds(BASE, group_col="grp", date_col="d", n_folds=2, min_train_groups=2)
    assert [(f.test_start, f.test_end) for f in a] == [(f.test_start, f.test_end) for f in b]
    with pytest.raises(SplitError):
        walk_forward_folds(BASE, group_col="grp", date_col="d", n_folds=6, min_train_groups=2)
    with pytest.raises(SplitError):
        walk_forward_folds(BASE, group_col="grp", date_col="d", n_folds=0)
