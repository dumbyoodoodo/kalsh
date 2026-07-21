"""EXP H0002 — CLI settlement-revision risk (docs/research/experiments/).

Computes exactly the pre-registered H0002 metrics from a versioned dataset
export's `observation_issuances.parquet`, and applies the pre-registered
decision rule. No model fitting; deterministic (Wilson CIs, exact binomial
sign test) so a re-run against the same dataset version reproduces identical
numbers.

Usage:
    python scripts/exp_h0002_cli_revisions.py --snapshot-dir <DATASET_DIR> \
        --out <RESULTS_JSON>

Primary metrics (denominator = ALL station/variable-days with >=1 stored
issuance, matching the hypothesis's own "11/60 of station/variable-days"
arithmetic; a single-issuance day counts as unrevised, and single-issuance
coverage is reported separately as the pre-registered caveat):

  1. revision frequency  = P(first-issued value != final value), Wilson 95% CI
  2. P(|delta| > 1degF), Wilson 95% CI

Decision rule (pre-registered): CONFIRMED if CI lower bound of (1) > 5% AND
CI lower bound of (2) > 1%; REJECTED if both upper bounds fall below those
thresholds; otherwise INCONCLUSIVE.
"""

import argparse
import json
import math
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

STATION = "NYC"
STATION_TZ = ZoneInfo("America/New_York")
VARIABLES = ("tmax_f", "tmin_f")

REVISION_FREQ_THRESHOLD = 0.05
GT1_THRESHOLD = 0.01
CONFIDENCE_Z = 1.959963984540054  # 95%


def wilson_ci(successes: int, n: int, z: float = CONFIDENCE_Z) -> tuple[float, float, float]:
    """(point estimate, lower, upper) Wilson score interval for a proportion."""
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = successes / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))
    return (p, max(0.0, center - margin), min(1.0, center + margin))


def exact_binomial_two_sided(k: int, n: int, p: float = 0.5) -> float:
    """Two-sided exact binomial test p-value (method: sum of outcome
    probabilities <= observed outcome's probability)."""
    if n == 0:
        return 1.0
    probs = [math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(n + 1)]
    observed = probs[k]
    return min(1.0, sum(q for q in probs if q <= observed * (1 + 1e-12)))


def _per_day(issuances: pl.DataFrame) -> pl.DataFrame:
    """One row per (station, variable, observation_date): first/final
    issuance values and times, issuance count, and the revision delta."""
    return (
        issuances.sort("issuance_time")
        .group_by(["station_id", "variable", "observation_date"])
        .agg(
            n_issuances=pl.len(),
            first_value=pl.col("value").first(),
            final_value=pl.col("value").last(),
            first_issuance_time=pl.col("issuance_time").first(),
            final_issuance_time=pl.col("issuance_time").last(),
        )
        .with_columns(
            delta=pl.col("final_value") - pl.col("first_value"),
        )
        .with_columns(
            revised=pl.col("delta") != 0.0,
            abs_delta=pl.col("delta").abs(),
        )
        .sort(["variable", "observation_date"])
    )


def _freq_block(days: pl.DataFrame) -> dict[str, Any]:
    n = days.height
    revised = int(days["revised"].sum()) if n else 0
    gt1 = int((days["abs_delta"] > 1.0).sum()) if n else 0
    rf = wilson_ci(revised, n)
    g1 = wilson_ci(gt1, n)
    return {
        "days": n,
        "revised_days": revised,
        "revision_frequency": rf[0],
        "revision_frequency_ci95": [rf[1], rf[2]],
        "days_abs_delta_gt_1f": gt1,
        "p_abs_delta_gt_1f": g1[0],
        "p_abs_delta_gt_1f_ci95": [g1[1], g1[2]],
    }


