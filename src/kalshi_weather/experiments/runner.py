"""H0018 experiment runner: freeze the decision grain, verify leakage safety,
split chronologically, fit the pre-registered models, evaluate, and write an
immutable artifact. Deterministic given the DB snapshot and the fixed seed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.dataset.builder import load_source_frames
from kalshi_weather.dataset.provenance import EnvironmentPolicy
from kalshi_weather.experiments import baseline as bl
from kalshi_weather.experiments.decision_grain import build_decision_grain
from kalshi_weather.settlement.labels import build_labels

# Pre-registered split boundaries (H0018) -- frozen before test evaluation.
TRAIN_END = date(2026, 6, 30)
VAL_END = date(2026, 7, 12)
TEST_END = date(2026, 7, 25)
HORIZON_HOURS = 24
SEED = 20260726
N_BOOT = 2000

# feature columns for the logistic models (no liquidity features anywhere)
_WEATHER_FEATURES = ["fc_gap_dir", "obs_gap_dir", "fc_point", "obs_value", "threshold", "fc_age_h"]
_LIQUIDITY_BANNED = {
    "volume",
    "open_interest",
    "trade",
    "trades",
    "spread",
    "depth",
    "imbalance",
    "best_yes_bid_cents",
    "best_yes_ask_cents",
    "orderbook",
}


def _leakage_checks(g: pl.DataFrame) -> dict[str, Any]:
    """Every as-of timestamp must be <= the decision time, and no liquidity
    feature may be present. Returns pass/fail per check."""
    checks: dict[str, Any] = {}
    checks["price_asof_before_decision"] = bool((g["price_asof"] <= g["decision_time"]).all())
    checks["forecast_before_decision"] = bool((g["fc_issue"] <= g["decision_time"]).all())
    checks["observation_before_decision"] = bool((g["obs_time"] <= g["decision_time"]).all())
    checks["decision_before_close"] = bool((g["decision_time"] < g["close_time"]).all())
    checks["no_liquidity_features"] = not any(
        any(b in c.lower() for b in _LIQUIDITY_BANNED) for c in g.columns
    )
    checks["no_result_derived_feature"] = "y" in g.columns and all(
        f not in _WEATHER_FEATURES for f in ("y", "result", "settlement")
    )
    checks["all_passed"] = all(v for v in checks.values())
    return checks


def _feature_matrix(g: pl.DataFrame, cols: list[str]) -> np.ndarray:
    return g.select(cols).to_numpy().astype(float)


def _metrics(p: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    slope, intercept = bl.calibration_slope_intercept(p, y)
    return {
        "brier": bl.brier(p, y),
        "log_loss": bl.log_loss(p, y),
        "ece": bl.expected_calibration_error(p, y),
        "calibration_slope": slope,
        "calibration_intercept": intercept,
        "mean_pred": float(np.mean(p)),
        "actual_rate": float(np.mean(y)),
        "n": len(y),
    }


async def run_h0018(session: AsyncSession, *, out_dir: Path) -> dict[str, Any]:
    policy = EnvironmentPolicy()
    sources = await load_source_frames(session, env_policy=policy)
    labels = await build_labels(session)
    dg = build_decision_grain(sources, labels, horizon_hours=HORIZON_HOURS)
    g = dg.frame
    if g.height == 0:
        raise RuntimeError("empty decision grain")

    leakage = _leakage_checks(g)

    # freeze: hash the grain content deterministically
    import io

    buf = io.BytesIO()
    g.sort("market_ticker").write_parquet(buf)
    grain_bytes = buf.getvalue()
    grain_hash = hashlib.sha256(grain_bytes).hexdigest()

    td = g["target_date"]
    train = g.filter(pl.col("target_date") <= TRAIN_END)
    val = g.filter((pl.col("target_date") > TRAIN_END) & (pl.col("target_date") <= VAL_END))
    test = g.filter((pl.col("target_date") > VAL_END) & (pl.col("target_date") <= TEST_END))

    # Coverage guard (pre-registered claim reduction): the held-out comparison
    # requires a non-empty training set with the weather features. If forecast
    # coverage leaves train/val empty (only recent days survive the as-of
    # forecast join), the pre-registered test is NOT evaluable. Fall back to a
    # DESCRIPTIVE in-sample fit on all rows, clearly flagged held_out=False --
    # it supports no generalization claim and no confirmatory verdict.
    held_out = train.height >= 20 and test.height >= 20
    if not held_out:
        train = g
        val = g
        test = g

    def yq(df: pl.DataFrame) -> np.ndarray:
        return df["y"].to_numpy().astype(float)

    base_rate = float(yq(train).mean()) if train.height else 0.5

    # fit weather-only (M2) and market+weather (M3) on TRAIN; Platt (M4) on TRAIN+VAL
    xw_tr = _feature_matrix(train, _WEATHER_FEATURES)
    m2 = bl.fit_logistic(xw_tr, yq(train), l2=1.0)
    xmw_cols = [*_WEATHER_FEATURES, "mkt_prob"]
    m3 = bl.fit_logistic(_feature_matrix(train, xmw_cols), yq(train), l2=1.0)
    trval = pl.concat([train, val])
    m4 = bl.fit_platt(trval["mkt_prob"].to_numpy().astype(float), yq(trval))

    def predict(df: pl.DataFrame) -> dict[str, np.ndarray]:
        mkt = df["mkt_prob"].to_numpy().astype(float)
        return {
            "M0_base_rate": np.full(df.height, base_rate),
            "M1_raw_market": bl.clip_prob(mkt),
            "M2_weather_only": m2.predict(_feature_matrix(df, _WEATHER_FEATURES)),
            "M3_market_weather": m3.predict(_feature_matrix(df, xmw_cols)),
            "M4_calibrated_market": bl.apply_platt(m4, mkt),
        }

    def eval_split(df: pl.DataFrame) -> dict[str, Any]:
        if df.height == 0:
            return {}
        y = yq(df)
        return {name: _metrics(p, y) for name, p in predict(df).items()}

    val_results = eval_split(val)
    test_results = eval_split(test)

    # primary comparison: M3 - M1 Brier on TEST, grouped bootstrap by event
    boot = None
    if test.height:
        preds = predict(test)
        boot = bl.grouped_bootstrap_brier_diff(
            preds["M3_market_weather"],
            preds["M1_raw_market"],
            yq(test),
            test["event_group"].to_numpy(),
            n_boot=N_BOOT,
            seed=SEED,
        )

    # subgroup stability on test: M3-M1 Brier diff by station
    subgroups = {}
    if test.height:
        preds = predict(test)
        y = yq(test)
        d = (preds["M3_market_weather"] - y) ** 2 - (preds["M1_raw_market"] - y) ** 2
        st = test["station"].to_numpy()
        for s in np.unique(st):
            mask = st == s
            subgroups[str(s)] = {"n": int(mask.sum()), "mean_brier_diff": float(d[mask].mean())}

    result: dict[str, Any] = {
        "hypothesis": "H0018",
        "held_out_test_evaluable": held_out,
        "coverage_note": (
            "held-out train/val/test split populated"
            if held_out
            else "COVERAGE-BLOCKED: as-of forecast coverage leaves the pre-registered "
            "train/val empty; results below are DESCRIPTIVE in-sample only and support "
            "no generalization or confirmatory claim"
        ),
        "horizon_hours": HORIZON_HOURS,
        "grain_sha256": grain_hash,
        "n_rows": g.height,
        "n_events": int(g["event_group"].n_unique()),
        "target_date_range": [str(td.min()), str(td.max())],
        "split_sizes": {"train": train.height, "val": val.height, "test": test.height},
        "split_events": {
            "train": int(train["event_group"].n_unique()) if train.height else 0,
            "val": int(val["event_group"].n_unique()) if val.height else 0,
            "test": int(test["event_group"].n_unique()) if test.height else 0,
        },
        "station_distribution": dict(
            zip(*[x.to_list() for x in g.group_by("station").len().sort("station")], strict=False)
        ),
        "exclusions": dg.exclusions,
        "leakage_checks": leakage,
        "base_rate_train": base_rate,
        "validation": val_results,
        "test": test_results,
        "primary_bootstrap_M3_minus_M1": boot,
        "subgroup_stability_by_station": subgroups,
        "seed": SEED,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(result, indent=2, default=str))
    (out_dir / "decision_grain.parquet").write_bytes(grain_bytes)
    # small CSV sample
    g.head(50).write_csv(out_dir / "sample.csv")
    # per-row test scores
    if test.height:
        preds = predict(test)
        test.select(
            ["market_ticker", "station", "target_date", "event_group", "y", "mkt_prob"]
        ).with_columns([pl.Series(k, v) for k, v in preds.items()]).write_csv(
            out_dir / "test_predictions.csv"
        )
    return result
