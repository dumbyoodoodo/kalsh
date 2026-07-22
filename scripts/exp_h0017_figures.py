"""H0017 registered figures -- generated AFTER the frozen verdict, from
the pinned inputs via the analysis module's own cohort construction
(PREREG Sec 7: registered quantities only; non-decisional).

Usage:
    python scripts/exp_h0017_figures.py \
        --market-dir "<KALSHI_DATA_DIR>/datasets/exp-20260722-h0017-market" \
        --issuance-dir "<KALSHI_DATA_DIR>/datasets/exp-20260722-h0013-replication" \
        --results docs/research/experiments/EXP-20260722-H0017-revision-risk-pricing-results.json \
        --out-dir docs/research/experiments/figures/h0017
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import exp_h0017_revision_risk_pricing as h0017

SURFACE = "#fcfcfb"
TEXT_SECONDARY = "#52514e"
BLUE = "#2a78d6"
GREEN = "#008300"
NEUTRAL = "#9b9a95"
plt.rcParams.update(
    {
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "axes.grid": True,
        "grid.color": "#e5e4e0",
        "axes.axisbelow": True,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "font.size": 9,
        "figure.dpi": 150,
    }
)


def mean_path(
    cohort: list[dict], candles: dict, offsets: range
) -> tuple[list[int], list[float], list[int]]:
    xs, ys, ns = [], [], []
    for off in offsets:
        vals = []
        for r in cohort:
            t = r["event_time"] + timedelta(minutes=off)
            mid, _ = h0017.window_midpoint(
                candles.get(r["ticker"], []), t - timedelta(minutes=30), t
            )
            if mid is not None:
                vals.append(float(mid))
        if vals:
            xs.append(off)
            ys.append(100 * sum(vals) / len(vals))
            ns.append(len(vals))
    return xs, ys, ns


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--market-dir", type=Path, required=True)
    parser.add_argument("--issuance-dir", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()

    results = json.loads(args.results.read_text())
    market_frame = pl.read_parquet(args.market_dir / "market_prices.parquet")
    labels = pl.read_parquet(args.market_dir / "settlement_labels.parquet")
    issuance = pl.read_parquet(args.issuance_dir / "observation_issuances.parquet")
    gate_outcome = h0017.run_gates(market_frame, labels, issuance)
    assert gate_outcome.all_gates_passed and gate_outcome.cohort is not None
    assert gate_outcome.inputs is not None
    cohort = gate_outcome.cohort
    candles = gate_outcome.inputs.candles
    args.out_dir.mkdir(parents=True, exist_ok=True)

    # fig1: event-study mean price path of at-risk contracts around E.
    fig, ax = plt.subplots(figsize=(8, 3.4))
    xs, ys, _ = mean_path(cohort, candles, range(-120, 181, 5))
    ax.plot(xs, ys, "-", color=BLUE, linewidth=2, label="adjacent at-risk (mean midpoint)")
    locked_cohort = [
        {"ticker": r["locked"].ticker, "event_time": r["event_time"]}
        for r in cohort
        if r["locked"] is not None
    ]
    xs2, ys2, _ = mean_path(locked_cohort, candles, range(-120, 181, 5))
    ax.plot(xs2, ys2, "-", color=NEUTRAL, linewidth=2, label="adjacent locked-NO control")
    rate = float(Decimal(results["primary_results"]["cohort"]["realized_rate"])) * 100
    ax.axhline(rate, color=GREEN, linewidth=1.2, linestyle="--")
    ax.annotate(
        f"realized qualifying-revision rate {rate:.1f}%",
        (0.55, rate),
        xycoords=("axes fraction", "data"),
        textcoords="offset points",
        xytext=(0, 5),
        fontsize=8,
        color=TEXT_SECONDARY,
    )
    ax.axvline(0, color=TEXT_SECONDARY, linewidth=1, linestyle=":")
    ax.annotate(
        "preliminary CLI issues", (2, ax.get_ylim()[1] * 0.9), fontsize=8, color=TEXT_SECONDARY
    )
    ax.set_xlabel("minutes from archived preliminary publication")
    ax.set_ylabel("mean implied probability (¢)")
    ax.set_title(
        "At-risk contracts trade near the true revision probability before and after "
        "the preliminary",
        fontsize=10,
        loc="left",
    )
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(args.out_dir / "fig1-event-study-path.png", bbox_inches="tight")
    plt.close(fig)

    # fig2: calibration bar -- implied vs realized, primary + per variable.
    fig, ax = plt.subplots(figsize=(7, 3.2))
    d = results["descriptive_results"]["per_variable"]
    cells = [
        (
            "pooled\n(primary)",
            results["primary_results"]["cohort"]["mean_p_post"],
            results["primary_results"]["cohort"]["realized_rate"],
        ),
        ("tmax_f", d["tmax_f"]["mean_price"], d["tmax_f"]["realized_rate"]),
        ("tmin_f", d["tmin_f"]["mean_price"], d["tmin_f"]["realized_rate"]),
    ]
    x = range(len(cells))
    width = 0.38
    ax.bar(
        [i - width / 2 for i in x],
        [100 * float(Decimal(c[1])) for c in cells],
        width=width,
        color=BLUE,
        label="mean implied probability",
    )
    ax.bar(
        [i + width / 2 for i in x],
        [100 * float(Decimal(c[2])) for c in cells],
        width=width,
        color=GREEN,
        label="realized frequency",
    )
    ax.set_xticks(list(x))
    ax.set_xticklabels([c[0] for c in cells])
    ax.set_ylabel("%")
    cal = results["primary_results"]["calibration"]
    ax.set_title(
        f"Post-event calibration: D = {float(Decimal(cal['d_hat'])) * 100:+.2f}pp, "
        f"95% CI [{float(Decimal(cal['ci95'][0])) * 100:+.2f}, "
        f"{float(Decimal(cal['ci95'][1])) * 100:+.2f}]pp — within ±7.5pp margin",
        fontsize=10,
        loc="left",
    )
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(args.out_dir / "fig2-calibration.png", bbox_inches="tight")
    plt.close(fig)

    # fig3: residual distribution.
    fig, ax = plt.subplots(figsize=(7, 3.0))
    residuals = [100 * float(r["p_post"] - Decimal(r["y"])) for r in cohort]
    ax.hist(residuals, bins=30, color=BLUE, edgecolor=SURFACE)
    ax.axvline(0, color=TEXT_SECONDARY, linewidth=1, linestyle="--")
    ax.set_xlabel("calibration residual p_POST - y (pp)")
    ax.set_ylabel("at-risk variable-days")
    ax.set_title(
        "Residuals: mass near 0 (correctly-priced NOs); the left tail is the 9 realized revisions",
        fontsize=10,
        loc="left",
    )
    fig.tight_layout()
    fig.savefig(args.out_dir / "fig3-residuals.png", bbox_inches="tight")
    plt.close(fig)

    # fig4: controls panel -- reactions and locked control.
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(8, 3.0))
    pr = results["descriptive_results"]["placebo_reaction"]
    ax1.bar(
        [0, 1],
        [
            100 * float(Decimal(pr["event"]["mean_abs_change"])),
            100 * float(Decimal(pr["placebo"]["mean_abs_change"])),
        ],
        color=[BLUE, NEUTRAL],
        width=0.6,
    )
    ax1.set_xticks([0, 1])
    ax1.set_xticklabels(["event\n(16:35 ET)", "placebo\n(12:35 ET)"])
    ax1.set_ylabel("mean |Δ price| pre→post (pp)")
    ax1.set_title("Reactions: no event-specific jump", fontsize=9, loc="left")
    lc = results["descriptive_results"]["locked_no_control"]
    ax2.bar([0], [100 * float(Decimal(lc["mean_post_price"]))], color=NEUTRAL, width=0.4)
    ax2.axhline(0, color=TEXT_SECONDARY, linewidth=1)
    ax2.set_xticks([0])
    ax2.set_xticklabels([f"locked-NO (n={lc['priced']})"])
    ax2.set_ylabel("mean post-event price (¢)")
    ax2.set_ylim(0, 5)
    ax2.set_title("Decided contracts price at ~0", fontsize=9, loc="left")
    fig.tight_layout()
    fig.savefig(args.out_dir / "fig4-controls.png", bbox_inches="tight")
    plt.close(fig)

    for f in sorted(args.out_dir.glob("*.png")):
        print(f"wrote {f}")
