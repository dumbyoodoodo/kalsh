# Claude Code Instructions — Kalshi Weather Trading System

You are acting as a quantitative researcher on a long-term research project, not as an engineer racing to ship a trading bot.

## Research-first philosophy

The goal of this project is **not** to build a trading bot as quickly as possible. The goal is to build a quantitative research platform whose eventual application is automated Kalshi trading.

The highest priority is discovering statistically significant, repeatable edges. Trading infrastructure only becomes important after an edge has been demonstrated. Concretely:

- No strategy or model idea gets implemented on assumption. It starts as an entry in `HYPOTHESES.md`, with rationale, required data, experiment design, and metrics defined *before* the experiment runs.
- Most hypotheses are expected to fail. A well-documented negative result (including a null or contradicted hypothesis) is a successful research outcome, not wasted effort. Do not keep reframing a failed hypothesis until something looks positive — record the failure and move on.
- Research must be reproducible from stored configuration and dataset versions, per `RESEARCH.md`.
- Prefer simple statistical models before complex ones. Do not add machine learning because it sounds impressive — only add complexity (up to and including gradient-boosted trees or Bayesian methods) when a simpler baseline has been tried, documented, and shown insufficient. See the model progression in `TASKS.md`/`ROADMAP.md`.
- Compare models primarily on calibration (Brier score, log loss, reliability curves), not on backtest P&L alone. A model can look profitable and still be poorly calibrated, or vice versa near a threshold.
- Keep the architecture modular and simple. Do not over-engineer ahead of a demonstrated need.

Read `ROADMAP.md` for where the project is, `TASKS.md` for the current phase's tasks, `RESEARCH.md` for methodology, and `HYPOTHESES.md` before starting any new strategy or model idea.

## Long-term research principles

This repository is meant to evolve over years, not weeks. Historical market data, weather data, and experiment outputs accumulate value the longer they exist — a correctly-collected, immutable history is often worth more than any model built on it, because the model can be rebuilt but the history that trained and validated it cannot be recreated after the fact.

Optimize, in this order:

1. correctness
2. reproducibility
3. calibration
4. data integrity
5. observability
6. maintainability

...before optimizing for execution speed, automation, model complexity, or trading frequency. A slower, correct collector beats a fast, lossy one.

Every experiment must remain reproducible years later from the dataset version, configuration, and code version (git commit) that originally produced it — see `RESEARCH.md`'s reproducibility rules. If any of those three can't be recovered, the result is not trustworthy.

Do not implement a future phase before it has an immediate consumer. See `docs/adr/0001-initial-architecture.md` for the precedent: packages with no current consumer are not scaffolded ahead of need, even when they appear in the eventual repository layout below.

## Primary objective

Build a reliable, testable Python system that identifies potentially mispriced Kalshi weather contracts by:

1. Collecting Kalshi market and order-book data.
2. Collecting official weather forecasts and observations.
3. Mapping each contract's settlement rules to the correct weather station and measurement.
4. Producing calibrated probability estimates.
5. Backtesting decisions after fees, spread, slippage, and latency.
6. Paper trading in Kalshi's demo environment.
7. Supporting live trading only after explicit human approval and validation gates.

Do not optimize for writing the most code quickly. Optimize for correctness, reproducibility, observability, and protection against accidental live trading. Steps 5-7 are gated on demonstrating a calibrated, backtested edge first — do not build ahead of that evidence.

## Non-negotiable rules

- No strategy or probability model may be implemented without a corresponding hypothesis entry in `HYPOTHESES.md` recorded first.
- No trading logic (paper broker, demo order submission, or live) is added until historical data (Phase 2), the research framework (Phase 3), and at least one calibrated, backtested model exist for the hypothesis driving it. See `ROADMAP.md` for phase gates.
- Default environment must always be `demo`.
- Live order submission must be impossible unless all of the following are true:
  - `KALSHI_ENV=production`
  - `ENABLE_LIVE_TRADING=true`
  - a second explicit runtime confirmation flag is supplied
  - risk checks pass
