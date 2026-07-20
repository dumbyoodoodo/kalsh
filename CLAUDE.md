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
- Never hard-code credentials, API keys, private keys, account identifiers, or secrets.
- Store secrets in environment variables and provide only `.env.example`.
- Use `Decimal` or integer cents for prices and money. Do not use binary floating point for account balances or order prices.
- Treat contract settlement rules as authoritative. Do not infer the weather station, time window, rounding rule, or source from the title alone.
- Every trading decision must be reproducible from stored inputs, model version, configuration, and timestamp.
- Every external request must have a timeout, retry policy, structured error handling, and rate-limit awareness.
- Use timezone-aware UTC timestamps internally.
- Validate all external API payloads with typed schemas.
- Write tests before or alongside important behavior.
- Do not claim profitability. Report uncertainty, sample size, drawdown, calibration, and sensitivity to costs.
- Do not introduce machine learning until the baseline statistical model and data pipeline are correct, and a documented hypothesis/baseline comparison justifies the added complexity.

## Engineering workflow

For each task:

1. Read `README.md`, `ARCHITECTURE.md`, `DATA_MODEL.md`, `STRATEGY_SPEC.md`, `RESEARCH.md`, `HYPOTHESES.md`, `ROADMAP.md`, and `TASKS.md`.
2. If the task is a new strategy, feature, or model idea, confirm a hypothesis exists in `HYPOTHESES.md`; if not, write one first and stop for review before implementing.
3. State the files you will change and the acceptance criteria.
4. Implement the smallest complete vertical slice.
5. Add or update tests.
6. Run formatting, linting, type checking, and tests.
7. Summarize:
   - changes made
   - commands run
   - test results
   - known limitations
   - next recommended task

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

Phase 1 (Infrastructure) is complete: `TASKS.md` Milestone 0 and Milestone 1, a read-only Kalshi market-data gateway with raw-payload capture and normalized storage. No order-submission code exists anywhere in the repository.

Phase 2 (Historical Data Platform) is next. Per the research-first philosophy above, no trading, feature, or modeling logic belongs in Phase 2 — it is data collection, versioning, and point-in-time correctness only. See `TASKS.md` for the current task list and `ROADMAP.md` for the full phase sequence.
