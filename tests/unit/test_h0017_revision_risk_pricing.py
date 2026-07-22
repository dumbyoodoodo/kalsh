"""Tests for scripts/exp_h0017_revision_risk_pricing.py.

Implements PREREG-20260722-H0017 Sec 10's checklist. All data synthetic;
nothing touches the pinned inputs or computes a real price-vs-outcome
quantity.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import exp_h0017_revision_risk_pricing as h0017

# --- Synthetic input builders ------------------------------------------------


def _event_time(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 20, 35)


def _build_inputs(
    *,
    n_days: int = 100,
    price_cents: dict[str, int] | None = None,
    y_rate: dict[str, float] | None = None,
    start: date = date(2026, 5, 15),
    drop_post_quotes_first: int = 0,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """(market_prices, settlement_labels, issuance) frames. Per variable-day
    ladder: a neither bucket (contains prelim), an adjacent at-risk bucket,
    and a locked-NO contract. Bresenham outcome spread gives exact y rates."""
    price_cents = price_cents or {"tmax_f": 10, "tmin_f": 10}
    y_rate = y_rate or {"tmax_f": 0.10, "tmin_f": 0.10}
    PRELIM = {"tmax_f": 80.0, "tmin_f": 60.0}

    mp_rows: list[dict[str, Any]] = []
    lb_rows: list[dict[str, Any]] = []
    iss_rows: list[dict[str, Any]] = []
    payload = 0

    def candle(ticker: str, t: datetime, bid: int | None, ask: int | None) -> dict[str, Any]:
        return {
            "market_ticker": ticker,
            "period_interval_seconds": 60,
            "period_start": t,
            "period_end": t + timedelta(minutes=1),
            "price_open_cents": None,
            "price_high_cents": None,
            "price_low_cents": None,
            "price_close_cents": (bid + ask) // 2 if bid is not None and ask is not None else None,
            "price_mean_cents": None,
            "price_close_is_carried_forward": False,
            "yes_bid_open_cents": bid,
            "yes_bid_high_cents": bid,
            "yes_bid_low_cents": bid,
            "yes_bid_close_cents": bid,
            "yes_ask_open_cents": ask,
            "yes_ask_high_cents": ask,
            "yes_ask_low_cents": ask,
            "yes_ask_close_cents": ask,
            "volume": 3,
            "open_interest": 10,
            "raw_payload_id": 1,
            "event_ticker": ticker.rsplit("-", 1)[0],
            "close_time": datetime.combine(t.date() + timedelta(days=1), datetime.min.time())
            + timedelta(hours=5),
            "floor_strike": None,
            "cap_strike": None,
            "strike_type": None,
            "data_quality_status": "ok",
            "station_id": "NYC",
            "variable": None,
            "target_date": None,
            "settlement_time": None,
            "value_at_close": None,
            "value_at_settlement": None,
            "latest_final_value": None,
            "kalshi_result": None,
            "kalshi_expiration_value": None,
            "settlement_label_status": None,
        }

    for variable in ("tmax_f", "tmin_f"):
        prelim = PRELIM[variable]
        c = int(y_rate[variable] * 100)
        for i in range(n_days):
            d = start + timedelta(days=i)
            e = _event_time(d)
            y = ((i + 1) * c) // 100 > (i * c) // 100  # Bresenham exact rate
            sign = 1 if variable == "tmax_f" else -1
            settle = prelim + sign * (2.0 if y else 0.0)

            base = f"SYN{'MAX' if variable == 'tmax_f' else 'MIN'}-{d.isoformat()}"
            neither = f"{base}-N"
            atrisk = f"{base}-A"
            locked = f"{base}-L"
            ladder = [
                (neither, "between", prelim - 1.5, prelim + 0.5)
                if variable == "tmax_f"
                else (neither, "between", prelim - 0.5, prelim + 1.5),
                (atrisk, "between", prelim + 0.5, prelim + 2.5)
                if variable == "tmax_f"
                else (atrisk, "between", prelim - 2.5, prelim - 0.5),
                (locked, "less", None, prelim - 1.5)
                if variable == "tmax_f"
                else (locked, "greater", prelim + 1.5, None),
            ]
            for ticker, stype, floor, cap in ladder:
                result_yes = h0017.region_yes(
                    stype,
                    Decimal(str(floor)) if floor is not None else None,
                    Decimal(str(cap)) if cap is not None else None,
                    Decimal(str(settle)),
                )
                lb_rows.append(
                    {
                        "market_ticker": ticker,
                        "station_id": "NYC",
                        "variable": variable,
                        "target_date": d,
                        "settlement_label_status": "resolved",
                        "kalshi_result": "yes" if result_yes else "no",
                        "value_at_settlement": settle,
                    }
                )
                rows = []
                if not (ticker == atrisk and i < drop_post_quotes_first):
                    p = price_cents[variable]
                    rows.append(candle(ticker, e + timedelta(minutes=60), p - 1, p + 1))
                rows.append(candle(ticker, e - timedelta(minutes=30), 4, 6))  # pre window
                rows.append(candle(ticker, e - timedelta(minutes=270), 4, 6))  # placebo pre
                rows.append(candle(ticker, e - timedelta(minutes=180), 4, 6))  # placebo post
                for row in rows:
                    row.update(
                        {
                            "floor_strike": floor,
                            "cap_strike": cap,
                            "strike_type": stype,
                            "variable": variable,
                            "target_date": d,
                            "kalshi_result": "yes" if result_yes else "no",
                            "value_at_settlement": settle,
                            "settlement_label_status": "resolved",
                        }
                    )
                    mp_rows.append(row)

            payload += 2
            iss_rows.append(
                {
                    "station_id": "NYC",
                    "variable": variable,
                    "value": prelim,
                    "observation_date": d,
                    "issuance_time": e,
                    "raw_payload_id": payload - 1,
                }
            )
            iss_rows.append(
                {
                    "station_id": "NYC",
                    "variable": variable,
                    "value": settle,
                    "observation_date": d,
                    "issuance_time": e + timedelta(hours=14),
                    "raw_payload_id": payload,
                }
            )
    for j, other in enumerate(("CHI", "DEN", "LAX")):
        payload += 1
        iss_rows.append(
            {
                "station_id": other,
                "variable": "tmax_f",
                "value": 70.0,
                "observation_date": start,
                "issuance_time": _event_time(start) + timedelta(minutes=j + 1),
                "raw_payload_id": payload,
            }
        )
    return pl.DataFrame(mp_rows), pl.DataFrame(lb_rows), pl.DataFrame(iss_rows)


def _pin_hashes(mp: pl.DataFrame, lb: pl.DataFrame, iss: pl.DataFrame) -> Any:
    class _Pins:
        def __enter__(self) -> None:
            self.ps = [
                patch.object(h0017, "MARKET_PRICES_HASH", h0017.frame_content_hash(mp)),
                patch.object(h0017, "LABELS_HASH", h0017.frame_content_hash(lb)),
                patch.object(h0017, "ISSUANCE_HASH", h0017.frame_content_hash(iss)),
            ]
            for p in self.ps:
                p.__enter__()

        def __exit__(self, *exc: object) -> None:
            for p in reversed(self.ps):
                p.__exit__(*exc)

    return _Pins()


def _write_dirs(
    mp: pl.DataFrame, lb: pl.DataFrame, iss: pl.DataFrame, tmp_path: Path
) -> tuple[Path, Path]:
    m_dir = tmp_path / "market"
    i_dir = tmp_path / "issuance"
    m_dir.mkdir()
    i_dir.mkdir()
    mp.write_parquet(m_dir / "market_prices.parquet")
    lb.write_parquet(m_dir / "settlement_labels.parquet")
    iss.write_parquet(i_dir / "observation_issuances.parquet")
    return m_dir, i_dir


# --- Strike semantics (PREREG Sec 4/10) --------------------------------------


def test_region_yes_all_types_and_boundaries() -> None:
    f, c = Decimal("80.5"), Decimal("82.5")
    assert h0017.region_yes("between", f, c, Decimal("81")) is True
    assert h0017.region_yes("between", f, c, Decimal("80.5")) is True  # inclusive
    assert h0017.region_yes("between", f, c, Decimal("80")) is False
    assert h0017.region_yes("greater", f, None, Decimal("81")) is True
    assert h0017.region_yes("greater", f, None, Decimal("80.5")) is False  # strict
    assert h0017.region_yes("less", None, c, Decimal("82")) is True
    assert h0017.region_yes("less", None, c, Decimal("82.5")) is False  # strict
    with pytest.raises(ValueError, match="strike_type"):
        h0017.region_yes("banana", f, c, Decimal("81"))


def _meta(ticker: str, stype: str, floor: float | None, cap: float | None) -> h0017.MarketMeta:
    return h0017.MarketMeta(
        ticker=ticker,
        variable="tmax_f",
        target_date=date(2026, 6, 1),
        strike_type=stype,
        floor=Decimal(str(floor)) if floor is not None else None,
        cap=Decimal(str(cap)) if cap is not None else None,
        kalshi_result="no",
        close_time=None,
        label_status="resolved",
    )


def test_adjacent_selection_tmax() -> None:
    ladder = [
        _meta("L1", "less", None, 76.5),
        _meta("N", "between", 78.5, 80.5),
        _meta("A1", "between", 80.5, 82.5),
        _meta("A2", "between", 82.5, 84.5),
        _meta("G", "greater", 84.5, None),
    ]
    at_risk, locked = h0017.select_adjacent(ladder, "tmax_f", Decimal("80"))
    assert at_risk is not None and at_risk.ticker == "A1"  # smallest floor above 80
    assert locked is not None and locked.ticker == "L1"
    assert h0017.at_risk_distance(at_risk, "tmax_f", Decimal("80")) == Decimal("0.5")


def test_adjacent_selection_tmin_mirrored() -> None:
    ladder = [
        h0017.MarketMeta(
            "A1",
            "tmin_f",
            date(2026, 6, 1),
            "between",
            Decimal("56.5"),
            Decimal("58.5"),
            "no",
            None,
            "resolved",
        ),
        h0017.MarketMeta(
            "N",
            "tmin_f",
            date(2026, 6, 1),
            "between",
            Decimal("58.5"),
            Decimal("60.5"),
            "no",
            None,
            "resolved",
        ),
        h0017.MarketMeta(
            "G",
            "tmin_f",
            date(2026, 6, 1),
            "greater",
            Decimal("60.5"),
            None,
            "no",
            None,
            "resolved",
        ),
        h0017.MarketMeta(
            "L2", "tmin_f", date(2026, 6, 1), "less", None, Decimal("54.5"), "no", None, "resolved"
        ),
    ]
    at_risk, locked = h0017.select_adjacent(ladder, "tmin_f", Decimal("60"))
    assert at_risk is not None and at_risk.ticker == "A1"  # largest cap below 60
    assert locked is not None and locked.ticker == "G"


def test_no_at_risk_candidate_returns_none() -> None:
    ladder = [_meta("G", "greater", 74.5, None)]  # region includes values <= prelim
    at_risk, _ = h0017.select_adjacent(ladder, "tmax_f", Decimal("80"))
    assert at_risk is None


# --- Window / price measurement (PREREG Sec 3/10) ----------------------------


def _c(t: datetime, bid: int | None, ask: int | None, cf: bool = False) -> h0017.Candle:
    return h0017.Candle(
        period_start=t,
        bid_close=bid,
        ask_close=ask,
        price_close=(bid + ask) // 2 if bid is not None and ask is not None else None,
        carried_forward=cf,
        volume=1,
    )


def test_window_midpoint_takes_last_fully_quoted() -> None:
    e = datetime(2026, 6, 1, 20, 35)
    candles = [
        _c(e + timedelta(minutes=20), 8, 12),
        _c(e + timedelta(minutes=60), 10, None),  # not fully quoted -> skipped
        _c(e + timedelta(minutes=90), 4, 8),
        _c(e + timedelta(minutes=130), 50, 60),  # outside window
    ]
    mid, chosen = h0017.window_midpoint(
        candles, e + timedelta(minutes=15), e + timedelta(minutes=120)
    )
    assert mid == Decimal("0.06")  # (4+8)/200
    assert chosen is not None and chosen.period_start == e + timedelta(minutes=90)


def test_window_midpoint_missing_when_no_quotes() -> None:
    e = datetime(2026, 6, 1, 20, 35)
    mid, chosen = h0017.window_midpoint([], e, e + timedelta(minutes=60))
    assert mid is None and chosen is None


def test_window_trade_price_skips_carried_forward() -> None:
    e = datetime(2026, 6, 1, 20, 35)
    candles = [
        _c(e + timedelta(minutes=30), 10, 12, cf=False),
        _c(e + timedelta(minutes=60), 20, 22, cf=True),  # carried forward -> skipped
    ]
    p = h0017.window_trade_price(candles, e, e + timedelta(minutes=120))
    assert p == Decimal("0.11")


# --- mean/CI and decision table (PREREG Sec 5/6/10) --------------------------


def test_mean_ci_hand_check() -> None:
    residuals = [Decimal("0.1"), Decimal("-0.1"), Decimal("0.1"), Decimal("-0.1")]
    mean, sd, lower, upper = h0017.mean_ci(residuals)
    assert mean == Decimal("0")
    # sd = sqrt(4*(0.01)/3) = 0.11547...; half = 1.96*sd/2
    assert abs(sd - Decimal("0.1154700538")) < Decimal("1E-9")
    assert abs((upper - lower) / 2 - h0017.Z_95 * sd / Decimal(2)) < Decimal("1E-20")


def _verdict_for(lower: str, upper: str) -> str:
    gates = h0017.GateOutcome(
        gates={}, all_gates_passed=True, inputs=None, cohort=None, exclusions=None
    )
    return h0017.evaluate(gates, Decimal(lower), Decimal(upper)).outcome


def test_decision_table_branches() -> None:
    assert _verdict_for("0.010", "0.120") == "OVERPRICED"
    assert _verdict_for("-0.120", "-0.010") == "UNDERPRICED"
    assert _verdict_for("-0.050", "0.050") == "EFFICIENT-WITHIN-MARGIN"
    assert _verdict_for("-0.120", "0.030") == "INCONCLUSIVE"
    # touching thresholds resolves to the less-decisive branch:
    assert _verdict_for("0.000000000000", "0.050") == "EFFICIENT-WITHIN-MARGIN"  # L==0 not >0
    assert _verdict_for("-0.075000000000", "0.050") == "INCONCLUSIVE"  # L==-margin not inside
    assert _verdict_for("-0.050", "0.075000000000") == "INCONCLUSIVE"
    gates = h0017.GateOutcome(
        gates={}, all_gates_passed=False, inputs=None, cohort=None, exclusions=None
    )
    assert h0017.evaluate(gates, None, None).outcome == "blocked"


def test_verify_config_matches_prereg_true_and_detects_drift() -> None:
    assert h0017.verify_config_matches_prereg() is True
    with patch.object(h0017, "EQUIVALENCE_MARGIN", Decimal("0.10")):
        assert h0017.verify_config_matches_prereg() is False
    with patch.object(h0017, "POST_END_MIN", 60):
        assert h0017.verify_config_matches_prereg() is False


# --- Gates -------------------------------------------------------------------


def test_gates_pass_on_valid_synthetic_inputs() -> None:
    mp, lb, iss = _build_inputs()
    with _pin_hashes(mp, lb, iss):
        outcome = h0017.run_gates(mp, lb, iss)
    assert outcome.all_gates_passed is True
    assert outcome.gates["g3_semantics"]["semantics_counts"]["disagree"] == 0
    assert outcome.gates["g4_cohort"]["n_primary"] == 200
    assert outcome.gates["g4_cohort"]["qualifying_events"] == 20


def test_g1_hash_tamper_detection() -> None:
    mp, lb, iss = _build_inputs(n_days=10)
    outcome = h0017.run_gates(mp, lb, iss)  # real pins, synthetic data
    assert outcome.gates["g1a_market_hashes_match"] is False
    assert outcome.gates["g2_invariants"] == h0017.NOT_EVALUATED


def test_g3_fails_on_semantics_disagreement() -> None:
    mp, lb, iss = _build_inputs(n_days=90)
    lb = lb.with_columns(
        pl.when(pl.int_range(pl.len()) < 30)
        .then(pl.lit("yes"))
        .otherwise(pl.col("kalshi_result"))
        .alias("kalshi_result")
    )
    with _pin_hashes(mp, lb, iss):
        outcome = h0017.run_gates(mp, lb, iss)
    assert outcome.gates["g3_semantics"]["pass"] is False
    assert outcome.all_gates_passed is False


def test_g4_blocks_on_thin_quote_coverage() -> None:
    mp, lb, iss = _build_inputs(n_days=100, drop_post_quotes_first=60)
    with _pin_hashes(mp, lb, iss):
        outcome = h0017.run_gates(mp, lb, iss)
    g4 = outcome.gates["g4_cohort"]
    assert g4["exclusion_counts_by_reason"]["no_post_quote"] == 120  # 60 per variable
    assert Decimal(g4["quote_coverage"]) == Decimal("0.4")
    assert outcome.all_gates_passed is False


# --- End-to-end run() --------------------------------------------------------


def test_run_end_to_end_efficient(tmp_path: Path) -> None:
    mp, lb, iss = _build_inputs(price_cents={"tmax_f": 10, "tmin_f": 10})
    m_dir, i_dir = _write_dirs(mp, lb, iss, tmp_path)
    with _pin_hashes(mp, lb, iss):
        results = h0017.run(m_dir, i_dir)
    assert results["primary_results"]["decision"]["outcome"] == "EFFICIENT-WITHIN-MARGIN"
    assert results["primary_results"]["cohort"]["n"] == 200
    h0017.validate_manifest(results)


def test_run_end_to_end_underpriced(tmp_path: Path) -> None:
    mp, lb, iss = _build_inputs(
        price_cents={"tmax_f": 1, "tmin_f": 1}, y_rate={"tmax_f": 0.15, "tmin_f": 0.15}
    )
    m_dir, i_dir = _write_dirs(mp, lb, iss, tmp_path)
    with _pin_hashes(mp, lb, iss):
        results = h0017.run(m_dir, i_dir)
    assert results["primary_results"]["decision"]["outcome"] == "UNDERPRICED"


def test_run_end_to_end_overpriced(tmp_path: Path) -> None:
    mp, lb, iss = _build_inputs(
        price_cents={"tmax_f": 30, "tmin_f": 30}, y_rate={"tmax_f": 0.05, "tmin_f": 0.05}
    )
    m_dir, i_dir = _write_dirs(mp, lb, iss, tmp_path)
    with _pin_hashes(mp, lb, iss):
        results = h0017.run(m_dir, i_dir)
    assert results["primary_results"]["decision"]["outcome"] == "OVERPRICED"


def test_run_controls_and_descriptives_present(tmp_path: Path) -> None:
    mp, lb, iss = _build_inputs()
    m_dir, i_dir = _write_dirs(mp, lb, iss, tmp_path)
    with _pin_hashes(mp, lb, iss):
        results = h0017.run(m_dir, i_dir)
    d = results["descriptive_results"]
    assert d["locked_no_control"]["priced"] == 200
    assert Decimal(d["locked_no_control"]["mean_post_price"]) == Decimal("0.1")
    assert d["placebo_reaction"]["event"]["n_with_both_windows"] == 200
    assert d["placebo_reaction"]["placebo"]["n_with_both_windows"] == 200
    # placebo windows sit at constant 5c quotes -> zero mean reaction
    assert Decimal(d["placebo_reaction"]["placebo"]["mean_abs_change"]) == Decimal("0")
    assert set(d["per_variable"].keys()) == {"tmax_f", "tmin_f"}
    assert d["exclusions"]["no_post_quote"] == 0


def test_run_blocked_produces_schema_complete_manifest(tmp_path: Path) -> None:
    mp, lb, iss = _build_inputs(n_days=10)
    m_dir, i_dir = _write_dirs(mp, lb, iss, tmp_path)
    with _pin_hashes(mp, lb, iss):
        results = h0017.run(m_dir, i_dir)  # n=20 < 80 -> blocked at G4
    assert results["primary_results"]["decision"]["outcome"] == "blocked"
    h0017.validate_manifest(results)


def test_run_twice_byte_identical(tmp_path: Path) -> None:
    mp, lb, iss = _build_inputs()
    m_dir, i_dir = _write_dirs(mp, lb, iss, tmp_path)
    with _pin_hashes(mp, lb, iss):
        first = h0017.run(m_dir, i_dir)
        second = h0017.run(m_dir, i_dir)
    assert json.dumps(first, sort_keys=True, default=str) == json.dumps(
        second, sort_keys=True, default=str
    )


def test_render_markdown_contains_decision_and_primary(tmp_path: Path) -> None:
    mp, lb, iss = _build_inputs()
    m_dir, i_dir = _write_dirs(mp, lb, iss, tmp_path)
    with _pin_hashes(mp, lb, iss):
        results = h0017.run(m_dir, i_dir)
    md = h0017.render_markdown(results)
    assert "## Decision" in md
    assert "EFFICIENT-WITHIN-MARGIN" in md
    assert "adjacent at-risk variable-days" in md