def analyze(issuances: pl.DataFrame) -> dict[str, Any]:
    frame = issuances.filter(
        (pl.col("station_id") == STATION) & (pl.col("variable").is_in(list(VARIABLES)))
    )
    days = _per_day(frame)
    n_days = days.height

    # --- primary metrics + decision rule ---------------------------------
    primary = _freq_block(days)
    rf_lo, rf_hi = primary["revision_frequency_ci95"]
    g1_lo, g1_hi = primary["p_abs_delta_gt_1f_ci95"]
    if rf_lo > REVISION_FREQ_THRESHOLD and g1_lo > GT1_THRESHOLD:
        decision = "confirmed"
    elif rf_hi < REVISION_FREQ_THRESHOLD and g1_hi < GT1_THRESHOLD:
        decision = "rejected"
    else:
        decision = "inconclusive"

    # --- coverage caveat (pre-registered): single-issuance days -----------
    multi = days.filter(pl.col("n_issuances") >= 2)
    coverage = {
        "days_total": n_days,
        "days_with_ge2_issuances": multi.height,
        "coverage_ge2_fraction": multi.height / n_days if n_days else 0.0,
        "note": (
            "single-issuance days count as unrevised in the primary metrics; "
            "IEM gaps could hide revisions on those days, so the primary "
            "estimates are lower bounds in that respect"
        ),
        "revision_frequency_among_ge2_days": _freq_block(multi),
    }

    # --- |delta| histogram (revised days, integer degrees) ----------------
    revised_days = days.filter(pl.col("revised"))
    histogram = (
        revised_days.with_columns(bucket=pl.col("abs_delta").ceil().cast(pl.Int64))
        .group_by("bucket")
        .agg(count=pl.len())
        .sort("bucket")
        .to_dicts()
        if revised_days.height
        else []
    )
    delta_stats = (
        {
            "max_abs_delta": float(revised_days["abs_delta"].max()),
            "mean_abs_delta_when_revised": float(revised_days["abs_delta"].mean()),
            "mean_signed_delta_when_revised": float(revised_days["delta"].mean()),
        }
        if revised_days.height
        else {}
    )

    # --- sign asymmetry for same-day preliminary tmax (pre-registered) ----
    tmax_days = days.filter(pl.col("variable") == "tmax_f").with_columns(
        first_local_date=pl.col("first_issuance_time")
        .dt.replace_time_zone("UTC")
        .dt.convert_time_zone(str(STATION_TZ))
        .dt.date()
    )
    same_day = tmax_days.filter(pl.col("first_local_date") == pl.col("observation_date"))
    same_day_revised = same_day.filter(pl.col("revised"))
    up = int((same_day_revised["delta"] > 0).sum())
    down = int((same_day_revised["delta"] < 0).sum())
    sign_asymmetry = {
        "tmax_days_with_same_day_first_issuance": same_day.height,
        "revised_of_those": same_day_revised.height,
        "revised_upward": up,
        "revised_downward": down,
        "upward_fraction_of_revised": up / same_day_revised.height
        if same_day_revised.height
        else None,
        "sign_test_two_sided_p": exact_binomial_two_sided(up, up + down)
        if (up + down)
        else None,
        "note": "physical prior: a same-day running max can only rise; downward "
        "revisions indicate boundary/correction effects",
    }

    # --- pre-registered breakdowns: by year, by variable, by month --------
    def _by(group_expr: pl.Expr, name: str) -> list[dict[str, Any]]:
        out = []
        for key, sub in sorted(
            days.with_columns(group_expr.alias(name)).partition_by(name, as_dict=True).items()
        ):
            block = _freq_block(sub)
            cov = sub.filter(pl.col("n_issuances") >= 2).height / sub.height
            out.append({name: str(key[0]), **block, "coverage_ge2_fraction": cov})
        return out

    by_year = _by(pl.col("observation_date").dt.year(), "year")
    by_variable = _by(pl.col("variable"), "variable")
    by_month = _by(pl.col("observation_date").dt.month(), "month")

    return {
        "station": STATION,
        "variables": list(VARIABLES),
        "date_range": [
            str(days["observation_date"].min()),
            str(days["observation_date"].max()),
        ],
        "primary": primary,
        "decision_rule": {
            "confirmed_if": "CI lower bounds: revision_frequency > 0.05 AND p_gt1 > 0.01",
            "outcome": decision,
        },
        "coverage": coverage,
        "abs_delta_histogram_f": histogram,
        "delta_stats": delta_stats,
        "sign_asymmetry_same_day_tmax": sign_asymmetry,
        "by_year": by_year,
        "by_variable": by_variable,
        "by_month": by_month,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    issuances = pl.read_parquet(args.snapshot_dir / "observation_issuances.parquet")
    manifest = json.loads((args.snapshot_dir / "manifest.json").read_text())

    results = {
        "experiment": "EXP-20260721-H0002-cli-revisions",
        "hypothesis": "H0002",
        "dataset": {
            "version": manifest["version"],
            "content_hashes": manifest["content_hashes"],
            "git_commit": manifest["git_commit"],
            "source_db_revision": manifest["source_db_revision"],
        },
        "analysis": analyze(issuances),
    }
    args.out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(json.dumps(results["analysis"]["primary"], indent=2))
    print("decision:", results["analysis"]["decision_rule"]["outcome"])
    print(f"written: {args.out}")


if __name__ == "__main__":
    main()
