"""Provenance inclusion policy (dataset/provenance.py, ADR 0014).

Pins the deterministic classification and the production-only liquidity rule
that keep demo/NULL liquidity out of the canonical dataset.
"""

import pytest

from kalshi_weather.dataset.provenance import (
    HISTORICAL_UNKNOWN,
    VERIFIED_DEMO,
    VERIFIED_PRODUCTION,
    EnvironmentPolicy,
    ProvenanceCoverage,
    classify_provenance,
    liquidity_admissible,
    price_admissible,
)


@pytest.mark.parametrize(
    "env, table, expected",
    [
        ("production", "trades", VERIFIED_PRODUCTION),
        ("demo", "trades", VERIFIED_DEMO),
        ("unknown", "trades", HISTORICAL_UNKNOWN),
        (None, "trades", HISTORICAL_UNKNOWN),
        (None, "market_snapshots", HISTORICAL_UNKNOWN),
        # deterministic: sole candlestick writer is always-production price-sync
        (None, "market_candlesticks", VERIFIED_PRODUCTION),
        ("production", "market_candlesticks", VERIFIED_PRODUCTION),
        ("demo", "market_candlesticks", VERIFIED_DEMO),
        ("unknown", "market_candlesticks", HISTORICAL_UNKNOWN),
    ],
)
def test_classify_provenance(env, table, expected) -> None:  # type: ignore[no-untyped-def]
    assert classify_provenance(env, table=table) == expected


def test_liquidity_is_production_only() -> None:
    p = EnvironmentPolicy()
    assert liquidity_admissible("production", p) is True
    assert liquidity_admissible("demo", p) is False
    assert liquidity_admissible("unknown", p) is False
    assert liquidity_admissible(None, p) is False
    # even on the deterministic-production candle table, a NULL row's LIQUIDITY
    # is not admissible (its price is; its volume is not).
    assert liquidity_admissible(None, p) is False


def test_price_admits_production_and_deterministic_null_candles() -> None:
    p = EnvironmentPolicy()
    assert price_admissible("production", p, table="market_candlesticks") is True
    assert price_admissible(None, p, table="market_candlesticks") is True  # deterministic
    assert price_admissible("demo", p, table="market_candlesticks") is False
    assert price_admissible("unknown", p, table="market_candlesticks") is False
    # NULL is NOT price-admissible on a mixed-writer table
    assert price_admissible(None, p, table="market_snapshots") is False


def test_policy_can_disable_deterministic_null_candles() -> None:
    strict = EnvironmentPolicy(price_includes_deterministic_null_candles=False)
    assert price_admissible(None, strict, table="market_candlesticks") is False
    assert price_admissible("production", strict, table="market_candlesticks") is True


def test_coverage_counts_by_environment_and_provenance() -> None:
    p = EnvironmentPolicy()
    cov = ProvenanceCoverage()
    for env in ["production", "production", "demo", None, "unknown"]:
        cov.record("trades", env, p)
    cov.record("market_candlesticks", None, p)
    rep = cov.to_report()
    assert rep["by_environment"]["trades"] == {"production": 2, "demo": 1, "NULL": 1, "unknown": 1}
    assert rep["by_provenance"]["trades"][VERIFIED_PRODUCTION] == 2
    assert rep["by_provenance"]["trades"][HISTORICAL_UNKNOWN] == 2  # NULL + unknown
    assert rep["by_provenance"]["trades"][VERIFIED_DEMO] == 1
    # 3 non-production trades excluded from liquidity
    assert rep["liquidity_rows_excluded_non_production"]["trades"] == 3
    # NULL candle counts as verified_production provenance
    assert rep["by_provenance"]["market_candlesticks"][VERIFIED_PRODUCTION] == 1


def test_policy_manifest_is_serializable() -> None:
    m = EnvironmentPolicy().to_manifest()
    assert m["liquidity_environments"] == ["production"]
    assert "0014" in m["policy_reference"]
