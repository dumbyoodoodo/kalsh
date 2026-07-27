"""H0020 close-time preflight scopes: frozen stage windows, gap exclusion,
as-of truncation, and metadata-only isolation. Synthetic only."""

import re
from datetime import date
from pathlib import Path

import pytest

from kalshi_weather.experiments.h0020_readiness import (
    CLOSE_MARGIN_DAYS,
    PREFLIGHT_STAGES,
    UnknownStageError,
    stage_close_ranges,
)

GAP_START, GAP_END = date(2026, 8, 12), date(2026, 8, 25)


def test_stage_windows_match_frozen_spec() -> None:
    assert PREFLIGHT_STAGES["train"] == (date(2026, 7, 21), date(2026, 8, 11))
    assert PREFLIGHT_STAGES["validation"] == (date(2026, 8, 26), date(2026, 9, 8))
    assert PREFLIGHT_STAGES["initial-test"] == (date(2026, 9, 9), date(2026, 9, 22))
    assert PREFLIGHT_STAGES["extension-1"] == (date(2026, 9, 23), date(2026, 9, 29))
    assert PREFLIGHT_STAGES["extension-2"] == (date(2026, 9, 30), date(2026, 10, 6))


def test_single_stage_ranges() -> None:
    (start, end), = stage_close_ranges("train")
    assert start == date(2026, 7, 21)
    assert end == date(2026, 8, 11 + CLOSE_MARGIN_DAYS)
    (v_start, _), = stage_close_ranges("validation")
    assert v_start == date(2026, 8, 26)


def test_excluded_h0019_gap_never_in_any_stage() -> None:
    for stage in [*PREFLIGHT_STAGES, "full"]:
        for s, _e in stage_close_ranges(stage):
            assert not (GAP_START <= s <= GAP_END), (stage, s)
    # target windows themselves never start in the gap
    for name, (t_start, t_end) in PREFLIGHT_STAGES.items():
        assert not (GAP_START <= t_start <= GAP_END), name
        assert not (GAP_START <= t_end <= GAP_END), name


def test_full_scope_is_union_of_stages_without_overlapping_targets() -> None:
    full = stage_close_ranges("full")
    assert len(full) == len(PREFLIGHT_STAGES)
    # target windows are pairwise disjoint (stages cannot share a market's
    # target date; the close-range margin overlap is a scan margin only)
    import itertools

    windows = sorted(PREFLIGHT_STAGES.values())
    for (a_s, a_e), (b_s, b_e) in itertools.pairwise(windows):
        assert a_e < b_s, ((a_s, a_e), (b_s, b_e))


def test_extension_stages_do_not_enter_initial_test() -> None:
    (_i_start, _), = stage_close_ranges("initial-test")
    (e1_start, _), = stage_close_ranges("extension-1")
    assert e1_start == date(2026, 9, 23)  # strictly after initial test end
    (e2_start, _), = stage_close_ranges("extension-2")
    assert e2_start == date(2026, 9, 30)


def test_as_of_truncation_and_not_yet_observable() -> None:
    # as-of mid-train: train range capped at as_of+1; future stages empty
    capped = stage_close_ranges("train", as_of=date(2026, 7, 28))
    (s, e), = capped
    assert s == date(2026, 7, 21) and e == date(2026, 7, 29)
    assert stage_close_ranges("validation", as_of=date(2026, 7, 28)) == []
    assert stage_close_ranges("extension-2", as_of=date(2026, 7, 28)) == []
    full = stage_close_ranges("full", as_of=date(2026, 7, 28))
    assert len(full) == 1  # only train observable so far


def test_unknown_stage_fails_closed() -> None:
    with pytest.raises(UnknownStageError):
        stage_close_ranges("vibes")


def test_preflight_cli_metadata_only_isolation() -> None:
    """The h0020 preflight CLI path must reference no outcome/model fields:
    the guard input carries only (ticker, id, observed_at, close_time,
    environment) and the disclaimer must be present."""
    src = (Path(__file__).resolve().parents[2] / "src/kalshi_weather/cli.py").read_text()
    block = src.split("def _preflight_h0020_close_time")[1].split("\n\n\n")[0]
    banned = re.compile(
        r"kalshi_result|expiration_value|price_close|yes_bid|brier|prediction|fit_",
        re.IGNORECASE,
    )
    m = banned.search(block)
    assert m is None, f"h0020 preflight touches outcome/model field: {m.group(0)!r}"
    assert "does NOT mean" in src  # disclaimer wired into output
    assert "readiness state" in block or "readiness" in block  # separation documented
