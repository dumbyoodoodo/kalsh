# ADR 0001: Initial repository foundation and Milestone 1 scope decisions

## Status

Accepted.

## Context

`CLAUDE.md` specifies a full package layout (`weather/`, `ingestion/`,
`settlement/`, `features/`, `models/`, `strategy/`, `execution/`,
`backtest/`, `api/`, `websocket.py`, etc.) intended for the entire project
lifecycle. The immediate assignment (`TASKS.md` Milestone 0 + Milestone 1)
only requires `domain/`, `kalshi/`, `storage/`, `cli.py`, `config.py`, and
`logging.py`.

## Decisions

1. **Only create packages a milestone actually uses.** Empty placeholder
   packages for `weather/`, `ingestion/`, `settlement/`, `features/`,
   `models/`, `strategy/`, `execution/`, `backtest/`, and `api/` were not
   scaffolded. They will be created in the milestone that first needs them
   (Milestone 2 introduces `settlement/`, Milestone 3 introduces `weather/`
   and `ingestion/`, etc.). This is a deliberate deviation from `CLAUDE.md`'s
   literal file tree, made to avoid shipping unused abstractions ahead of
   need, per the project's own engineering rules against speculative code.
   `websocket.py` is deferred for the same reason -- Milestone 1 uses REST
   only.

2. **FastAPI is not yet a dependency.** It's part of the required stack but
   has no consumer until Milestone 8 (paper trading monitoring/control API).
   Adding it now would be an unused dependency.

3. **Kalshi API wire-format details, originally unverified assumptions, are
   now confirmed against live data.** `docs.kalshi.com` was (and remains)
   unreachable from this environment (WebFetch and direct HTTPS both return
   connection resets), so verification instead used Kalshi's official
   `kalshi-starter-code-python` reference client plus real demo API
   responses. See `docs/API_VERIFICATION.md` for the full report. Several of
   the original assumptions turned out to be wrong and were fixed:
   the signed path was missing the `/trade-api/v2` prefix, and the live wire
   format uses decimal-dollar-string (`*_dollars`) and fixed-point-string
   (`*_fp`) fields instead of the assumed plain integer fields -- `Market`,
   `OrderbookResponse`, and `Trade` in `kalshi/models.py` now normalize these
   into the original integer-cents/integer-count fields so the rest of the
   codebase is unaffected. `kalshi/models.py` still uses `extra="allow"`
   deliberately so a *future* incorrect assumption about field names
   surfaces as extra attributes rather than a hard parse failure -- raw
   payloads are still captured unconditionally before schema validation
   regardless of whether the typed model matches. Authenticated
   `/portfolio/*` reads were also confirmed working end-to-end once a
   correctly demo-scoped API key was used (the initial credentials tested
   belonged to a production account, not a code defect -- see the report).

4. **Order book reconstruction is a pure, schema-independent function**
   (`kalshi/orderbook.py`). Because it takes plain `(price_cents, quantity)`
   tuples rather than the live response type, its extensive unit and
   property-based tests (including the DATA_MODEL.md complement identity)
   remain valid even if the wire schema assumption above turns out to be
   wrong and `models.py` needs to change.

5. **Auth is opportunistic, not endpoint-gated.** `KalshiClient` signs every
   request automatically when `key_id`/`private_key` are configured (needed
   for demo, where public market-data endpoints may still require
   authentication) and sends anonymous requests otherwise (works for public
   production market-data access). A `require_auth` flag exists for future
   endpoints that must fail closed without credentials, but no such endpoint
   is called in this milestone.

6. **Local toolchain workaround: `uv run --no-editable`.** The CPython
   3.12.13 build resolved by `uv python install 3.12` in this environment
   skips `.pth` files whose name starts with `_` (a hidden-file hardening
   behavior), which breaks hatchling's default editable-install `.pth`
   (`_editable_impl_kalshi_weather.pth`) -- `import kalshi_weather` fails
   outright. Installing non-editably sidesteps the `.pth` mechanism
   entirely. The `Makefile` and CI workflow both call
   `uv run --no-editable ...` accordingly. This is an environment quirk, not
   a project design choice; if it doesn't reproduce on a different Python
   build, `--no-editable` is harmless but unnecessary there.

   Trade-off: a non-editable install is a snapshot copy, and `uv run`'s
   implicit sync does not detect that only source files (not
   `pyproject.toml`/lock) changed, so it goes stale silently between edits
   -- tests/CLI runs will keep using old code. `make test` therefore depends
   on `make sync`, which runs
   `uv sync --no-editable --reinstall-package kalshi-weather` before every
   test invocation to force a fresh build. Anyone running `uv run
   --no-editable ...` directly instead of through `make` must remember to
   reinstall after edits, or will see stale-code behavior that's easy to
   mistake for a real bug.

## Consequences

- The tree will grow additional top-level packages incrementally as later
  milestones land, rather than all at once now.
- Anyone continuing this work must verify the `# ASSUMPTION:` items against
  a live Kalshi response (production, unauthenticated, for the read-only
  endpoints) before trusting parsed data, and against a demo response before
  trusting authenticated signing.
- `make check` / CI always pass `--no-editable`; don't remove it without
  confirming the local editable-install bug no longer reproduces.
