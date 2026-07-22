"""E0001 -- EXPLORATORY mechanism analysis of the CHI 2026 tmax revision
elevation (H0014 outcome B). Read-only throughout; no hypothesis test, no
decision rule, no confirmatory claim. Facts computed here exist to rank
candidate mechanisms and seed future pre-registered H-series entries.

Inputs: the pinned exp-20260722-h0013-replication frame (issuance-level
provenance) and the raw archived CLI product text in `raw_api_payloads`
(occurrence times, correction markers, AS-OF cutoffs -- the fields the
structured frame deliberately does not carry).

Usage:
    python scripts/e0001_chi_mechanism_exploration.py \
        --dataset-dir "<KALSHI_DATA_DIR>/datasets/exp-20260722-h0013-replication" \
        --out docs/research/investigations/2026-07-22-e0001-facts.json \
        --fig-dir docs/research/investigations/figures/e0001
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from kalshi_weather.dataset.manifest import frame_content_hash

STATIONS = ["CHI", "NYC"]
YEARS = [2023, 2024, 2025, 2026]
HIST_YEARS = [2023, 2024, 2025]
TZ = {"CHI": ZoneInfo("America/Chicago"), "NYC": ZoneInfo("America/New_York")}
LATE_CUTOFF_HOUR = 16.0  # the products' universal "AS OF 0400 PM" preliminary cutoff

# CHI: "MAXIMUM   75  2:59 PM"; NYC: "MAXIMUM   74   357 PM" (no colon).
MAX_RE = re.compile(r"MAXIMUM\s+(-?\d+)R?\s+(\d{1,2}):?(\d{2})\s*([AP]M)")
MIN_RE = re.compile(r"MINIMUM\s+(-?\d+)R?\s+(\d{1,2}):?(\d{2})\s*([AP]M)")
HDR_RE = re.compile(r"^C[A-Z]US\d\d\s+K[A-Z]{3}\s+\d{6}\s*([A-Z]{3})?", re.M)
ASOF_RE = re.compile(r"AS OF (\d{1,2})(\d{2}) ([AP]M)")

# Validated palette (dataviz reference instance, light mode).
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


def in_sm(d: date) -> bool:
    return d.year in YEARS and (d.month < 7 or (d.month == 7 and d.day <= 21))


def occ_hour(m: re.Match[str]) -> float:
    h, mi, ap = int(m.group(2)), int(m.group(3)), m.group(4)
    return h % 12 + (12 if ap == "PM" else 0) + mi / 60


def fetch_products() -> dict[tuple[str, str, date], list[tuple[datetime, str]]]:
    """(station, variable, obs_date) -> [(issuance_time_utc, product_text)],
    ascending. Read-only bulk export via psql COPY."""
    q = (
        "COPY (SELECT wo.station_id, wo.variable, wo.observation_date, "
        "wo.issuance_time AT TIME ZONE 'UTC', "
        "replace(replace(rp.payload_json->>'text', E'\\n', '<NL>'), '|', '<PIPE>') "
        "FROM weather_observations wo JOIN raw_api_payloads rp ON rp.id = wo.raw_payload_id "
        "WHERE wo.station_id IN ('CHI','NYC')) "
        "TO STDOUT WITH (FORMAT csv, DELIMITER '|', QUOTE E'\\x01')"
    )
    out = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "kalshi",
            "-d",
            "kalshi_weather",
            "-c",
            q,
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
    ).stdout
    rows: dict[tuple[str, str, date], list[tuple[datetime, str]]] = defaultdict(list)
    for line in out.splitlines():
        parts = line.split("|", 4)
        if len(parts) != 5:
            continue
        station, variable, obs_date, iss, text = parts
        rows[(station, variable, date.fromisoformat(obs_date))].append(
            (
                datetime.fromisoformat(iss),
                text.replace("<NL>", "\n").replace("<PIPE>", "|"),
            )
        )
    for issuances in rows.values():
        issuances.sort()
    return rows


def analyze(dataset_dir: Path) -> dict[str, Any]:
    frame = pl.read_parquet(dataset_dir / "observation_issuances.parquet")
    frame_hash = frame_content_hash(frame)
    products = fetch_products()

    facts: dict[str, Any] = {
        "study": "E0001 (exploratory -- no hypothesis test, no decision rule)",
        "frame_content_hash": frame_hash,
        "frame_hash_matches_pin": frame_hash
        == "5ccf5b7a3ec11f1ec10fd8a3eac244640406130ec75934e58ead3b2f44e28775",
        "late_cutoff_local_hour": LATE_CUTOFF_HOUR,
        "stations": {},
    }

    # Frame-side per-day first/final values for tmax (revision truth source).
    day_vals: dict[tuple[str, date], tuple[float, float]] = {}
    tmax = frame.filter(pl.col("variable") == "tmax_f").sort(
        ["station_id", "observation_date", "issuance_time"]
    )
    tmp: dict[tuple[str, date], list[float]] = defaultdict(list)
    for r in tmax.iter_rows(named=True):
        tmp[(r["station_id"], r["observation_date"])].append(r["value"])
    for k, vals in tmp.items():
        day_vals[k] = (vals[0], vals[-1])

    for station in STATIONS:
        st: dict[str, Any] = {"by_year": {}, "monthly_2026_vs_hist": {}}

        for year in YEARS:
            y: dict[str, Any] = {}
            n_days = rev = up = down = mag_le1 = mag_1_2 = mag_gt2 = 0
            occ_hist = [0] * 24
            late = 0
            occ_parsed = occ_unparsed = corrected = 0
            rev_late = rev_early = unrev_late = unrev_early = 0
            min_occ_late = 0  # tmin occurrence at/after 16:00 (control; expect ~0)
            asof: dict[str, int] = {}
            finals: list[float] = []
            prev: float | None = None
            d2d: list[float] = []

            dates = sorted(
                d
                for (s, v, d) in products
                if s == station and v == "tmax_f" and d.year == year and in_sm(d)
            )
            for d in dates:
                issuances = products[(station, "tmax_f", d)]
                n_days += 1
                first_v, final_v = day_vals[(station, d)]
                revised = round(first_v, 2) != round(final_v, 2)
                delta = round(final_v - first_v, 1)
                if revised:
                    rev += 1
                    up += delta > 0
                    down += delta < 0
                    a = abs(delta)
                    mag_le1 += a <= 1.0
                    mag_1_2 += 1.0 < a <= 2.0
                    mag_gt2 += a > 2.0
                finals.append(final_v)
                if prev is not None:
                    d2d.append(abs(final_v - prev))
                prev = final_v

                for _, text in issuances:
                    m = HDR_RE.search(text)
                    if m and m.group(1) and m.group(1).startswith("CC"):
                        corrected += 1
                m = ASOF_RE.search(issuances[0][1])
                if m:
                    h = int(m.group(1)) % 12 + (12 if m.group(3) == "PM" else 0)
                    asof[str(h)] = asof.get(str(h), 0) + 1

                final_text = issuances[-1][1]
                m = MAX_RE.search(final_text)
                if m:
                    occ_parsed += 1
                    h = occ_hour(m)
                    occ_hist[int(h)] += 1
                    is_late = h >= LATE_CUTOFF_HOUR
                    late += is_late
                    if revised:
                        rev_late += is_late
                        rev_early += not is_late
                    else:
                        unrev_late += is_late
                        unrev_early += not is_late
                else:
                    occ_unparsed += 1
                mn = MIN_RE.search(final_text)
                if mn and occ_hour(mn) >= LATE_CUTOFF_HOUR:
                    min_occ_late += 1

            y.update(
                {
                    "n_days": n_days,
                    "revised": rev,
                    "revised_up": up,
                    "revised_down": down,
                    "delta_mag_le1": mag_le1,
                    "delta_mag_1_2": mag_1_2,
                    "delta_mag_gt2": mag_gt2,
                    "occ_parsed": occ_parsed,
                    "occ_unparsed": occ_unparsed,
                    "late_max_days": late,
                    "late_max_fraction": round(late / occ_parsed, 4) if occ_parsed else None,
                    "revised_given_late": [rev_late, rev_late + unrev_late],
                    "revised_given_early": [rev_early, rev_early + unrev_early],
                    "occurrence_hour_histogram": occ_hist,
                    "corrected_products": corrected,
                    "asof_cutoff_hours": dict(sorted(asof.items())),
                    "tmin_occurrence_late": min_occ_late,
                    "mean_final_tmax": round(sum(finals) / len(finals), 2) if finals else None,
                    "mean_abs_day_over_day_change": (
                        round(sum(d2d) / len(d2d), 2) if d2d else None
                    ),
                    "days_ge_85F": sum(1 for v in finals if v >= 85.0),
                }
            )
            st["by_year"][str(year)] = y

        # monthly late-max + revision, 2026 vs pooled hist (tmax)
        for month in range(1, 8):
            h_late = h_n = h_rev = c_late = c_n = c_rev = 0
            for (s, v, d), issuances in products.items():
                if s != station or v != "tmax_f" or d.month != month or not in_sm(d):
                    continue
                m = MAX_RE.search(issuances[-1][1])
                if not m:
                    continue
                is_late = occ_hour(m) >= LATE_CUTOFF_HOUR
                first_v, final_v = day_vals[(station, d)]
                revised = round(first_v, 2) != round(final_v, 2)
                if d.year in HIST_YEARS:
                    h_n += 1
                    h_late += is_late
                    h_rev += revised
                elif d.year == 2026:
                    c_n += 1
                    c_late += is_late
                    c_rev += revised
            st["monthly_2026_vs_hist"][str(month)] = {
                "hist": {"n": h_n, "late": h_late, "revised": h_rev},
                "y2026": {"n": c_n, "late": c_late, "revised": c_rev},
            }
        facts["stations"][station] = st
    return facts


def make_figures(facts: dict[str, Any], fig_dir: Path) -> None:
    fig_dir.mkdir(parents=True, exist_ok=True)

    # fig1: occurrence-hour histograms, CHI: pooled hist vs 2026 (+ NYC control)
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.4), sharey=True)
    for ax, station in zip(axes, STATIONS, strict=True):
        by_year = facts["stations"][station]["by_year"]
        hist = [0] * 24
        for year in HIST_YEARS:
            hist = [
                a + b
                for a, b in zip(hist, by_year[str(year)]["occurrence_hour_histogram"], strict=True)
            ]
        hist_n = sum(hist) or 1
        y26 = by_year["2026"]["occurrence_hour_histogram"]
        y26_n = sum(y26) or 1
        ax.step(
            range(24),
            [100 * v / hist_n for v in hist],
            where="mid",
            color=NEUTRAL,
            linestyle="--",
            linewidth=2,
            label="2023-25 pooled",
        )
        ax.step(
            range(24),
            [100 * v / y26_n for v in y26],
            where="mid",
            color=GREEN,
            linewidth=2,
            label="2026",
        )
        ax.axvline(LATE_CUTOFF_HOUR - 0.5, color=TEXT_SECONDARY, linewidth=1, linestyle=":")
        ax.set_title(f"{station} (share of days, %)", fontsize=9, loc="left")
        ax.set_xlabel("local hour of daily maximum (final CLI product)")
    axes[0].legend(frameon=False, fontsize=8)
    fig.suptitle(
        "EXPLORATORY -- when the daily max occurred (dotted line: 4 PM preliminary cutoff)",
        fontsize=10,
        x=0.02,
        ha="left",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(fig_dir / "e0001-fig1-occurrence-hours.png", bbox_inches="tight")
    plt.close(fig)

    # fig2: late-max fraction and revision rate by year, per station
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.2), sharey=True)
    for ax, station in zip(axes, STATIONS, strict=True):
        by_year = facts["stations"][station]["by_year"]
        xs = range(len(YEARS))
        late = [
            100 * by_year[str(y)]["late_max_days"] / max(by_year[str(y)]["occ_parsed"], 1)
            for y in YEARS
        ]
        rev = [100 * by_year[str(y)]["revised"] / by_year[str(y)]["n_days"] for y in YEARS]
        ax.plot(xs, late, "o-", color=BLUE, linewidth=2, label="max at/after 4 PM (% days)")
        ax.plot(xs, rev, "o-", color=GREEN, linewidth=2, label="tmax revised (% days)")
        ax.set_xticks(list(xs))
        ax.set_xticklabels([str(y) if y != 2026 else "2026\nYTD" for y in YEARS])
        ax.set_title(station, fontsize=10, loc="left")
    axes[0].set_ylabel("% of season-matched days")
    axes[0].legend(frameon=False, fontsize=8)
    fig.suptitle(
        "EXPLORATORY -- late maxima and revisions move together at CHI; NYC stable",
        fontsize=10,
        x=0.02,
        ha="left",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(fig_dir / "e0001-fig2-late-max-vs-revisions.png", bbox_inches="tight")
    plt.close(fig)

    # fig3: conversion rates P(revised | late) and P(revised | early)
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.2), sharey=True)
    for ax, station in zip(axes, STATIONS, strict=True):
        by_year = facts["stations"][station]["by_year"]
        xs = range(len(YEARS))
        p_late = [
            100
            * by_year[str(y)]["revised_given_late"][0]
            / max(by_year[str(y)]["revised_given_late"][1], 1)
            for y in YEARS
        ]
        p_early = [
            100
            * by_year[str(y)]["revised_given_early"][0]
            / max(by_year[str(y)]["revised_given_early"][1], 1)
            for y in YEARS
        ]
        ax.plot(xs, p_late, "o-", color=BLUE, linewidth=2, label="P(revised | late max)")
        ax.plot(xs, p_early, "o-", color=NEUTRAL, linewidth=2, label="P(revised | early max)")
        ax.set_xticks(list(xs))
        ax.set_xticklabels([str(y) if y != 2026 else "2026\nYTD" for y in YEARS])
        ax.set_title(station, fontsize=10, loc="left")
    axes[0].set_ylabel("% of days in stratum revised")
    axes[0].legend(frameon=False, fontsize=8)
    fig.suptitle(
        "EXPLORATORY -- revision risk is concentrated in late-max days at both stations",
        fontsize=10,
        x=0.02,
        ha="left",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(fig_dir / "e0001-fig3-conversion-rates.png", bbox_inches="tight")
    plt.close(fig)

    # fig4: CHI monthly late-max fraction, 2026 vs hist
    fig, ax = plt.subplots(figsize=(7, 3.0))
    monthly = facts["stations"]["CHI"]["monthly_2026_vs_hist"]
    months = list(range(1, 8))
    hist = [
        100 * monthly[str(m)]["hist"]["late"] / max(monthly[str(m)]["hist"]["n"], 1) for m in months
    ]
    y26 = [
        100 * monthly[str(m)]["y2026"]["late"] / max(monthly[str(m)]["y2026"]["n"], 1)
        for m in months
    ]
    ax.plot(months, hist, "--", color=NEUTRAL, linewidth=2, label="2023-25 pooled")
    ax.plot(months, y26, "o-", color=BLUE, linewidth=2, label="2026")
    ax.set_xlabel("month")
    ax.set_ylabel("% days with max at/after 4 PM")
    ax.set_title(
        "EXPLORATORY -- CHI late-maximum share by month (point estimates, n~28-31/month in 2026)",
        fontsize=9,
        loc="left",
    )
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(fig_dir / "e0001-fig4-chi-monthly-late-max.png", bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--fig-dir", type=Path, required=True)
    args = parser.parse_args()

    facts = analyze(args.dataset_dir)
    facts["generated"] = datetime.now(UTC).date().isoformat()
    import contextlib

    with contextlib.suppress(subprocess.CalledProcessError, FileNotFoundError):
        facts["git_commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()
    args.out.write_text(json.dumps(facts, indent=2, default=str))
    make_figures(facts, args.fig_dir)
    print(f"wrote {args.out}")
    for f in sorted(args.fig_dir.glob("*.png")):
        print(f"wrote {f}")
