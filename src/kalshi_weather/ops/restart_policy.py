"""Restart policy for the long-running collector service.

This module is the single source of truth for which repository changes
require restarting the running collector (``ops run`` under launchd — see
``docs/runbooks/collector_service.md``). It answers three questions:

1. Which paths affect the running collector, and how severely (``RULES``)?
2. What code is the current collector actually running (a *manifest* of
   content hashes, written by ``scripts/service/launch.py`` at each service
   start)?
3. Does the working tree differ from the running code in a way that
   requires a restart (``compare`` / ``ops restart-check``)?

Design notes:

- Comparison is content-hash based, not git-SHA based, so uncommitted edits
  are detected. Git information is recorded for context only.
- The manifest stores repo-relative paths and SHA-256 digests only — never
  file contents. ``.env`` and ``secrets/`` are hashed so credential rotation
  is detected, but no key material ever leaves the repo.
- Paths not matched by any rule default to RESTART: a false "restart
  required" costs one graceful restart; a false "not required" leaves stale
  code collecting the scientific record.
- Nothing in this module restarts anything. It only classifies and reports;
  the operator acts via ``scripts/service/check_restart.sh``.
"""

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import IntEnum
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = 1
GIT_TIMEOUT_SECONDS = 10

#: Directory names never descended into. Hidden directories (``.git``,
#: ``.venv``, ``.pytest_cache``, …) are pruned wholesale; hidden *files*
#: (``.env``!) are still considered.
PRUNED_DIR_NAMES = frozenset({"__pycache__", "node_modules", "data"})


class Action(IntEnum):
    """What a change to a path demands of the running service.

    Ordered by severity so the verdict for a set of changes is ``max()``.
    """

    NONE = 0  # not loaded by the collector process; no action
    RESTART = 1  # picked up by scripts/service/restart.sh (graceful)
    REINSTALL = 2  # plist/entry-script layer; re-run scripts/service/install.sh


@dataclass(frozen=True)
class Rule:
    """First-match-wins classification rule.

    ``pattern`` is matched with :func:`fnmatch.fnmatch` against the
    repo-relative POSIX path; note ``*`` crosses ``/``, so ``src/*`` matches
    the whole subtree.
    """

    pattern: str
    action: Action
    reason: str


#: The policy. Ordered; the first matching rule wins. Documented for
#: operators in docs/runbooks/operations.md ("Collector restart policy"),
#: which defers to this table as authoritative.
RULES: tuple[Rule, ...] = (
    # Service layer: run.sh/launch.py/preflight are re-read at every start,
    # but install.sh also generates the launchd plists — conservatively
    # treat the whole directory as the reinstall layer (install.sh is
    # idempotent and cheap).
    Rule("scripts/service/*", Action.REINSTALL, "service entry/plist layer; re-run install.sh"),
    # Everything under src/ is importable by the collector process
    # (launch.py runs kalshi_weather.cli, which imports ingestion, weather,
    # kalshi auth/client, storage, config, settlement, ops — effectively the
    # whole package, including alembic migrations under src/kalshi_weather/
    # alembic; apply migrations *before* restarting).
    Rule("src/*", Action.RESTART, "collector code, imported by the running process"),
    Rule(".env", Action.RESTART, "runtime environment/credentials, read once at start"),
    Rule(".env.*", Action.NONE, "template/backup env files; not read by Settings"),
    Rule("secrets/*", Action.RESTART, "key material, loaded once at start"),
    Rule("config/*", Action.RESTART, "runtime configuration, read at start"),
    Rule("pyproject.toml", Action.RESTART, "dependency change; run `uv sync` before restarting"),
    Rule("uv.lock", Action.RESTART, "dependency change; run `uv sync` before restarting"),
    Rule("alembic.ini", Action.RESTART, "migration config; run `alembic upgrade head` first"),
    # Postgres itself is applied with `docker compose up -d`; the collector
    # reconnects/retries on its own (see preflight + cycle retry), so a
    # compose change needs no collector restart.
    Rule("docker-compose.yml", Action.NONE, "infrastructure; apply via docker compose"),
    Rule("docs/*", Action.NONE, "documentation/research record; not executed"),
    Rule("tests/*", Action.NONE, "test code; not part of the running service"),
    Rule("notebooks/*", Action.NONE, "analysis notebooks; not part of the running service"),
    Rule("scripts/*", Action.NONE, "operator/experiment scripts, run on demand"),
    Rule("Makefile", Action.NONE, "developer tooling"),
    Rule("*.md", Action.NONE, "documentation"),
    Rule(".gitignore", Action.NONE, "developer tooling"),
    Rule(".python-version", Action.NONE, "interpreter pin; takes effect via uv sync/reinstall"),
)

DEFAULT_RULE = Rule(
    "*", Action.RESTART, "unclassified path — conservative default (extend RULES if wrong)"
)


def classify(rel_path: str) -> Rule:
    """Return the first matching rule for a repo-relative POSIX path."""
    for rule in RULES:
        if fnmatch(rel_path, rule.pattern):
            return rule
    return DEFAULT_RULE


