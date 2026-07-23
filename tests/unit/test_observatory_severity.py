from kalshi_weather.observatory.severity import (
    DOMAINS,
    Finding,
    Severity,
    findings_by_domain,
    findings_by_severity,
    highest,
    overall_severity,
)


def test_highest_empty_is_info() -> None:
    assert highest([]) == Severity.INFO


def test_highest_picks_max_rank_regardless_of_order() -> None:
    assert highest([Severity.WARNING, Severity.INFO]) == Severity.WARNING
    assert highest([Severity.CRITICAL, Severity.WARNING, Severity.INFO]) == Severity.CRITICAL
    assert highest([Severity.INFO, Severity.INFO]) == Severity.INFO


def test_domains_cover_five_streams_plus_platform_and_backup() -> None:
    assert set(DOMAINS) == {
        "forecast",
        "observation",
        "market",
        "trade",
        "candle",
        "platform",
        "backup",
    }


def _finding(domain: str, severity: Severity, check: str = "x") -> Finding:
    return Finding(domain=domain, check=check, severity=severity, count=1, message="m")


def test_overall_severity_rolls_up_findings() -> None:
    findings = [
        _finding("forecast", Severity.INFO),
        _finding("market", Severity.WARNING),
    ]
    assert overall_severity(findings) == Severity.WARNING


def test_overall_severity_empty_is_info() -> None:
    assert overall_severity([]) == Severity.INFO


def test_findings_by_severity_filters() -> None:
    findings = [
        _finding("forecast", Severity.INFO),
        _finding("market", Severity.CRITICAL),
        _finding("trade", Severity.CRITICAL),
    ]
    assert findings_by_severity(findings, Severity.CRITICAL) == findings[1:]


def test_findings_by_domain_filters() -> None:
    findings = [
        _finding("forecast", Severity.INFO),
        _finding("market", Severity.INFO),
    ]
    assert findings_by_domain(findings, "market") == [findings[1]]


def test_finding_to_dict_is_json_shaped() -> None:
    finding = Finding(
        domain="forecast",
        check="archive_continuity_forecast",
        severity=Severity.WARNING,
        count=2,
        message="2 gaps",
        samples=("a", "b"),
    )
    assert finding.to_dict() == {
        "domain": "forecast",
        "check": "archive_continuity_forecast",
        "severity": "warning",
        "count": 2,
        "message": "2 gaps",
        "samples": ["a", "b"],
    }


def test_finding_to_dict_default_samples_is_empty_list() -> None:
    finding = Finding(domain="platform", check="x", severity=Severity.INFO, count=0, message="m")
    assert finding.to_dict()["samples"] == []
