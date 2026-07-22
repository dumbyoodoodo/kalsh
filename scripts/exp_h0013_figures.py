"""H0013 descriptive figures -- generated AFTER the frozen verdict, from the
frozen results JSON and the pinned dataset frame only (PREREG Sec 9:
figures visualize registered quantities; no new statistic is introduced).
Non-decisional throughout: nothing here feeds back into any outcome.

Usage:
    python scripts/exp_h0013_figures.py \
        --results docs/research/experiments/EXP-20260722-H0013-chi-replication-results.json \
        --dataset-dir "<KALSHI_DATA_DIR>/datasets/exp-20260722-h0013-replication" \
        --out-dir docs/research/experiments/figures/h0013
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from exp_h0013_chi_replication import wilson_interval

# Validated palette (dataviz reference instance, light mode): slot 1 blue for
# tmin, slot 2 green for tmax -- fixed assignment, never cycled.
SURFACE = "#fcfcfb"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
COLOR_TMIN = "#2a78d6"
COLOR_TMAX = "#008300"
GRID = "#e5e4e0"

plt.rcParams.update(
    {
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "text.color": TEXT_PRIMARY,
        "axes.labelcolor": TEXT_SECONDARY,
        "xtick.color": TEXT_SECONDARY,
        "ytick.color": TEXT_SECONDARY,
        "axes.edgecolor": GRID,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
        "axes.axisbelow": True,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "font.size": 9,
        "figure.dpi": 150,
    }
)


def _pct(s: str) -> float:
    return float(Decimal(s)) * 100


def fig1_rates_and_difference(results: dict, out: Path) -> None:
    """Revision rates with Wilson 95% CIs (CHI vs frozen NYC, by variable)
    and the rate difference D with Newcombe 95% CIs."""
    p1 = results["primary_results"]["part_1_rate_difference"]
    rep = results["primary_results"]["replication_assessment"]
    nyc = rep["nyc_reference"]

    cells = [
        ("NYC\n(H0011, published)", nyc["r_tmax"], nyc["n_per_variable"], COLOR_TMAX, "tmax"),
        ("NYC\n(H0011, published)", nyc["r_tmin"], nyc["n_per_variable"], COLOR_TMIN, "tmin"),
        ("CHI\n(H0013)", p1["r_tmax"], p1["n_tmax"], COLOR_TMAX, "tmax"),
        ("CHI\n(H0013)", p1["r_tmin"], p1["n_tmin"], COLOR_TMIN, "tmin"),
    ]

    fig, (ax1, ax2) = plt.subplots(
        1, 2, figsize=(9, 3.6), gridspec_kw={"width_ratios": [3, 2], "wspace": 0.35}
    )

    xs = [0.0, 0.35, 1.0, 1.35]
    for x, (_, r, n, color, _label) in zip(xs, cells, strict=True):
        rate = 100 * r / n
        lo, hi = wilson_interval(r, n)
        ax1.errorbar(
            x,
            rate,
            yerr=[[rate - _pct(str(lo))], [_pct(str(hi)) - rate]],
            fmt="o",
            markersize=8,
            color=color,
            capsize=4,
            linewidth=2,
        )
        ax1.annotate(
            f"{rate:.1f}%",
            (x, rate),
            textcoords="offset points",
            xytext=(10, -3),
            fontsize=8,
            color=TEXT_PRIMARY,
        )
    ax1.set_xticks([0.175, 1.175])
    ax1.set_xticklabels([c for c in ("NYC (H0011, published)", "CHI (H0013)")])
    ax1.set_xlim(-0.4, 1.9)
    ax1.set_ylabel("variable-days revised (%)")
    ax1.set_title("Revision rate by variable, Wilson 95% CI", fontsize=10, loc="left")
    handles = [
        plt.Line2D([], [], marker="o", linestyle="", color=COLOR_TMIN, label="tmin_f"),
        plt.Line2D([], [], marker="o", linestyle="", color=COLOR_TMAX, label="tmax_f"),
    ]
    ax1.legend(handles=handles, frameon=False, fontsize=8, loc="upper left")

    d_cells = [
        ("NYC (published)", _pct(nyc["d_nyc"]), [_pct(v) for v in nyc["ci95_newcombe"]]),
        ("CHI (this run)", _pct(p1["d_hat"]), [_pct(v) for v in p1["ci95_newcombe"]]),
    ]
    for y, (_label, d, (lo, hi)) in enumerate(d_cells):
        ax2.errorbar(
            d,
            y,
            xerr=[[d - lo], [hi - d]],
            fmt="o",
            markersize=8,
            color=COLOR_TMIN,
            capsize=4,
            linewidth=2,
        )
        ax2.annotate(
            f"+{d:.2f}pp [{lo:.2f}, {hi:.2f}]",
            (d, y),
            textcoords="offset points",
            xytext=(0, 10),
            ha="center",
            fontsize=8,
        )
    ax2.axvline(0, color=TEXT_SECONDARY, linewidth=1, linestyle="--")
    ax2.set_yticks([0, 1])
    ax2.set_yticklabels([c[0] for c in d_cells])
    ax2.set_ylim(-0.6, 1.6)
    ax2.set_xlim(-1, 16)
    ax2.set_xlabel("D = p_tmin - p_tmax (percentage points)")
    ax2.set_title("Rate difference, Newcombe 95% CI", fontsize=10, loc="left")

    fig.suptitle(
        "H0013 replication: tmin labels revise more than tmax at both stations",
        fontsize=11,
        x=0.02,
        ha="left",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(out / "fig1-revision-rates-and-difference.png", bbox_inches="tight")
    plt.close(fig)


def fig2_first_appearance_hours(results: dict, out: Path) -> None:
    """First-appearance local-hour histogram (revised CHI days, 24 bins)."""
    hist = results["descriptive_results"]["first_appearance_local_hour_histogram"]
    fig, ax = plt.subplots(figsize=(8, 3.2))
    hours = list(range(24))
    width = 0.42
    ax.bar(
        [h - width / 2 for h in hours],
        hist["tmin_f"],
        width=width,
        color=COLOR_TMIN,
        label="tmin_f",
        edgecolor=SURFACE,
        linewidth=1,
    )
    ax.bar(
        [h + width / 2 for h in hours],
        hist["tmax_f"],
        width=width,
        color=COLOR_TMAX,
        label="tmax_f",
        edgecolor=SURFACE,
        linewidth=1,
    )
    ax.axvline(-0.5, color=TEXT_SECONDARY, linewidth=1, linestyle="--")
    ax.annotate(
        "local midnight boundary",
        (-0.4, max(hist["tmin_f"]) * 0.95),
        fontsize=8,
        color=TEXT_SECONDARY,
    )
    ax.set_xticks(hours[::2])
    ax.set_xlabel("local hour (America/Chicago) of the final value's first appearance")
    ax.set_ylabel("revised variable-days")
    ax.set_title(
        "CHI revised labels reach their final value almost exclusively in the "
        "post-midnight report (issuance cadence, per the frozen scope statement)",
        fontsize=10,
        loc="left",
    )
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(out / "fig2-first-appearance-local-hour.png", bbox_inches="tight")
    plt.close(fig)


def fig3_issuance_count_distribution(frame: pl.DataFrame, out: Path) -> None:
    """Issuance-count distribution per variable-day, CHI vs NYC (from the
    pinned frame; a registered descriptive quantity)."""
    fig, axes = plt.subplots(1, 2, figsize=(8, 3.0), sharey=True)
    for ax, station in zip(axes, ["CHI", "NYC"], strict=True):
        sub = (
            frame.filter(pl.col("station_id") == station)
            .group_by(["variable", "observation_date"])
            .len()
            .group_by(["variable", "len"])
            .len(name="days")
            .sort(["variable", "len"])
        )
        counts = sorted(sub["len"].unique().to_list())
        width = 0.42
        for offset, (variable, color) in enumerate(
            [("tmin_f", COLOR_TMIN), ("tmax_f", COLOR_TMAX)]
        ):
            values = {
                row["len"]: row["days"]
                for row in sub.filter(pl.col("variable") == variable).iter_rows(named=True)
            }
            xs = [c + (offset - 0.5) * width for c in counts]
            ys = [values.get(c, 0) for c in counts]
            bars = ax.bar(
                xs,
                ys,
                width=width,
                color=color,
                label=variable,
                edgecolor=SURFACE,
                linewidth=1,
            )
            for bar, y in zip(bars, ys, strict=True):
                if y:
                    ax.annotate(
                        str(y),
                        (bar.get_x() + bar.get_width() / 2, y),
                        textcoords="offset points",
                        xytext=(0, 2),
                        ha="center",
                        fontsize=7,
                        color=TEXT_SECONDARY,
                    )
        ax.set_xticks(counts)
        ax.set_xlabel("issuances per variable-day")
        ax.set_title(station, fontsize=10, loc="left")
        ax.set_yscale("log")
    axes[0].set_ylabel("variable-days (log scale)")
    axes[0].legend(frameon=False, fontsize=8)
    fig.suptitle(
        "Matched issuance cadence: both archives are dominated by the "
        "two-issuance {same-day preliminary, post-midnight final} pattern",
        fontsize=10,
        x=0.02,
        ha="left",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.90))
    fig.savefig(out / "fig3-issuance-count-distribution.png", bbox_inches="tight")
    plt.close(fig)


def fig4_per_year_rates(results: dict, out: Path) -> None:
    """Per-year CHI revision rates by variable (point estimates; the
    registered stability disclosure)."""
    rates = results["descriptive_results"]["per_year_revision_rates"]
    years = sorted({y for v in rates.values() for y in v})
    fig, ax = plt.subplots(figsize=(7, 3.2))
    for variable, color in [("tmin_f", COLOR_TMIN), ("tmax_f", COLOR_TMAX)]:
        xs, ys = [], []
        for i, year in enumerate(years):
            cell = rates[variable].get(year)
            if cell is None or cell["rate"] is None:
                continue
            xs.append(i)
            ys.append(_pct(cell["rate"]))
        ax.plot(xs, ys, marker="o", markersize=7, linewidth=2, color=color, label=variable)
    labels = []
    for year in years:
        n = rates["tmin_f"].get(year, {}).get("n", 0)
        labels.append(f"{year}\n(n={n})")
    ax.set_xticks(range(len(years)))
    ax.set_xticklabels(labels)
    ax.set_ylabel("variable-days revised (%)")
    ax.set_ylim(0, None)
    ax.set_title(
        "CHI revision rates by calendar year (2022 is a single day; 2026 is a "
        "partial year) -- point estimates only",
        fontsize=10,
        loc="left",
    )
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(out / "fig4-per-year-revision-rates.png", bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()

    results = json.loads(args.results.read_text())
    frame = pl.read_parquet(args.dataset_dir / "observation_issuances.parquet")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    fig1_rates_and_difference(results, args.out_dir)
    fig2_first_appearance_hours(results, args.out_dir)
    fig3_issuance_count_distribution(frame, args.out_dir)
    fig4_per_year_rates(results, args.out_dir)
    for f in sorted(args.out_dir.glob("*.png")):
        print(f"wrote {f}")