def _iter_relevant_files(repo_root: Path) -> list[str]:
    """Repo-relative POSIX paths of every file whose change would matter
    (``classify() != NONE``), pruning hidden and cache directories."""
    relevant: list[str] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and d not in PRUNED_DIR_NAMES
        )
        for name in sorted(filenames):
            rel = (Path(dirpath) / name).relative_to(repo_root).as_posix()
            if classify(rel).action is not Action.NONE:
                relevant.append(rel)
    return relevant


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_info(repo_root: Path) -> tuple[str | None, bool | None]:
    """(HEAD commit, working-tree-dirty) — both None when git is unusable.
    Context only; never load-bearing for the verdict."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None, None
    return head, bool(status.strip())


def build_manifest(repo_root: Path) -> dict[str, Any]:
    """Snapshot of everything restart-relevant in the working tree."""
    commit, dirty = _git_info(repo_root)
    return {
        "schema": MANIFEST_SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        "git_commit": commit,
        "git_dirty": dirty,
        "pid": os.getpid(),
        "files": {rel: _sha256(repo_root / rel) for rel in _iter_relevant_files(repo_root)},
    }


def write_manifest(repo_root: Path, manifest_path: Path) -> dict[str, Any]:
    """Write the running-code manifest (called at service start, after the
    single-instance lock is held). Atomic rename so a crash mid-write never
    leaves a truncated manifest."""
    manifest = build_manifest(repo_root)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = manifest_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n")
    tmp.replace(manifest_path)
    return manifest


@dataclass(frozen=True)
class Change:
    path: str
    kind: str  # "modified" | "added" | "removed"
    action: Action
    reason: str


@dataclass(frozen=True)
class RestartReport:
    verdict: Action | None  #: None => unknown (no manifest to compare against)
    changes: tuple[Change, ...] = ()
    manifest: dict[str, Any] | None = None
    current_git: tuple[str | None, bool | None] = (None, None)
    notes: tuple[str, ...] = field(default=())

    @property
    def headline(self) -> str:
        if self.verdict is None:
            return "Collector restart state UNKNOWN (no manifest recorded)"
        if self.verdict is Action.REINSTALL:
            return "Collector service REINSTALL required"
        if self.verdict is Action.RESTART:
            return "Collector restart required"
        return "Collector restart not required"

    @property
    def exit_code(self) -> int:
        return {None: 12, Action.REINSTALL: 11, Action.RESTART: 10, Action.NONE: 0}[self.verdict]


def compare(repo_root: Path, manifest_path: Path) -> RestartReport:
    """Diff the working tree against the running collector's manifest."""
    current_git = _git_info(repo_root)
    if not manifest_path.exists():
        return RestartReport(
            verdict=None,
            current_git=current_git,
            notes=(
                f"no manifest at {manifest_path} — the running collector predates "
                "restart tracking (or the service was never installed); restart once "
                "to establish a baseline",
            ),
        )
    manifest = json.loads(manifest_path.read_text())
    recorded: dict[str, str] = manifest.get("files", {})
    current = {rel: _sha256(repo_root / rel) for rel in _iter_relevant_files(repo_root)}

    changes: list[Change] = []
    for rel in sorted(recorded.keys() | current.keys()):
        if rel not in current:
            kind = "removed"
        elif rel not in recorded:
            kind = "added"
        elif recorded[rel] != current[rel]:
            kind = "modified"
        else:
            continue
        rule = classify(rel)
        changes.append(Change(path=rel, kind=kind, action=rule.action, reason=rule.reason))

    verdict = max((c.action for c in changes), default=Action.NONE)
    notes = []
    pid = manifest.get("pid")
    if isinstance(pid, int) and not _pid_alive(pid):
        notes.append(
            f"manifest was written by pid {pid}, which is no longer running — "
            "is the service stopped? (scripts/service/status.sh)"
        )
    return RestartReport(
        verdict=verdict,
        changes=tuple(changes),
        manifest=manifest,
        current_git=current_git,
        notes=tuple(notes),
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def format_report(report: RestartReport) -> str:
    """Human-readable report; the last line is always the headline verdict."""
    lines: list[str] = []
    if report.manifest is not None:
        commit = report.manifest.get("git_commit")
        commit_s = (commit or "?")[:9] + (" (dirty)" if report.manifest.get("git_dirty") else "")
        lines.append(
            f"running:  commit {commit_s}, started {report.manifest.get('created_at')}, "
            f"pid {report.manifest.get('pid')}"
        )
    head, dirty = report.current_git
    lines.append(f"current:  commit {(head or '?')[:9]}{' (dirty working tree)' if dirty else ''}")
    for note in report.notes:
        lines.append(f"note:     {note}")
    if report.changes:
        lines.append("changes affecting the running collector:")
        for change in report.changes:
            tag = change.action.name.lower()
            lines.append(f"  [{tag:9}] {change.path} ({change.kind}) — {change.reason}")
    elif report.verdict is Action.NONE:
        lines.append("no restart-relevant differences between running code and working tree")
    lines.append(report.headline)
    if report.verdict is Action.REINSTALL:
        lines.append("  -> scripts/service/install.sh  (idempotent; regenerates plists, restarts)")
    elif report.verdict is Action.RESTART:
        lines.append("  -> scripts/service/restart.sh  (graceful: current cycles finish first)")
    elif report.verdict is None:
        lines.append("  -> scripts/service/restart.sh  (writes the first manifest)")
    return "\n".join(lines)
