"""H0014 descriptive figures -- generated AFTER the frozen verdict, from
the frozen results JSON only (PREREG Sec 9: registered quantities; no new
statistic). Non-decisional throughout.

Usage:
    python scripts/exp_h0014_figures.py \
        --results docs/research/experiments/EXP-20260722-H0014-recency-stability-results.json \
        --out-dir docs/research/experiments/figures/h0014
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Validated palette (dataviz reference instance, light mode). Variable
# identity keeps H0013's fixed assignment: tmin = blue, tmax = green.
# The historical baseline in fig4 is neutral ink, not a series color.
SURFACE = "#fcfcfb"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
COLOR_TMIN = "#2a78d6"
COLOR_TMAX = "#008300"
NEUTRAL = "#9b9a95"
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

STATIONS = ["CHI", "NYC"]
VARIABLES = [("tmin_f", COLOR_TMIN), ("tmax_f", COLOR_TMAX)]
YEARS = ["2023", "2024", "2025", "2026"]


def _pct(s: str) -> float:
    return float(Decimal(s)) * 100


def fig1_yearly_rates(results: dict, out: Path) -> None:
    cells = results["descriptive_results"]["cells_sm"]
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.4), sharey=True)
    for ax, station in zip(axes, STATIONS, strict=True):
        for offset, (variable, color) in zip((-0.08, 0.08), VARIABLES, strict=True):
            xs, ys, lo_err, hi_err = [], [], [], []
            for i, year in enumerate(YEARS):
                cell = cells[station][variable][year]
                rate = _pct(cell["rate"])
                lo, hi = (_pct(v) for v in cell["ci95_wilson"])
                xs.append(i + offset)
                ys.append(rate)
                lo_err.append(rate - lo)
                hi_err.append(hi - rate)
            ax.errorbar(
                xs,
                ys,
                yerr=[lo_err, hi_err],
                fmt="o-",
                markersize=6,
                linewidth=2,
                capsize=3,
                color=color,
                label=variable,
            )
        ax.set_xticks(range(len(YEARS)))
        ax.set_xticklabels([y if y != "2026" else "2026\nYTD" for y in YEARS])
        ax.set_title(station, fontsize=10, loc="left")
    axes[0].set_ylabel("variable-days revised (%)")
    axes[0].legend(frameon=False, fontsize=8, loc="upper left")
    fig.suptitle(
        "Season-matched (Jan 1 - Jul 21) revision rates by year, Wilson 95% CIs",
        fontsize=11,
        x=0.02,
        ha="left",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(out / "fig1-sm-yearly-rates.png", bbox_inches="tight")
    plt.close(fig)


def fig2_yearly_gaps(results: dict, out: Path) -> None:
    gaps = results["descriptive_results"]["gaps_sm"]
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.2), sharey=True)
    for ax, station in zip(axes, STATIONS, strict=True):
        xs, ys, lo_err, hi_err = [], [], [], []
        for i, year in enumerate(YEARS):
            cell = gaps[station][year]
            d = _pct(cell["d_hat"])
            lo, hi = (_pct(v) for v in cell["ci95_newcombe"])
            xs.append(i)
            ys.append(d)
            lo_err.append(d - lo)
            hi_err.append(hi - d)
        ax.errorbar(
            xs,
            ys,
            yerr=[lo_err, hi_err],
            fmt="o-",
            markersize=6,
            linewidth=2,
            capsize=3,
            color=COLOR_TMIN,
        )
        hist = _pct(gaps[station]["hist"]["d_hat"])
        ax.axhline(hist, color=NEUTRAL, linewidth=1.2, linestyle="--")
        ax.annotate(
            f"hist pooled {hist:+.1f}pp",
            (0.02, hist),
            xycoords=("axes fraction", "data"),
            textcoords="offset points",
            xytext=(0, 4),
            fontsize=8,
            color=TEXT_SECONDARY,
        )
        ax.axhline(0, color=TEXT_SECONDARY, linewidth=1)
        ax.set_xticks(range(len(YEARS)))
        ax.set_xticklabels([y if y != "2026" else "2026\nYTD" for y in YEARS])
        ax.set_title(station, fontsize=10, loc="left")
    axes[0].set_ylabel("gap D = p_tmin - p_tmax (pp)")
    fig.suptitle(
        "Season-matched revision-rate gap by year, Newcombe 95% CIs "
        "(single series per panel: the gap)",
        fontsize=11,
        x=0.02,
        ha="left",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(out / "fig2-sm-yearly-gaps.png", bbox_inches="tight")
    plt.close(fig)


def fig3_contrast_forest(results: dict, out: Path) -> None:
    contrasts = results["primary_results"]["contrasts"]
    rows = []
    for station in STATIONS:
        for variable, color in VARIABLES:
            rows.append((station, variable, color, contrasts[station][variable]))
    fig, ax = plt.subplots(figsize=(8, 3.2))
    for y, (_station, _variable, color, c) in enumerate(reversed(rows)):
        d = _pct(c["delta_hat"])
        l95, u95 = (_pct(v) for v in c["ci95_newcombe"])
        l98, u98 = (_pct(v) for v in c["ci9875_newcombe"])
        ax.plot([l98, u98], [y, y], color=color, linewidth=1.2)  # decision CI (thin, wide)
        ax.plot([l95, u95], [y, y], color=color, linewidth=3.5)  # reporting CI (thick)
        ax.plot([d], [y], "o", color=color, markersize=8, markeredgecolor=SURFACE)
        tag = "  SHIFT" if c["shift_event"] else ""
        ax.annotate(
            f"{d:+.2f}pp [{l98:.2f}, {u98:.2f}]{tag}",
            (u98, y),
            textcoords="offset points",
            xytext=(8, -3),
            fontsize=8,
        )
    ax.axvline(0, color=TEXT_SECONDARY, linewidth=1, linestyle="--")
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([f"{s} {v}" for s, v, _, _ in reversed(rows)])
    ax.set_xlim(-14, 30)
    ax.set_xlabel(
        "Δ = p_2026 - p_hist, season-matched (pp); thick = 95%, thin = 98.75% decision CI"
    )
    ax.set_title(
        "The declared family of 4 contrasts: only CHI tmax shifts at the decision level",
        fontsize=10,
        loc="left",
    )
    fig.tight_layout()
    fig.savefig(out / "fig3-contrast-forest.png", bbox_inches="tight")
    plt.close(fig)


def fig4_monthly_profiles(results: dict, out: Path) -> None:
    monthly = results["descriptive_results"]["monthly_rates"]
    fig, axes = plt.subplots(2, 2, figsize=(9, 5.2), sharex=True, sharey=True)
    months = [str(m) for m in range(1, 8)]
    for row, (variable, color) in enumerate(VARIABLES):
        for col, station in enumerate(STATIONS):
            ax = axes[row][col]
            hist = [_pct(monthly[station][variable][m]["hist"]["rate"]) for m in months]
            y26 = [_pct(monthly[station][variable][m]["y2026"]["rate"]) for m in months]
            ax.plot(range(1, 8), hist, "--", color=NEUTRAL, linewidth=2, label="2023-25 pooled")
            ax.plot(range(1, 8), y26, "o-", color=color, linewidth=2, markersize=5, label="2026")
            ax.set_title(f"{station} {variable}", fontsize=9, loc="left")
            if row == 1:
                ax.set_xlabel("month")
            if col == 0:
                ax.set_ylabel("revised (%)")
            if row == 0 and col == 0:
                ax.legend(frameon=False, fontsize=8)
    fig.suptitle(
        "Monthly revision rates within the matched Jan-Jul window: 2026 vs pooled history "
        "(point estimates; n per month ~28-31 in 2026)",
        fontsize=10,
        x=0.02,
        ha="left",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(out / "fig4-monthly-profiles.png", bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()

    results = json.loads(args.results.read_text())
    args.out_dir.mkdir(parents=True, exist_ok=True)
    fig1_yearly_rates(results, args.out_dir)
    fig2_yearly_gaps(results, args.out_dir)
    fig3_contrast_forest(results, args.out_dir)
    fig4_monthly_profiles(results, args.out_dir)
    for f in sorted(args.out_dir.glob("*.png")):
        print(f"wrote {f}")
