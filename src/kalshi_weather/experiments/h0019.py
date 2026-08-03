"""H0019 registered single-shot runner: the future held-out replication of H0018.

This is the executable half of the H0019 registration
(``docs/research/experiments/EXP-FUTURE-H0019/``). The registration mandates
that the final test run **exactly once**, via a registered runner, never ad
hoc -- see ``docs/runbooks/next_operator_actions.md`` section B.

Relationship to ``runner.py`` (H0018). The registration freezes H0019's model
and feature spec as *identical to H0018*. That identity is therefore enforced
**by construction**: the feature list, leakage checks, feature matrix, and
metric set are imported from ``runner.py`` rather than restated here. A copy
would be free to drift; an import cannot.

What this module adds on top of H0018's mechanics, all of it required by the
registration and none of it optional at run time:

1. **Frozen-hash verification.** ``config.json`` and ``registration.md`` must
   hash to their ledger-recorded values (ledger sequence 2) before anything
   else happens.
2. **A hard readiness gate.** The run is refused unless
   ``readiness.compute_readiness`` reports ``READY_FOR_FINAL_TEST``.
3. **No descriptive fallback.** H0018's runner degrades to a flagged in-sample
   fit when coverage leaves train/val empty (``held_out=False``). For a
   confirmatory single-shot test that behaviour would manufacture a
   verdict-shaped artifact out of an in-sample fit, so here thin coverage is a
   refusal instead.
4. **Single-shot enforcement.** An existing ``results.json`` in the output
   directory aborts the run. The registration forbids re-running after seeing
   an outcome; the runner must not be the thing that makes it easy.
5. **The frozen decision rule**, applied mechanically to produce the verdict.

The gate and decision-rule functions are pure and clock-free, so the whole
verdict surface is unit-testable on synthetic numbers without a database and
without H0019's real data existing.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_weather.dataset.builder import load_source_frames
from kalshi_weather.dataset.provenance import EnvironmentPolicy
from kalshi_weather.experiments import baseline as bl
from kalshi_weather.experiments import readiness as rd
from kalshi_weather.experiments import runner as h0018
from kalshi_weather.experiments.decision_grain import build_decision_grain
from kalshi_weather.settlement.labels import build_labels

HYPOTHESIS_ID = "H0019"

#: Frozen model constants (config.json ``model_spec``; identical to H0018).
SEED = 20260726
N_BOOT = 2000
L2 = 1.0
HORIZON_HOURS = rd.HORIZON_HOURS

#: The registration's windows live in ``readiness`` -- one source of truth, so
#: the runner can never split on different dates than the gate measured.
TRAIN_START, TRAIN_END = rd.TRAIN_START, rd.TRAIN_END
VAL_START, VAL_END = rd.VAL_START, rd.VAL_END
TEST_START, TEST_END = rd.TEST_START, rd.TEST_END

#: Feature spec, imported (never restated) so "identical to H0018" holds by
#: construction. ``_WEATHER_FEATURES`` is H0018's own list object.
WEATHER_FEATURES = h0018._WEATHER_FEATURES
MARKET_WEATHER_FEATURES = [*WEATHER_FEATURES, "mkt_prob"]

#: Registration directory and its ledger-recorded artifact hashes (sequence 2).
REGISTRATION_DIR = Path("docs/research/experiments/EXP-FUTURE-H0019")
FROZEN_HASHES = {
    "config.json": "5d1f763dcf6b78eabe25add8226f2d2d4f814cf6b191be5dca554a29f840ab0e",
    "registration.md": "e8e1200ac3b9d2944f324881d5c8ea755b6da5c4af69fbd2f557d680e78907b5",
}

#: Verdicts. Exactly the four the registration names -- no others may be
#: emitted, and none may be softened.
CONFIRMED = "CONFIRMED"
SUPPORTED_BUT_INCONCLUSIVE = "SUPPORTED BUT INCONCLUSIVE"
NOT_SUPPORTED = "NOT SUPPORTED"
INVALID = "INVALID"


class H0019GateError(RuntimeError):
    """A precondition of the registered single-shot run is not satisfied.

    Raised *before* any model is fitted or any test row is scored, so a
    refused run can never have inspected an outcome.
    """


class H0019SubgroupIntegrityError(H0019GateError):
    """The station-subgroup computation cannot be trusted, so no verdict is
    emitted (the frozen clarification's "fail closed").

    This is the registered ``INVALID`` condition (integrity failure). The run
    deliberately writes **no** verdict artifact: recording a verdict would mean
    trusting the very computation that just failed its integrity check. The
    operator records ``INVALID`` in HYPOTHESES.md by hand.
    """


#: Frozen minimum for subgroup eligibility. NOT a new threshold -- it is the
#: registration's own ``min_events_per_station_test``, reused by reference so
#: the two can never diverge (HYPOTHESES.md H0019 clarification 2026-08-03).
MIN_SUBGROUP_EVENT_GROUPS = rd.REQUIREMENTS.min_events_per_station_test


@dataclass(frozen=True)
class SubgroupDiff:
    """One station's test-set subgroup summary under the frozen clarification.

    ``mean_brier_diff`` is the **unweighted** mean over the station's test
    event-groups (each event-group contributes exactly once, regardless of how
    many contracts it holds). It is ``None`` only for an ineligible station
    whose value is not finite -- an eligible station can never reach that
    state, because a non-finite value fails closed first.
    """

    station: str
    n_event_groups: int
    mean_brier_diff: float | None
    eligible: bool

    @property
    def insufficient_for_veto(self) -> bool:
        """Reported for every thin station: it can neither trigger nor clear
        the sign-reversal condition."""
        return not self.eligible


def station_event_group_counts(
    stations: Sequence[str], event_groups: Sequence[object]
) -> dict[str, int]:
    """Distinct test event-groups per station. **Counts only** -- this function
    never receives or touches an outcome value, so eligibility is decided
    before any Brier difference is loaded, as the clarification requires."""
    per: dict[str, set[object]] = {}
    for station, group in zip(stations, event_groups, strict=True):
        per.setdefault(str(station), set()).add(group)
    return {s: len(g) for s, g in per.items()}


def eligible_stations(
    counts: dict[str, int], *, min_event_groups: int = MIN_SUBGROUP_EVENT_GROUPS
) -> set[str]:
    """Stations carrying at least the registered per-station minimum."""
    return {s for s, n in counts.items() if n >= min_event_groups}


def build_station_subgroups(
    *,
    stations: Sequence[str],
    event_groups: Sequence[object],
    brier_diffs: Sequence[float | None],
    min_event_groups: int = MIN_SUBGROUP_EVENT_GROUPS,
) -> list[SubgroupDiff]:
    """Per-station subgroup summaries under the frozen clarification.

    Aggregation is two-stage and deliberately so: rows are first collapsed to
    one value per event-group, then the station mean is the **unweighted** mean
    of those event-group values. A station whose event-groups hold wildly
    different numbers of contracts therefore gets the same per-event-group
    weighting as any other -- market-count weighting is structurally impossible
    here, not merely avoided by convention.

    Fails closed (``H0019SubgroupIntegrityError``) on: an event-group spanning
    more than one station, or a missing/non-finite value for an **eligible**
    station.
    """
    counts = station_event_group_counts(stations, event_groups)  # counts only, first
    eligible = eligible_stations(counts, min_event_groups=min_event_groups)

    # Integrity: an event-group is (station, variable, target_date), so it must
    # belong to exactly one station. More than one means duplicated or
    # mis-keyed results -- never silently merged.
    group_to_stations: dict[object, set[str]] = {}
    group_values: dict[object, list[float]] = {}
    for station, group, diff in zip(stations, event_groups, brier_diffs, strict=True):
        group_to_stations.setdefault(group, set()).add(str(station))
        # ``None`` is an explicitly missing result -- carried through as NaN so
        # it fails closed for an eligible station rather than vanishing.
        group_values.setdefault(group, []).append(float("nan") if diff is None else float(diff))

    duplicated = {g: s for g, s in group_to_stations.items() if len(s) > 1}
    if duplicated:
        raise H0019SubgroupIntegrityError(
            f"event-group(s) span multiple stations (duplicated/mis-keyed results): "
            f"{sorted(str(g) for g in duplicated)} -- failing closed, no verdict emitted"
        )

    station_groups: dict[str, list[object]] = {}
    for group, stset in group_to_stations.items():
        station_groups.setdefault(next(iter(stset)), []).append(group)

    out: list[SubgroupDiff] = []
    for station in sorted(counts):
        groups = station_groups.get(station, [])
        is_eligible = station in eligible

        # One value per event-group: mean over that group's rows.
        per_group: list[float] = []
        for group in groups:
            values = group_values.get(group, [])
            per_group.append(float(np.mean(values)) if values else float("nan"))

        missing = not per_group
        non_finite = [v for v in per_group if not np.isfinite(v)]
        if is_eligible and (missing or non_finite):
            raise H0019SubgroupIntegrityError(
                f"eligible station {station!r} has "
                f"{'no' if missing else 'non-finite'} event-group results "
                "-- failing closed, no verdict emitted (registered INVALID condition)"
            )

        # Unweighted across event-groups: each contributes exactly once.
        mean = float(np.mean(per_group)) if per_group else float("nan")
        out.append(
            SubgroupDiff(
                station=station,
                n_event_groups=counts[station],
                mean_brier_diff=mean if np.isfinite(mean) else None,
                eligible=is_eligible,
            )
        )
    return out


def has_major_station_reversal(subgroups: list[SubgroupDiff]) -> bool:
    """Whether any **eligible** station reverses the primary sign.

    Per the frozen clarification (HYPOTHESES.md H0019, 2026-08-03): a reversal
    is present when at least one eligible station -- at least
    ``MIN_SUBGROUP_EVENT_GROUPS`` test event-groups -- has an unweighted mean
    Brier difference **strictly greater than zero**. Exactly zero is not a
    reversal. One such station is sufficient. Ineligible stations can neither
    trigger nor clear the condition.
    """
    return any(
        s.eligible and s.mean_brier_diff is not None and s.mean_brier_diff > 0 for s in subgroups
    )


def classify(
    *,
    ci95_lo: float,
    ci95_hi: float,
    point_estimate: float,
    subgroups: list[SubgroupDiff],
    station_concentration: float,
    integrity_ok: bool,
    max_station_concentration: float = rd.REQUIREMENTS.max_station_concentration,
) -> tuple[str, list[str]]:
    """Apply the frozen decision rule. Returns ``(verdict, reasons)``.

    A negative Brier difference means M3 (market+weather) beats M1 (raw
    market): lower Brier is better, so improvement is the *negative* direction
    throughout.

    Pure and total -- every input combination yields exactly one of the four
    registered verdicts.
    """
    reasons: list[str] = []
    if not integrity_ok:
        return INVALID, ["integrity_failure"]

    concentration_ok = station_concentration <= max_station_concentration
    reversal = has_major_station_reversal(subgroups)
    ci_entirely_below_zero = ci95_hi < 0
    point_improves = point_estimate < 0

    if ci_entirely_below_zero and not reversal and concentration_ok:
        reasons.append("ci95 entirely below zero; no major station reversal; concentration ok")
        return CONFIRMED, reasons

    if not point_improves:
        reasons.append(f"no improvement: point estimate {point_estimate:+.6f} is not below zero")
        return NOT_SUPPORTED, reasons

    # Point estimate improves but at least one confirmatory condition failed.
    if not ci_entirely_below_zero:
        reasons.append(f"ci95 spans zero: [{ci95_lo:+.6f}, {ci95_hi:+.6f}]")
    if reversal:
        reasons.append("major station-subgroup sign reversal present")
    if not concentration_ok:
        reasons.append(
            f"station concentration {station_concentration:.3f} exceeds "
            f"{max_station_concentration:.2f}"
        )
    return SUPPORTED_BUT_INCONCLUSIVE, reasons


def verify_frozen_hashes(registration_dir: Path = REGISTRATION_DIR) -> dict[str, str]:
    """Hash the frozen registration artifacts, raising if any has changed.

    Returns the observed digests. A mismatch means the registration was edited
    after freezing, which invalidates the experiment -- the runner refuses
    rather than recording a verdict against an unknown specification.
    """
    observed: dict[str, str] = {}
    for name, expected in FROZEN_HASHES.items():
        path = registration_dir / name
        if not path.is_file():
            raise H0019GateError(f"frozen registration artifact missing: {path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        observed[name] = digest
        if digest != expected:
            raise H0019GateError(
                f"frozen hash mismatch for {name}: expected {expected[:12]}..., "
                f"got {digest[:12]}... -- STOP and investigate; do not edit frozen files"
            )
    return observed


def _split(g: pl.DataFrame, start: date, end: date) -> pl.DataFrame:
    return g.filter((pl.col("target_date") >= start) & (pl.col("target_date") <= end))


def _subgroups(test: pl.DataFrame, preds: dict[str, np.ndarray]) -> list[SubgroupDiff]:
    """Per-station subgroup summaries on test, under the frozen clarification.

    Delegates the entire rule to ``build_station_subgroups``, so the aggregation
    the verdict depends on is exactly the code the synthetic tests exercise --
    there is no second, production-only aggregation path.
    """
    y = test["y"].to_numpy().astype(float)
    d = (preds["M3_market_weather"] - y) ** 2 - (preds["M1_raw_market"] - y) ** 2
    return build_station_subgroups(
        stations=[str(s) for s in test["station"].to_list()],
        event_groups=list(test["event_group"].to_list()),
        brier_diffs=[float(v) for v in d],
    )


async def run_h0019(
    session: AsyncSession,
    *,
    out_dir: Path,
    now: date,
    registration_dir: Path = REGISTRATION_DIR,
) -> dict[str, Any]:
    """Execute the registered H0019 final test exactly once.

    Order matters and is part of the specification: single-shot check, then
    frozen hashes, then readiness, then -- and only then -- any model fitting.
    Every refusal path raises ``H0019GateError`` before a single test row is
    scored.
    """
    results_path = out_dir / "results.json"
    if results_path.exists():
        raise H0019GateError(
            f"{results_path} already exists -- H0019 is single-shot and must never be "
            "re-run after an outcome is visible. Archive the existing artifact instead."
        )

    observed_hashes = verify_frozen_hashes(registration_dir)

    sources = await load_source_frames(session, env_policy=EnvironmentPolicy())
    labels = await build_labels(session)

    readiness = rd.compute_readiness(sources, labels, now=now)
    if readiness.state != rd.READY_FOR_FINAL_TEST:
        failing = [k for k, v in readiness.conditions.items() if not v]
        raise H0019GateError(
            f"readiness is {readiness.state}, not {rd.READY_FOR_FINAL_TEST}; "
            f"failing conditions: {failing}. Wait -- never weaken a gate to force a run."
        )

    dg = build_decision_grain(sources, labels, horizon_hours=HORIZON_HOURS)
    g = dg.frame
    if g.height == 0:
        raise H0019GateError("empty decision grain")

    leakage = h0018._leakage_checks(g)
    if not leakage["all_passed"]:
        failed = [k for k, v in leakage.items() if k != "all_passed" and not v]
        raise H0019GateError(f"leakage checks failed: {failed} -- verdict would be INVALID")

    buf = io.BytesIO()
    g.sort("market_ticker").write_parquet(buf)
    grain_bytes = buf.getvalue()
    grain_hash = hashlib.sha256(grain_bytes).hexdigest()

    train = _split(g, TRAIN_START, TRAIN_END)
    val = _split(g, VAL_START, VAL_END)
    test = _split(g, TEST_START, TEST_END)

    # No descriptive fallback: readiness already proved these are populated, so
    # an empty split here means the grain and the gate disagree -- a defect, not
    # a reason to fit in-sample.
    for name, df in (("train", train), ("val", val), ("test", test)):
        if df.height == 0:
            raise H0019GateError(
                f"{name} split is empty despite READY_FOR_FINAL_TEST -- "
                "readiness and decision grain disagree; investigate, do not fit in-sample"
            )

    def yq(df: pl.DataFrame) -> np.ndarray:
        return df["y"].to_numpy().astype(float)

    base_rate = float(yq(train).mean())

    m2 = bl.fit_logistic(h0018._feature_matrix(train, WEATHER_FEATURES), yq(train), l2=L2)
    m3 = bl.fit_logistic(h0018._feature_matrix(train, MARKET_WEATHER_FEATURES), yq(train), l2=L2)
    trval = pl.concat([train, val])
    m4 = bl.fit_platt(trval["mkt_prob"].to_numpy().astype(float), yq(trval))

    def predict(df: pl.DataFrame) -> dict[str, np.ndarray]:
        mkt = df["mkt_prob"].to_numpy().astype(float)
        return {
            "M0_base_rate": np.full(df.height, base_rate),
            "M1_raw_market": bl.clip_prob(mkt),
            "M2_weather_only": m2.predict(h0018._feature_matrix(df, WEATHER_FEATURES)),
            "M3_market_weather": m3.predict(h0018._feature_matrix(df, MARKET_WEATHER_FEATURES)),
            "M4_calibrated_market": bl.apply_platt(m4, mkt),
        }

    def eval_split(df: pl.DataFrame) -> dict[str, Any]:
        y = yq(df)
        return {name: h0018._metrics(p, y) for name, p in predict(df).items()}

    test_preds = predict(test)
    boot = bl.grouped_bootstrap_brier_diff(
        test_preds["M3_market_weather"],
        test_preds["M1_raw_market"],
        yq(test),
        test["event_group"].to_numpy(),
        n_boot=N_BOOT,
        seed=SEED,
    )

    subgroups = _subgroups(test, test_preds)
    test_stats = readiness.detail["split_coverage"]["test"]
    concentration = float(test_stats.get("max_station_concentration", 1.0))

    verdict, reasons = classify(
        ci95_lo=boot["ci95_lo"],
        ci95_hi=boot["ci95_hi"],
        point_estimate=boot["mean_brier_diff"],
        subgroups=subgroups,
        station_concentration=concentration,
        integrity_ok=True,
    )

    result: dict[str, Any] = {
        "hypothesis": HYPOTHESIS_ID,
        "verdict": verdict,
        "verdict_reasons": reasons,
        "run_date": str(now),
        "readiness_state": readiness.state,
        "frozen_hashes": observed_hashes,
        "grain_sha256": grain_hash,
        "horizon_hours": HORIZON_HOURS,
        "windows": {
            "train": [str(TRAIN_START), str(TRAIN_END)],
            "val": [str(VAL_START), str(VAL_END)],
            "test": [str(TEST_START), str(TEST_END)],
        },
        "split_sizes": {"train": train.height, "val": val.height, "test": test.height},
        "split_events": {
            "train": int(train["event_group"].n_unique()),
            "val": int(val["event_group"].n_unique()),
            "test": int(test["event_group"].n_unique()),
        },
        "station_concentration_test": concentration,
        "exclusions": dg.exclusions,
        "leakage_checks": leakage,
        "base_rate_train": base_rate,
        "validation": eval_split(val),
        "test": eval_split(test),
        "primary_bootstrap_M3_minus_M1": boot,
        # Every station is reported, including thin ones: the clarification
        # requires their counts and insufficiency to be visible, not omitted.
        "subgroup_stability_by_station": [
            {
                "station": s.station,
                "n_event_groups": s.n_event_groups,
                "mean_brier_diff": s.mean_brier_diff,
                "eligible_for_veto": s.eligible,
                "insufficient_for_veto": s.insufficient_for_veto,
            }
            for s in subgroups
        ],
        "subgroup_min_event_groups": MIN_SUBGROUP_EVENT_GROUPS,
        "subgroup_rule": (
            "unweighted mean Brier difference across a station's test event-groups "
            "(each event-group contributes exactly once); reversal when an eligible "
            f"station (>= {MIN_SUBGROUP_EVENT_GROUPS} test event-groups) is strictly "
            "> 0; frozen HYPOTHESES.md H0019 clarification 2026-08-03"
        ),
        "major_station_reversal": has_major_station_reversal(subgroups),
        "seed": SEED,
        "n_boot": N_BOOT,
        "claim_scope": (
            "Probability scoring only. No tradable, execution, or market-efficiency "
            "claim under any outcome."
        ),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(result, indent=2, default=str))
    (out_dir / "decision_grain.parquet").write_bytes(grain_bytes)
    test.select(
        ["market_ticker", "station", "target_date", "event_group", "y", "mkt_prob"]
    ).with_columns([pl.Series(k, v) for k, v in test_preds.items()]).write_csv(
        out_dir / "test_predictions.csv"
    )

    # Archive manifest: sha256 of every artifact written, so the run can be
    # verified later without trusting this process.
    manifest = {
        name: hashlib.sha256((out_dir / name).read_bytes()).hexdigest()
        for name in ("results.json", "decision_grain.parquet", "test_predictions.csv")
    }
    (out_dir / "artifact_hashes.json").write_text(json.dumps(manifest, indent=2))
    return result
