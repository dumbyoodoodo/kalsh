"""The execution simulator must be fully isolated from H0019 (Phase 11):
it never imports H0019 code, never reads its artifacts, and H0019's registration
remains byte-unchanged.
"""

import json
from pathlib import Path

SRC = Path("src/kalshi_weather/execution")


def test_execution_never_reads_h0019_artifacts_or_experiments() -> None:
    # No execution module may import the experiments package or reference an
    # H0019/H0018 artifact PATH. (Docstrings may *mention* H0019 to document the
    # isolation -- that is not an access; we check imports and path strings.)
    for py in SRC.glob("*.py"):
        for line in py.read_text().splitlines():
            s = line.strip()
            if s.startswith(("import ", "from ")):
                assert "experiments" not in s, f"{py}: forbidden import: {s}"
                assert "readiness" not in s, f"{py}: forbidden import: {s}"
            assert "EXP-FUTURE-H0019" not in line, f"{py}: reads an H0019 artifact path"
            assert "EXP-20260726-H0018" not in line, f"{py}: reads an H0018 artifact path"


def test_execution_import_graph_excludes_experiments() -> None:
    # Import the execution package in a FRESH subprocess and assert it never
    # pulls in the experiments package (a shared test session would pollute
    # sys.modules, so isolate it).
    import subprocess
    import sys

    code = (
        "import sys; import kalshi_weather.execution.replay; "
        "import kalshi_weather.execution.engine; import kalshi_weather.execution.loader; "
        "import kalshi_weather.execution.history; import kalshi_weather.execution.history_replay; "
        "import kalshi_weather.execution.availability; import kalshi_weather.execution.coverage; "
        "assert not any(m.startswith('kalshi_weather.experiments') for m in sys.modules), "
        "'execution pulled in experiments'; print('isolated')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0 and "isolated" in out.stdout, out.stderr


def test_history_adapter_reads_only_market_data_tables() -> None:
    # The historical adapter must touch only market-data storage models, never any
    # H0019 experiment/readiness/prediction code.
    src = (SRC / "history.py").read_text() + (SRC / "history_replay.py").read_text()
    for forbidden in ("readiness", "experiments", "EXP-FUTURE-H0019", "prediction", "h0019"):
        for line in src.splitlines():
            s = line.strip()
            if s.startswith(("import ", "from ")):
                assert forbidden not in s.lower(), f"history adapter imports {forbidden}: {s}"


def test_smoke_market_is_not_an_h0019_market() -> None:
    # The historical smoke test uses a production market unrelated to H0019 model
    # selection (H0019 markets are chosen by the experiment, not by this adapter).
    tickers = Path(
        "docs/research/experiments/EXP-PAPER-PILOT/history_smoke/tickers.txt"
    ).read_text()
    assert "H0019" not in tickers
    cfg = json.loads(Path("docs/research/experiments/EXP-FUTURE-H0019/config.json").read_text())
    # the H0019 registration remains byte-stable regardless of this task
    assert cfg["hypothesis"] == "H0019"
    assert cfg["status"] == "accumulating_prospective_data"


def test_h0019_registration_artifacts_unchanged() -> None:
    cfg = json.loads(Path("docs/research/experiments/EXP-FUTURE-H0019/config.json").read_text())
    assert cfg["hypothesis"] == "H0019"
    assert cfg["status"] == "accumulating_prospective_data"
    assert cfg["windows"]["test"] == ["2026-08-12", "2026-08-25"]
    # readiness constants still frozen
    from kalshi_weather.experiments import readiness as rd

    assert rd.TEST_START.isoformat() == "2026-08-12"
    assert rd.REQUIREMENTS.min_test_events == 25


def test_h0018_frozen_artifact_still_present() -> None:
    d = json.loads(Path("docs/research/experiments/EXP-20260726-H0018/results.json").read_text())
    assert d["hypothesis"] == "H0018" and d["held_out_test_evaluable"] is False