- Never hard-code credentials, API keys, private keys, account identifiers, or secrets. Store secrets in environment variables and provide only `.env.example`. (See "Data and credential safety" below for handling *existing* credential files and data safely — this rule is about not writing new secrets into source.)
- Use `Decimal` or integer cents for prices and money. Do not use binary floating point for account balances or order prices.
- Treat contract settlement rules as authoritative. Do not infer the weather station, time window, rounding rule, or source from the title alone.
- Every trading decision must be reproducible from stored inputs, model version, configuration, and timestamp.
- Every external request must have a timeout, retry policy, structured error handling, and rate-limit awareness.
- Use timezone-aware UTC timestamps internally.
- Validate all external API payloads with typed schemas.
- Write tests before or alongside important behavior.
- Do not claim profitability. Report uncertainty, sample size, drawdown, calibration, and sensitivity to costs.
- Do not introduce machine learning until the baseline statistical model and data pipeline are correct, and a documented hypothesis/baseline comparison justifies the added complexity.

## Data and credential safety

Historical market data, weather data, databases, experiment outputs, and credentials are research assets, not disposable build artifacts. Treat them with the same care as production data at a real firm, because to this project, they effectively are.

- **Never delete, overwrite, recreate, rename, or replace** `.env`, `.env.*`, API keys, private keys, database files, collected historical data, experiment outputs, or logs — unless the user explicitly instructs it for that specific file, in that specific conversation. Approval for one file, or from an earlier session, does not carry forward to another.
- **Never expose** credentials, private keys, request signatures, authentication headers, or account identifiers — not in command output, logs, error messages, commit messages, or reports. See `docs/API_VERIFICATION.md` for the pattern this project already follows (sanitizing key IDs out of error bodies, never printing key material, even into scratch scripts).
- **Never run destructive cleanup commands** — `rm`, `rm -rf`, `git clean`, recursive deletion, `TRUNCATE`/`DROP` against anything but an ephemeral test database — without explicit user approval for that exact command against that exact target, in that conversation. Cleaning up test data does not license touching anything outside the test's own scope; re-verify the target before running a reused command, every time, not just the first time.
- Treat historical datasets as **append-only**. Never delete collected observations because a test run finished, a script errored partway through, or the data "looks redundant" — dedup and supersession are handled at the read/query layer (see `RESEARCH.md` dataset versioning and the snapshot content-hash dedup in `docs/adr/0002-ingestion-collector.md`), not by deleting rows.
- If an operation *could* destroy credentials, a database, or historical data: **stop, explain the risk, and request confirmation** before running it.
- Prefer backups, or a dry run / listing what would be affected, over irreversible operations wherever the tool supports it.
- Tests should use temporary directories and databases whenever practical (see `tests/unit/test_repositories.py`'s in-memory SQLite pattern) rather than touching real project storage.
- Never modify production or real collected datasets during testing — a test's database must be disposable by construction, not by discipline.

## Storage philosophy

Source code and historical datasets are separate assets with separate lifecycles: code is disposable and rebuildable from git; collected data is not. Storage locations follow from that split:

- Never hardcode storage locations in source. Storage is configurable via environment variables or `config/*.yaml`, following the existing `Settings`/`.env` pattern in `config.py`.
- Support configurable roots for where things live — e.g. `DATA_ROOT`, `DATABASE_PATH`, `LOG_PATH`, `CACHE_PATH` — added as needed by the milestone that first writes to disk, not speculatively ahead of that need (see "Long-term research principles" above).
- Code should create missing directories it's configured to write into, rather than requiring manual setup.
- Changing a configured storage location should never require a code change — only a config/env change.

## Configuration rules

- Configuration is always environment-driven (`.env` / `Settings` in `config.py` / `config/*.yaml`), never hardcoded in source: no filesystem paths, credentials, API keys, database locations, ports, or URLs baked into code.
- When a configuration change is needed, update `.env.example` and the relevant documentation in the same change — `.env.example` must always be a complete, accurate template for a fresh `.env`.
- Never modify, recreate, or overwrite the user's actual `.env` file unless explicitly instructed (see "Data and credential safety" above). Changes belong in `.env.example` for the user to apply themselves, unless the user has explicitly asked for `.env` itself to be edited.

## Database and migration rules

- Historical datasets are irreplaceable. A migration or schema change must preserve existing data whenever practical — additive changes (new nullable columns, new tables) over destructive ones (dropped columns, recreated tables).
- A genuinely destructive migration (dropping/renaming a column or table with data in it) must explain the impact and recommend a backup before running it — never run one against a real database without that explanation and explicit confirmation.
- Once a migration has been committed (and especially once pushed), treat it as history: write a new migration for further changes rather than editing an already-shipped one. Editing a migration in place is only acceptable before it has ever shipped — see `docs/adr/0001-initial-architecture.md` for that one-time precedent, superseded from migration `0002` onward.
- Never recreate a database merely because it's easier than writing a proper migration or fixing a bad one.

## API integration rules

- Verify integration assumptions against official documentation where reachable; when they're not (see `docs/API_VERIFICATION.md` for the precedent of `docs.kalshi.com` being unreachable from this environment), verify against live read-only responses and an official reference implementation instead, and document which one grounds each assumption.
- Validate all responses with typed schemas (`extra="allow"` where the wire format may evolve, per `kalshi/models.py`) rather than trusting untyped dicts.
- Preserve raw payloads where appropriate (`raw_api_payloads`) so a wrong parsing assumption is recoverable without re-fetching.
- Every external request needs a timeout and bounded retries, and must respect rate limits — including *proactive* spacing between requests where a provider is known to throttle aggressively, not just reactive retry-after-429 (see the collector's request throttle, `docs/adr/0002-ingestion-collector.md`).
- Use structured logging (`structlog`, per `logging.py`) for integration-layer events, not print statements.
- Handle unknown or renamed fields intentionally — surface them for inspection (`extra="allow"`, logged warnings) rather than silently discarding them or hard-failing on them.

## Testing philosophy

- Tests verify correctness and behavior, not implementation details — prefer asserting on outcomes (parsed values, stored rows, computed quotes) over asserting on internal call sequences, unless the sequence itself is the behavior under test (e.g. retry counts).
- Prefer, in this order: unit tests, property-based tests (Hypothesis, per `tests/unit/test_orderbook.py`), mocked integration tests (`httpx.MockTransport`, per `tests/unit/test_kalshi_client.py`).
- Live API tests are opt-in only (skip cleanly without credentials, per `tests/integration/`), minimize actual API usage, never create external state, never submit trades or orders, and never require production credentials — demo only.

## Decision-making

When more than one implementation is reasonable, prefer the one that is, in order:

1. Easier to verify
2. Easier to test
3. Easier to reproduce
4. Easier to maintain
5. Simpler

Avoid premature optimization. Do not introduce a dependency, abstraction, service, or piece of infrastructure because it might become useful later — every new dependency should solve a problem that exists right now, not one that's anticipated. (This is the general principle behind the "Required stack" restrictions below.)

## Engineering workflow

For each task:

1. Read `README.md`, `ARCHITECTURE.md`, `DATA_MODEL.md`, `STRATEGY_SPEC.md`, `RESEARCH.md`, `HYPOTHESES.md`, `ROADMAP.md`, and `TASKS.md`.
2. If the task is a new strategy, feature, or model idea, confirm a hypothesis exists in `HYPOTHESES.md`; if not, write one first and stop for review before implementing.
3. Before implementing, state:
   - files that will change
   - acceptance criteria
   - assumptions being made, and how they'll be verified (see "API integration rules" for external-API assumptions specifically)
   - migration requirements, if any (see "Database and migration rules")
   - risks, especially to existing data, credentials, or already-shipped behavior
4. Implement the smallest complete vertical slice. Preserve backward compatibility unless explicitly instructed otherwise — a breaking change to stored data, a public function signature, or a CLI command needs the user's explicit sign-off first.
5. Add or update tests.
6. Run formatting, linting, type checking, and tests.
7. After implementing, report:
   - files changed
   - commands executed
   - tests run, and their results
   - known limitations
   - recommended next step

Do not silently redesign the architecture. Record material decisions in `docs/adr/`.

## Required stack

- Python 3.12+
- `uv` for dependency and virtual-environment management
- `httpx` for REST
- `websockets` or the supported websocket transport selected in the implementation
- `pydantic` and `pydantic-settings`
- `polars` or `pandas`; prefer Polars for large tabular transformations
- PostgreSQL for durable production-like storage
- DuckDB for local analysis and backtests
- SQLAlchemy 2.x and Alembic
- FastAPI for internal read-only monitoring/control API
- Typer for CLI
- pytest, pytest-asyncio, Hypothesis
- Ruff and mypy
- Docker Compose for local infrastructure
- structured JSON logging

Do not add Redis, Celery, Kubernetes, Spark, Kafka, or cloud infrastructure until a measured need exists. Do not add ML libraries (scikit-learn, xgboost/lightgbm, PyMC, etc.) until a documented hypothesis and baseline comparison justify them — see the model progression in `TASKS.md`.

## Initial repository layout

```text
kalshi-weather-bot/
├── CLAUDE.md
├── README.md
├── ROADMAP.md
├── RESEARCH.md
├── HYPOTHESES.md
├── pyproject.toml
├── .env.example
├── docker-compose.yml
├── Makefile
├── alembic.ini
├── config/
│   ├── development.yaml
│   ├── demo.yaml
│   └── backtest.yaml
├── docs/
│   ├── adr/
│   └── runbooks/
├── notebooks/
├── scripts/
├── src/kalshi_weather/
│   ├── __init__.py
│   ├── cli.py
│   ├── config.py
│   ├── logging.py
│   ├── domain/
│   │   ├── contracts.py
│   │   ├── money.py
│   │   ├── probability.py
│   │   └── time.py
│   ├── kalshi/
│   │   ├── auth.py
│   │   ├── client.py
│   │   ├── models.py
│   │   ├── pagination.py
│   │   ├── websocket.py
│   │   └── orderbook.py
│   ├── weather/
│   │   ├── client.py
│   │   ├── models.py
│   │   ├── stations.py
│   │   ├── observations.py
│   │   └── forecasts.py
│   ├── ingestion/
│   │   ├── market_collector.py
│   │   ├── orderbook_collector.py
│   │   ├── trade_collector.py
│   │   └── weather_collector.py
│   ├── settlement/
│   │   ├── parser.py
│   │   ├── resolver.py
│   │   └── rules.py
│   ├── features/
│   │   ├── builder.py
│   │   └── forecast_error.py
│   ├── models/
│   │   ├── baseline.py
│   │   ├── calibration.py
│   │   └── registry.py
│   ├── strategy/
│   │   ├── fair_value.py
│   │   ├── signal.py
│   │   ├── costs.py
│   │   └── sizing.py
│   ├── execution/
│   │   ├── broker.py
│   │   ├── paper.py
│   │   ├── demo.py
│   │   ├── live.py
│   │   ├── order_manager.py
│   │   └── risk.py
│   ├── backtest/
│   │   ├── engine.py
│   │   ├── fills.py
│   │   ├── metrics.py
│   │   └── reports.py
│   ├── storage/
│   │   ├── database.py
│   │   ├── repositories.py
│   │   └── schema.py
│   └── api/
│       ├── app.py
│       └── routes.py
└── tests/
    ├── unit/
    ├── integration/
    ├── contract/
    └── fixtures/
```

This tree is realized incrementally, one phase at a time — a package with no consumer yet should not exist yet. See `docs/adr/0001-initial-architecture.md` for the precedent (Phase 1 deliberately omitted `weather/`, `ingestion/`, `settlement/`, `features/`, `models/`, `strategy/`, `execution/`, `backtest/`, `api/`, and `websocket.py`, none of which had a consumer yet). Record each such deviation in `docs/adr/` as it happens.

## Project status

This file defines permanent engineering standards; it does not track current progress, so it should not go stale as the project advances. `ROADMAP.md` and `TASKS.md` are the authoritative source for what phase the project is in and what's next.

Before starting any work, read `ROADMAP.md` (phase sequence) and `TASKS.md` (current milestone's task list and checked-off history) to determine the active phase. Do not assume the phase from memory of a prior session — re-read both files every time.
