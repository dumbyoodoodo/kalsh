"""SEA/PHX/MIA pilot extension registration: date rules, gate, isolation.

Injected clocks and synthetic deployment timestamps only. Nothing here queries
attempt outcomes -- which is the property the registration depends on, and is
asserted directly by ``test_no_attempt_outcome_query_during_registration``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from kalshi_weather.research import station_pilot_extension as ext

THIS_FILE = Path(__file__)
# Synthetic deployment validation instant (NOT a real deployment).
DEPLOY = datetime(2026, 8, 10, 18, 30, tzinfo=UTC)


# --- registration shape ------------------------------------------------------


def test_registration_created_before_any_attempt_outcome_existed() -> None:
    assert ext.REGISTRATION.attempt_outcomes_existed_at_registration is False
    assert ext.REGISTRATION.required_attempt_evidence is True


def test_seven_date_rule_is_frozen() -> None:
    assert ext.REQUIRED_COMPLETE_DATES == 7
    assert ext.REGISTRATION.required_complete_dates == 7


def test_deployment_date_is_not_hard_coded() -> None:
    """The registration must carry a start RULE, never a start date -- the
    deployment has not happened."""
    blob = str(ext.REGISTRATION.to_dict()).lower()
    for field in ("deployment_validated_at", "extension_start", "extension_end", "review_gate_at"):
        assert field not in ext.REGISTRATION.__dataclass_fields__, field
    assert "deployment_validated_at" in ext.REGISTRATION.start_rule
    # No ISO date literal for a start/gate anywhere in the frozen payload.
    assert "2026-08-1" not in blob and "2026-08-2" not in blob


def test_station_timezones_match_adr_0023() -> None:
    assert dict(ext.STATION_TIMEZONES) == {
        "SEA": "America/Los_Angeles",
        "PHX": "America/Phoenix",  # fixed UTC-7, never America/Denver
        "MIA": "America/New_York",
    }


def test_deterministic_registration_hash() -> None:
    a, b = ext.ExtensionRegistration(), ext.ExtensionRegistration()
    assert a.registration_hash() == b.registration_hash()
    assert a.registration_hash() == ext.REGISTRATION.to_dict()["registration_hash"]
    changed = ext.ExtensionRegistration(required_complete_dates=8)
    assert changed.registration_hash() != a.registration_hash()


# --- first included local date ----------------------------------------------


@pytest.mark.parametrize(
    ("station", "tz", "expected"),
    [
        ("SEA", "America/Los_Angeles", date(2026, 8, 11)),
        ("PHX", "America/Phoenix", date(2026, 8, 11)),
        ("MIA", "America/New_York", date(2026, 8, 11)),
    ],
)
def test_first_included_local_date_per_station(station: str, tz: str, expected: date) -> None:
    assert ext.first_included_local_date(DEPLOY, tz) == expected, station


def test_partially_instrumented_deployment_date_is_excluded() -> None:
    """The deployment day is never included: its local midnight is at or before
    the validation instant, so pre-deployment hours could leak in."""
    for _code, tz in ext.STATION_TIMEZONES:
        first = ext.first_included_local_date(DEPLOY, tz)
        deploy_local_date = DEPLOY.astimezone(__import__("zoneinfo").ZoneInfo(tz)).date()
        assert first > deploy_local_date


def test_deployment_just_before_local_midnight_still_excludes_that_day() -> None:
    """23:59 local on day D still excludes D -- D was not a complete date."""
    late = datetime(2026, 8, 11, 6, 59, tzinfo=UTC)  # 23:59 Aug 10 in Los Angeles
    assert ext.first_included_local_date(late, "America/Los_Angeles") == date(2026, 8, 11)


def test_naive_deployment_timestamp_rejected() -> None:
    with pytest.raises(ext.ExtensionRegistrationError, match="timezone-aware"):
        ext.first_included_local_date(datetime(2026, 8, 10, 18, 30), "America/Phoenix")


def test_seven_consecutive_dates_per_station() -> None:
    for _code, tz in ext.STATION_TIMEZONES:
        dates = ext.included_local_dates(DEPLOY, tz)
        assert len(dates) == 7
        assert dates == tuple(dates[0] + timedelta(days=i) for i in range(7))


# --- DST and offsets ---------------------------------------------------------


def test_phx_has_no_dst_shift_across_a_transition() -> None:
    """PHX is UTC-7 year round: its local-date end offset never changes."""
    summer = ext.local_date_end_utc(date(2026, 7, 15), "America/Phoenix")
    winter = ext.local_date_end_utc(date(2026, 12, 15), "America/Phoenix")
    assert summer.hour == winter.hour == 7


def test_dst_observing_stations_shift_but_phx_does_not() -> None:
    summer_sea = ext.local_date_end_utc(date(2026, 7, 15), "America/Los_Angeles")
    winter_sea = ext.local_date_end_utc(date(2026, 12, 15), "America/Los_Angeles")
    assert summer_sea.hour == 7 and winter_sea.hour == 8  # PDT -> PST
    summer_mia = ext.local_date_end_utc(date(2026, 7, 15), "America/New_York")
    winter_mia = ext.local_date_end_utc(date(2026, 12, 15), "America/New_York")
    assert summer_mia.hour == 4 and winter_mia.hour == 5  # EDT -> EST


# --- review gate -------------------------------------------------------------


def test_review_gate_is_the_latest_seventh_date_end() -> None:
    gate = ext.extension_review_gate(DEPLOY)
    ends = [
        ext.local_date_end_utc(ext.included_local_dates(DEPLOY, tz)[-1], tz)
        for _c, tz in ext.STATION_TIMEZONES
    ]
    assert gate == max(ends)
    assert gate > min(ends), "gate must not be the earliest station's end"


def test_review_gate_covers_every_station_seventh_date() -> None:
    gate = ext.extension_review_gate(DEPLOY)
    for _code, tz in ext.STATION_TIMEZONES:
        seventh_end = ext.local_date_end_utc(ext.included_local_dates(DEPLOY, tz)[-1], tz)
        assert gate >= seventh_end


def test_gate_is_deterministic() -> None:
    assert ext.extension_review_gate(DEPLOY) == ext.extension_review_gate(DEPLOY)


# --- readiness ---------------------------------------------------------------


def test_before_deployment_is_waiting_with_no_dates_or_gate() -> None:
    state = ext.extension_state(now=DEPLOY, deployment_validated_at=None)
    assert state is ext.ExtensionState.WAITING_FOR_ATTEMPT_ATTRIBUTION_DEPLOYMENT


def test_after_deployment_before_gate_is_collecting() -> None:
    state = ext.extension_state(now=DEPLOY + timedelta(days=3), deployment_validated_at=DEPLOY)
    assert state is ext.ExtensionState.COLLECTING_EXTENSION


def test_exactly_at_gate_is_ready() -> None:
    gate = ext.extension_review_gate(DEPLOY)
    assert (
        ext.extension_state(now=gate, deployment_validated_at=DEPLOY)
        is ext.ExtensionState.READY_FOR_EXTENSION_REVIEW
    )


def test_one_second_before_gate_is_not_ready() -> None:
    gate = ext.extension_review_gate(DEPLOY)
    assert (
        ext.extension_state(now=gate - timedelta(seconds=1), deployment_validated_at=DEPLOY)
        is ext.ExtensionState.COLLECTING_EXTENSION
    )


def test_naive_now_rejected() -> None:
    with pytest.raises(ext.ExtensionRegistrationError, match="timezone-aware"):
        ext.extension_state(now=datetime(2026, 8, 20), deployment_validated_at=DEPLOY)


# --- original artifact -------------------------------------------------------


def test_original_artifact_hash_required_and_matches_on_disk() -> None:
    artifact = Path("docs/research") / ext.ORIGINAL_REVIEW_ARTIFACT_NAME
    assert artifact.is_file(), "the immutable August 4 artifact must exist"
    assert ext.verify_original_artifact(artifact.read_bytes()) is True


def test_tampered_original_artifact_fails_closed() -> None:
    assert ext.verify_original_artifact(b"tampered") is False


def test_original_verdict_recorded_verbatim() -> None:
    assert ext.ORIGINAL_REVIEW_VERDICT == "PILOT_COLLECTION_EXTENSION_REQUIRED"


# --- outcome isolation -------------------------------------------------------


def test_no_performance_fields_in_registration() -> None:
    blob = str(ext.REGISTRATION.to_dict()).lower()
    parts = {p for p in "".join(c if c.isalnum() else " " for c in blob).split()}
    # The forbidden_analysis list intentionally NAMES these to forbid them, so
    # scan the field NAMES rather than the values.
    for name in ext.ExtensionRegistration.__dataclass_fields__:
        for token in ("brier", "calibration", "profit", "pnl", "rank", "accuracy", "sharpe"):
            assert token not in name.lower(), name
    assert "station_ranking" in ext.FORBIDDEN_ANALYSIS
    assert parts  # sanity


def test_forbidden_analysis_is_complete() -> None:
    for required in (
        "forecast_error",
        "brier_score",
        "calibration",
        "market_prices",
        "liquidity",
        "pnl",
        "expected_value",
        "profitability",
        "station_ranking",
        "hypothesis_support",
    ):
        assert required in ext.FORBIDDEN_ANALYSIS, required


def test_legacy_and_missing_evidence_unacceptable_in_extension() -> None:
    assert set(ext.UNACCEPTABLE_EXTENSION_EVIDENCE) == {"LEGACY_UNKNOWN", "EVIDENCE_MISSING"}


def test_no_attempt_outcome_query_during_registration() -> None:
    """Registration must be computable with no access to outcomes: the module
    imports nothing that could read them."""
    source = Path(ext.__file__).read_text()
    for line in source.splitlines():
        s = line.strip()
        if not (s.startswith("import ") or s.startswith("from ")):
            continue
        for banned in (
            "sqlalchemy",
            "psycopg",
            "weather_attempts",
            "weather_collection_attempts",
            "experiments",
            "h0019",
            "h0020",
            "paper",
            "httpx",
            "requests",
        ):
            assert banned not in s, f"registration imports {banned}: {s}"


def test_no_registry_mutation_surface() -> None:
    source = Path(ext.__file__).read_text()
    for banned in ("save_weather_station", "list_stations", "DELETE", "UPDATE ", "INSERT "):
        assert banned not in source, banned


def test_no_production_write_surface() -> None:
    source = Path(ext.__file__).read_text()
    for banned in ("open(", "write_text", "write_bytes", "mkdir", "Path("):
        assert banned not in source, f"{banned} — registration module must not write"
