"""Group-atomic chronological splits and walk-forward folds.

The split unit is a caller-declared *group* (e.g. an event-day such as
``station x target_date``), never a raw row: correlated rows sharing a group
can never straddle a boundary, which is the leakage mode chronological
row-level splits silently permit.

Each group is assigned its **latest** date. A group that touches a later
window therefore lands wholly in the later window -- the conservative
direction (information can only be held out longer, never leaked earlier).

Everything here is pure and deterministic: no wall clock, no randomness.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import polars as pl


class SplitError(ValueError):
    """A split request that cannot be satisfied safely."""


@dataclass(frozen=True)
class GroupedSplit:
    """Result of a grouped chronological split. Frames preserve input schema."""

    train: pl.DataFrame
    val: pl.DataFrame
    test: pl.DataFrame
    group_col: str
    date_col: str
    train_end: date
    val_end: date
    test_end: date | None

    def group_counts(self) -> dict[str, int]:
        return {
            "train": self.train.get_column(self.group_col).n_unique() if self.train.height else 0,
            "val": self.val.get_column(self.group_col).n_unique() if self.val.height else 0,
            "test": self.test.get_column(self.group_col).n_unique() if self.test.height else 0,
        }


@dataclass(frozen=True)
class WalkForwardFold:
    """One walk-forward fold: train on everything strictly before the test
    segment's first group date; test on the segment."""

    index: int
    train: pl.DataFrame
    test: pl.DataFrame
    test_start: date
    test_end: date


def _group_dates(frame: pl.DataFrame, group_col: str, date_col: str) -> pl.DataFrame:
    """One row per group with its assigned (latest) date."""
    for col in (group_col, date_col):
        if col not in frame.columns:
            raise SplitError(f"column {col!r} not in frame")
    if frame.get_column(group_col).null_count() or frame.get_column(date_col).null_count():
        raise SplitError("null group or date values cannot be split safely")
    return frame.group_by(group_col).agg(pl.col(date_col).max().alias("_group_date"))


def grouped_chronological_split(
    frame: pl.DataFrame,
    *,
    group_col: str,
    date_col: str,
    train_end: date,
    val_end: date,
    test_end: date | None = None,
) -> GroupedSplit:
    """Split by group date: train <= train_end < val <= val_end < test (<= test_end).

    Rows after ``test_end`` (when given) are excluded entirely rather than
    silently appended to test.
    """
    if not train_end < val_end:
        raise SplitError(f"train_end {train_end} must precede val_end {val_end}")
    if test_end is not None and not val_end < test_end:
        raise SplitError(f"val_end {val_end} must precede test_end {test_end}")

    dated = _group_dates(frame, group_col, date_col)
    joined = frame.join(dated, on=group_col, how="left")

    gd = pl.col("_group_date")
    train = joined.filter(gd <= train_end).drop("_group_date")
    val = joined.filter((gd > train_end) & (gd <= val_end)).drop("_group_date")
    test_pred = gd > val_end if test_end is None else (gd > val_end) & (gd <= test_end)
    test = joined.filter(test_pred).drop("_group_date")

    split = GroupedSplit(
        train=train,
        val=val,
        test=test,
        group_col=group_col,
        date_col=date_col,
        train_end=train_end,
        val_end=val_end,
        test_end=test_end,
    )
    assert_no_group_leakage(split)
    return split


def assert_no_group_leakage(split: GroupedSplit) -> None:
    """Every group appears in at most one part. Raises SplitError otherwise.

    Called by the splitter itself (leakage-free by construction *and* by
    check); exposed so experiment runners can re-assert on loaded artifacts.
    """
    parts = {"train": split.train, "val": split.val, "test": split.test}
    seen: dict[object, str] = {}
    for name, df in parts.items():
        if not df.height:
            continue
        for g in df.get_column(split.group_col).unique().to_list():
            if g in seen:
                raise SplitError(f"group {g!r} appears in both {seen[g]} and {name}")
            seen[g] = name


def walk_forward_folds(
    frame: pl.DataFrame,
    *,
    group_col: str,
    date_col: str,
    n_folds: int,
    min_train_groups: int = 1,
) -> list[WalkForwardFold]:
    """Expanding-window walk-forward evaluation over group dates.

    The distinct group dates (after reserving enough early groups to satisfy
    ``min_train_groups``) are cut into ``n_folds`` contiguous test segments in
    chronological order. Fold *k* trains on every group dated strictly before
    its test segment and tests on the segment -- so later folds train on more
    history, and no fold ever trains on its own or a later segment.
    """
    if n_folds < 1:
        raise SplitError("n_folds must be >= 1")
    if min_train_groups < 1:
        raise SplitError("min_train_groups must be >= 1")

    dated = _group_dates(frame, group_col, date_col)
    ordered = dated.sort("_group_date", group_col)
    n_groups = ordered.height
    if n_groups < min_train_groups + n_folds:
        raise SplitError(
            f"{n_groups} groups cannot support min_train_groups={min_train_groups} "
            f"+ n_folds={n_folds}"
        )

    # Reserve the earliest groups as the minimum training block, then never
    # split one date across segments: a segment boundary always falls between
    # distinct dates (the whole date moves into the earlier segment).
    test_pool = ordered.slice(min_train_groups, n_groups - min_train_groups)
    pool_dates: list[date] = sorted(set(test_pool.get_column("_group_date").to_list()))
    if len(pool_dates) < n_folds:
        raise SplitError(f"only {len(pool_dates)} distinct test dates for n_folds={n_folds}")

    per = len(pool_dates) / n_folds
    segments: list[list[date]] = []
    for k in range(n_folds):
        lo, hi = round(k * per), round((k + 1) * per)
        segment = pool_dates[lo:hi]
        if not segment:
            raise SplitError(f"fold {k} received no test dates")
        segments.append(segment)

    joined = frame.join(dated, on=group_col, how="left")
    folds: list[WalkForwardFold] = []
    for k, segment in enumerate(segments):
        seg_start, seg_end = segment[0], segment[-1]
        train = joined.filter(pl.col("_group_date") < seg_start).drop("_group_date")
        test = joined.filter(
            (pl.col("_group_date") >= seg_start) & (pl.col("_group_date") <= seg_end)
        ).drop("_group_date")
        if not train.height:
            raise SplitError(f"fold {k} has an empty training window")
        folds.append(
            WalkForwardFold(index=k, train=train, test=test, test_start=seg_start, test_end=seg_end)
        )
    return folds
