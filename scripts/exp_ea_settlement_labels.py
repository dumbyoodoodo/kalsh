"""EXP E-A — settlement-label reconstruction validation + H0012 metrics.

Part 1 (engineering validation, gates part 2): payout agreement between the
reconstructed settlement-time value and Kalshi's own expiration_value/result,
over every usable label with Kalshi settlement data. Mismatches are listed,
never ignored.

Part 2 (pre-registered H0012): stage-difference probabilities with Wilson 95%
CIs, unit = (variable, target_date) -- strikes of one event share a timeline
and are not double-counted.

Deterministic; reads only the versioned dataset export.

Usage:
    python scripts/exp_ea_settlement_labels.py --snapshot-dir <DIR> --out <JSON>
"""

import argparse
import json
import math
from pathlib import Path
from typing import Any

import polars as pl

CONFIDENCE_Z = 1.959963984540054


def wilson_ci(successes: int, n: int, z: float = CONFIDENCE_Z) -> tuple[float, float, float]:
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = successes / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))
    return (p, max(0.0, center - margin), min(1.0, center + margin))


def _prop(frame: pl.DataFrame, expr: pl.Expr, name: str) -> dict[str, Any]:
    n = frame.height
    k = int(frame.select(expr.sum()).item()) if n else 0
    p, lo, hi = wilson_ci(k, n)
    return {"metric": name, "count": k, "n": n, "p": p, "ci95": [lo, hi]}


def validate_payout_agreement(labels: pl.DataFrame) -> dict[str, Any]:
    """Engineering gate: does the reconstruction reproduce Kalshi's payouts?"""
    with_kalshi = labels.filter(
        (pl.col("settlement_label_status").is_in(["resolved", "bounded"]))
        & pl.col("kalshi_result").is_in(["yes", "no"])
        & pl.col("kalshi_expiration_value").is_not_null()
    )
    value_agree = with_kalshi.filter(pl.col("payout_value_agrees"))
    result_agree = with_kalshi.filter(pl.col("payout_result_agrees"))
    value_mismatches = with_kalshi.filter(~pl.col("payout_value_agrees")).select(
        "market_ticker",
        "value_at_settlement",
        "kalshi_expiration_value",
        "latest_final_value",
        "settlement_label_status",
    )
    result_mismatches = with_kalshi.filter(
        pl.col("payout_result_agrees").is_not_null() & ~pl.col("payout_result_agrees")
    ).select(
        "market_ticker",
        "value_at_settlement",
        "kalshi_expiration_value",
        "implied_result_at_settlement",
        "kalshi_result",
    )
    return {
        "markets_with_kalshi_settlement_data": with_kalshi.height,
        "value_agreement": {
            "agree": value_agree.height,
            "rate": value_agree.height / with_kalshi.height if with_kalshi.height else None,
        },
        "result_agreement": {
            "agree": result_agree.height,
            "rate": result_agree.height / with_kalshi.height if with_kalshi.height else None,
        },
        "value_mismatches": value_mismatches.to_dicts(),
        "result_mismatches": result_mismatches.to_dicts(),
    }


def per_date_frame(labels: pl.DataFrame) -> pl.DataFrame:
    """Collapse strikes: one row per (variable, target_date). Timeline values
    are identical across strikes of one event by construction; verified here
    (n_unique == 1 per group), which is itself a consistency check."""
    usable = labels.filter(pl.col("settlement_label_status").is_in(["resolved", "bounded"]))
    grouped = (
        usable.group_by(["station_id", "variable", "target_date"])
        .agg(
            value_at_close=pl.col("value_at_close").first(),
            value_at_settlement=pl.col("value_at_settlement").first(),
            latest_final_value=pl.col("latest_final_value").first(),
            settlement_time_is_exact=pl.col("settlement_time_is_exact").first(),
            n_close_variants=pl.col("value_at_close").n_unique(),
            n_settle_variants=pl.col("value_at_settlement").n_unique(),
            kalshi_expiration_value=pl.col("kalshi_expiration_value").first(),
        )
        .sort(["variable", "target_date"])
    )
    inconsistent = grouped.filter(
        (pl.col("n_close_variants") > 1) | (pl.col("n_settle_variants") > 1)
    )
    if inconsistent.height:
        raise AssertionError(
            f"strikes of one event disagree on timeline values: {inconsistent.to_dicts()[:3]}"
        )
    return grouped


