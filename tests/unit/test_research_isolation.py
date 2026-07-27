"""The research workbench must stay hypothesis-agnostic: no H0019 coupling,
no frozen-experiment file access, no wall-clock dependence."""

import re
from pathlib import Path

RESEARCH_DIR = Path(__file__).resolve().parents[2] / "src" / "kalshi_weather" / "research"


def research_sources() -> list[tuple[Path, str]]:
    files = sorted(RESEARCH_DIR.glob("*.py"))
    assert files, f"no research sources found under {RESEARCH_DIR}"
    return [(p, p.read_text()) for p in files]


def test_no_h0019_or_frozen_experiment_references() -> None:
    banned = re.compile(r"h0019|EXP-FUTURE|EXP-2026|readiness", re.IGNORECASE)
    for path, text in research_sources():
        match = banned.search(text)
        assert match is None, f"{path.name} references frozen experiment state: {match.group(0)!r}"


def test_no_experiment_runner_or_hypothesis_imports() -> None:
    # baseline.py (pure generic metrics) is the only permitted experiments import.
    pattern = re.compile(
        r"from kalshi_weather\.experiments(?:\.(\w+))?\s+import"
        r"|import kalshi_weather\.experiments(?:\.(\w+))?"
    )
    for path, text in research_sources():
        for m in pattern.finditer(text):
            module = m.group(1) or m.group(2)
            assert module == "baseline", (
                f"{path.name} imports experiments.{module or '<pkg>'}; only the generic "
                f"metrics in experiments.baseline are permitted"
            )


def test_no_wall_clock_or_ambient_randomness() -> None:
    banned = re.compile(
        r"datetime\.now|date\.today|time\.time\(|utcnow|np\.random\.seed|default_rng\(\)"
    )
    for path, text in research_sources():
        match = banned.search(text)
        assert match is None, f"{path.name} uses ambient time/randomness: {match.group(0)!r}"


def test_no_filesystem_paths_into_docs_research() -> None:
    # The ledger API takes caller-supplied paths; nothing may hardcode a path
    # into the experiments/registration tree.
    banned = re.compile(r"docs/research")
    for path, text in research_sources():
        assert banned.search(text) is None, f"{path.name} hardcodes a docs/research path"
