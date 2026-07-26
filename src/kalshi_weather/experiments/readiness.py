"""H0019 readiness system: a READ-ONLY, deterministic monitor that reports when
enough genuinely point-in-time data has accumulated to run the future held-out
replication of H0018.

It computes coverage only -- it never trains a model, never generates test
predictions, and never computes a test Brier score. It reuses the leakage-safe
decision-grain builder purely to count complete, eligible rows per pre-registered
window. See docs/research/experiments/EXP-FUTURE-H0019/registration.md.

H0018 remains immutable; this is a NEW experiment (H0019) with new, future
windows -- future data cannot repair H0018's historical train/validation windows
(missed forecast vintages are unavailable), so a new identifier is required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import polars as pl

from kalshi_weather.dataset.builder import SourceFrames
from kalshi_weather.experiments.decision_grain import build_decision_grain
from kalshi_weather.settlement.labels import SettlementLabel

# --- Frozen registration (H0019) -------------------------------------------
# Anchor: reliable all-station forecast-vintage collection began 2026-07-21.
# Windows are FUTURE and fixed, sized for the observed ~4 event-groups/day so
# each split can plausibly reach its minimum. The test interval is future and
# untouched at registration (today is 2026-07-26).
FORECAST_ARCHIVE_START = date(2026, 7, 21)
HORIZON_HOURS = 24

TRAIN_START = date(2026, 7, 22)
TRAIN_END = date(2026, 8, 4)
VAL_START = date(2026, 8, 5)
VAL_END = date(2026, 8, 11)
TEST_START = date(2026, 8, 12)
TEST_END = date(2026, 8, 25)


@dataclass(frozen=True)
class ReadinessRequirements:
    """Pre-registered OPERATIONAL MINIMUMS (not a formal power analysis) for
    executing H0019, stated in independent event-groups (station x target_date),
    never raw market rows."""

    min_train_events: int = 40
    min_val_events: int = 15
    min_test_events: int = 25
    min_stations: int = 3
    min_events_per_station_test: int = 4
    max_station_concentration: float = 0.40  # no station > 40% of test events
    min_pos_labels_per_split: int = 5
    min_neg_labels_per_split: int = 5
    # window-scoped completion: fraction of eligible in-window markets that
    # produce a complete decision-grain row (price + forecast + observation).
    min_completion_coverage: float = 0.85
    min_bootstrap_units: int = 25  # == min_test_events


REQUIREMENTS = ReadinessRequirements()

# Readiness states.
NOT_READY = "NOT_READY"
TRAIN_READY = "TRAIN_READY"
VALIDATION_READY = "VALIDATION_READY"
READY_FOR_FINAL_TEST = "READY_FOR_FINAL_TEST"
COMPLETED = "COMPLETED"


@dataclass
class ReadinessReport:
    state: str
    now: date
    conditions: dict[str, bool] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis": "H0019",
            "state": self.state,
            "as_of": str(self.now),
            "conditions": self.conditions,
            "detail": self.detail,
        }


def _split_of(d: date) -> str | None:
    if TRAIN_START <= d <= TRAIN_END:
        return "train"
    if VAL_START <= d <= VAL_END:
        return "val"
    if TEST_START <= d <= TEST_END:
        return "test"
    return None


def _split_stats(g: pl.DataFrame) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for split in ("train", "val", "test"):
        sub = g.filter(pl.col("split") == split)
        if sub.height == 0:
            out[split] = {"rows": 0, "events": 0, "pos": 0, "neg": 0, "stations": 0}
            continue
        st = sub.group_by("station").agg(pl.col("event_group").n_unique().alias("e"))
        total_e = sub["event_group"].n_unique()
        max_conc = (int(st["e"].max()) / total_e) if total_e else 0.0  # type: ignore[arg-type]
        out[split] = {
            "rows": sub.height,
            "events": int(total_e),
            "pos": int(sub["y"].sum()),
            "neg": int(sub.height - sub["y"].sum()),
            "stations": int(sub["station"].n_unique()),
            "station_events": dict(zip(st["station"].to_list(), st["e"].to_list(), strict=False)),
            "max_station_concentration": max_conc,
            "min_events_per_station": int(st["e"].min()) if st.height else 0,  # type: ignore[arg-type]
        }
    return out


def compute_readiness(
    sources: SourceFrames,
    labels: list[SettlementLabel],
    *,
    now: date,
    requirements: ReadinessRequirements = REQUIREMENTS,
    completed: bool = False,
) -> ReadinessReport:
    """Deterministic, read-only readiness. Builds the coverage grain (no model
    fitting, no predictions), assigns each row to its pre-registered split by
    target_date, and checks the frozen minimums. ``completed`` is set by the
    caller only if the final experiment has already been executed."""
    dg = build_decision_grain(sources, labels, horizon_hours=HORIZON_HOURS)
    g = dg.frame
    if g.height:
        g = g.with_columns(
            pl.col("target_date")
            .map_elements(lambda d: _split_of(d), return_dtype=pl.Utf8)
            .alias("split")
        ).filter(pl.col("split").is_not_null())
    stats = (
        _split_stats(g)
        if g.height
        else {
            s: {"rows": 0, "events": 0, "pos": 0, "neg": 0, "stations": 0}
            for s in ("train", "val", "test")
        }
    )

    tr, va, te = stats["train"], stats["val"], stats["test"]
    r = requirements
    ex = dg.exclusions

    # Coverage is WINDOW-SCOPED (not whole-history): of the eligible settled
    # weather markets whose target_date falls in a window, what fraction produced
    # a complete decision-grain row? A whole-history rate would be permanently
    # dragged down by pre-archive markets that never had a forecast.
    elig_in_window = 0
    for lb in labels:
        if (
            lb.station_id in ("CHI", "DEN", "LAX", "NYC")
            and lb.kalshi_result in ("yes", "no")
            and lb.close_time is not None
            and lb.target_date is not None
            and _split_of(lb.target_date) is not None
        ):
            elig_in_window += 1
    complete_in_window = g.height if g.height else 0
    completion_cov = complete_in_window / elig_in_window if elig_in_window else 0.0

    conditions = {
        "train_events_met": tr["events"] >= r.min_train_events,
        "val_events_met": va["events"] >= r.min_val_events,
        "test_events_met": te["events"] >= r.min_test_events,
        "test_stations_met": te.get("stations", 0) >= r.min_stations,
        "test_events_per_station_met": te.get("min_events_per_station", 0)
        >= r.min_events_per_station_test,
        "station_concentration_ok": te.get("max_station_concentration", 1.0)
        <= r.max_station_concentration,
        "train_label_balance_ok": tr["pos"] >= r.min_pos_labels_per_split
        and tr["neg"] >= r.min_neg_labels_per_split,
        "test_label_balance_ok": te["pos"] >= r.min_pos_labels_per_split
        and te["neg"] >= r.min_neg_labels_per_split,
        "completion_coverage_ok": completion_cov >= r.min_completion_coverage,
        "bootstrap_units_met": te["events"] >= r.min_bootstrap_units,
        "test_window_elapsed": now > TEST_END,
    }

    # state machine (monotone by data + calendar; test only "ready" once the
    # test window has fully elapsed AND every condition passes)
    if completed:
        state = COMPLETED
    elif all(conditions.values()):
        state = READY_FOR_FINAL_TEST
    elif conditions["train_events_met"] and conditions["val_events_met"]:
        state = VALIDATION_READY
    elif conditions["train_events_met"]:
        state = TRAIN_READY
    else:
        state = NOT_READY

    detail = {
        "forecast_archive_start": str(FORECAST_ARCHIVE_START),
        "windows": {
            "train": [str(TRAIN_START), str(TRAIN_END)],
            "val": [str(VAL_START), str(VAL_END)],
            "test": [str(TEST_START), str(TEST_END)],
        },
        "days_until": {
            "train_end": (TRAIN_END - now).days,
            "val_end": (VAL_END - now).days,
            "test_start": (TEST_START - now).days,
            "test_end": (TEST_END - now).days,
        },
        "split_coverage": stats,
        "completion_coverage": round(completion_cov, 4),
        "eligible_in_windows": elig_in_window,
        "complete_in_windows": complete_in_window,
        "exclusions": ex,
        "requirements": {
            "min_train_events": r.min_train_events,
            "min_val_events": r.min_val_events,
            "min_test_events": r.min_test_events,
            "min_stations": r.min_stations,
            "max_station_concentration": r.max_station_concentration,
            "min_pos_labels_per_split": r.min_pos_labels_per_split,
            "min_completion_coverage": r.min_completion_coverage,
        },
    }
    return ReadinessReport(state=state, now=now, conditions=conditions, detail=detail)