def h0012_metrics(dates: pl.DataFrame) -> dict[str, Any]:
    """The pre-registered H0012 stage-difference metrics."""

    def block(frame: pl.DataFrame, label: str) -> dict[str, Any]:
        a = _prop(
            frame,
            (pl.col("value_at_close") != pl.col("latest_final_value")).fill_null(True),
            "P(value_at_close != latest_final)",
        )
        b = _prop(
            frame,
            pl.col("latest_final_value") != pl.col("value_at_settlement"),
            "P(latest_final != value_at_settlement)  [post-settlement correction]",
        )
        c = _prop(
            frame,
            (pl.col("value_at_settlement") != pl.col("value_at_close")).fill_null(True),
            "P(value_at_settlement != value_at_close)",
        )
        mag_close = frame.filter(
            pl.col("value_at_close").is_not_null()
            & (pl.col("value_at_close") != pl.col("latest_final_value"))
        ).select((pl.col("latest_final_value") - pl.col("value_at_close")).abs().alias("d"))
        mag_corr = frame.filter(
            pl.col("latest_final_value") != pl.col("value_at_settlement")
        ).select((pl.col("latest_final_value") - pl.col("value_at_settlement")).abs().alias("d"))
        return {
            "population": label,
            "n_dates": frame.height,
            "claims": [a, b, c],
            "mean_abs_delta_close_vs_final": (
                float(mag_close["d"].mean()) if mag_close.height else None
            ),
            "mean_abs_delta_correction": float(mag_corr["d"].mean()) if mag_corr.height else None,
        }

    pooled = block(dates, "pooled")
    a = next(x for x in pooled["claims"] if x["metric"].startswith("P(value_at_close"))
    b = next(x for x in pooled["claims"] if "correction" in x["metric"])
    if a["ci95"][0] > 0.05 and b["ci95"][1] < 0.02:
        outcome = "confirmed"
    elif a["ci95"][1] < 0.05 or b["ci95"][0] > 0.02:
        outcome = "rejected"
    else:
        outcome = "inconclusive"

    return {
        "pooled": pooled,
        "by_variable": [
            block(sub, str(key[0]))
            for key, sub in sorted(dates.partition_by("variable", as_dict=True).items())
        ],
        "sensitivity_exact_only": block(
            dates.filter(pl.col("settlement_time_is_exact")), "exact settlement_ts only"
        ),
        "decision_rule": {
            "confirmed_if": "A.ci_low > 0.05 AND B.ci_high < 0.02",
            "outcome": outcome,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    labels = pl.read_parquet(args.snapshot_dir / "settlement_labels.parquet")
    manifest = json.loads((args.snapshot_dir / "manifest.json").read_text())

    status_counts = (
        labels.group_by("settlement_label_status").agg(pl.len().alias("n")).sort("n").to_dicts()
    )
    validation = validate_payout_agreement(labels)
    dates = per_date_frame(labels)
    metrics = h0012_metrics(dates)

    results = {
        "experiment": "EXP-20260721-EA-settlement-labels",
        "hypothesis": "H0012",
        "dataset": {
            "version": manifest["version"],
            "content_hashes": manifest["content_hashes"],
            "git_commit": manifest["git_commit"],
            "source_db_revision": manifest["source_db_revision"],
            "reconstruction_version": manifest["config"][
                "settlement_label_reconstruction_version"
            ],
        },
        "label_status_counts": status_counts,
        "payout_validation": validation,
        "h0012": metrics,
    }
    args.out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"status_counts": status_counts}, indent=2))
    print(json.dumps(validation | {"value_mismatches": len(validation["value_mismatches"]),
                                   "result_mismatches": len(validation["result_mismatches"])},
                     indent=2, default=str))
    print("h0012 outcome:", metrics["decision_rule"]["outcome"])
    print(f"written: {args.out}")


if __name__ == "__main__":
    main()
