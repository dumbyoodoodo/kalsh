# Kalshi Weather Probability & Trading Research System

A long-term **research platform** for discovering statistically significant, repeatable pricing edges in Kalshi weather event contracts. Automated trading is a possible eventual application of this research, not the goal of the project.

## Research-first philosophy

The highest priority is discovering and validating edges, not shipping a trading bot quickly. Concretely:

- Every strategy idea starts as a hypothesis, not an assumption. See [HYPOTHESES.md](HYPOTHESES.md).
- Research must be reproducible from stored configuration and dataset versions. See [RESEARCH.md](RESEARCH.md).
- Trading infrastructure (paper and live execution) is built only after data is validated, probability models are calibrated, and strategies pass conservative backtests. See [ROADMAP.md](ROADMAP.md) and [TASKS.md](TASKS.md).
- Most hypotheses are expected to fail. A well-documented negative result is a successful outcome of this process, not wasted work.

If you're picking this project back up, read `ROADMAP.md` for where the project is, `TASKS.md` for what's next, `RESEARCH.md` for how work here is expected to be done, and `HYPOTHESES.md` before starting any new strategy idea.

## Why weather first?

Weather contracts have objective settlement criteria, frequent repeated events, public forecast and observation data, and a natural probabilistic structure. Those features make them suitable for disciplined model validation. They do **not** guarantee profit.

## System stages

1. **Observe:** collect market metadata, rules, order books, trades, forecasts, and observations.
2. **Resolve:** map contracts to exact settlement source, station, variable, date, window, units, and rounding rules.
3. **Estimate:** calculate a fair probability distribution.
4. **Evaluate:** measure calibration and backtest net of realistic costs.
5. **Paper trade:** run signals against historical replay and demo.
6. **Deploy cautiously:** enable production only after explicit validation gates.

These stages map onto the development phases in `ROADMAP.md`; stages 1-2 are Phase 2 (Historical Data Platform), stage 3 is Phases 4-5 (features and probability models), stage 4 is Phase 6 (backtesting), stages 5-6 are Phases 7-8 (paper and live trading).

## Current scope

The initial data scope is daily temperature threshold and bucket markets for one city/station. Expansion to precipitation, snow, hurricanes, economics, sports, politics, or market making is out of scope until the temperature system is validated.

The initial *project* scope (Phase 2 onward) is building the historical data platform and research framework. No trading logic is in scope until an edge has been demonstrated through the hypothesis process.

## Core research question

For a contract with current executable price `q`, estimate:

```text
edge = model_probability - all_in_executable_cost_probability
```

A signal exists only when the edge remains positive after:

- bid/ask spread
- exchange fees
- likely slippage
- forecast/model error buffer
- stale-data buffer
- uncertainty in settlement interpretation

## Important financial reality

A high backtest return is not enough. The system must demonstrate:

- out-of-sample performance
- calibrated probabilities
- sufficient sample size
- realistic fills
- cost sensitivity
- acceptable drawdown
- no look-ahead leakage
- stable results across dates, stations, thresholds, and model versions

## Local setup target

```bash
uv sync
cp .env.example .env
docker compose up -d postgres
uv run alembic upgrade head
uv run pytest
uv run kalshi-weather markets list --category weather
```

Exact commands may evolve as the repository is implemented.

## Continuous integration

`.github/workflows/ci.yml` runs the same gates as local development:

```bash
uv sync --frozen --no-editable --all-groups
uv run --no-editable ruff check .
uv run --no-editable mypy src
uv run --no-editable pytest -v -rs
```

`--frozen` is deliberate: CI must fail on a stale `uv.lock` rather than
re-resolve it. `tests/unit/test_ci_workflow_contract.py` parses the workflow and
fails if CI and these documented gates drift apart.

CI runs a disposable `postgres:16` service so the integration tests that assert
real dialect behaviour — Alembic upgrade/downgrade replay, FK and unique
constraints, `ON CONFLICT` dedup, cross-database isolation — actually execute.
Its database is named with the `test_` prefix that
`storage/migration_safety.py` requires of an approved disposable migration
target, so a CI database can never be mistaken for production. Without a
reachable server those tests skip on reachability rather than failing, which is
how they behave on a developer machine with no local PostgreSQL.

Only two integration modules are expected to skip in CI — the demo-exchange
client and the live NWS provider — because shared CI must not hold exchange
credentials or depend on live third-party endpoints. That allowlist is enforced,
not merely documented: a new skipping module fails the contract test until it is
classified.

## Environment policy

- `development`: public data collection and local work
- `demo`: Kalshi demo authentication and simulated funds
- `backtest`: deterministic historical replay
- `production`: disabled by default and protected by multiple flags

## Project status

Phase 1 (Infrastructure) and Phase 2 (Historical Data Platform) are complete: the Kalshi market-data collector (Milestone 2), automated settlement resolution (Milestone 2b), the weather forecast/observation collector (Milestone 3), and the point-in-time research dataset builder (Milestone 4) — see `docs/runbooks/collector.md`, `docs/runbooks/settlement.md`, `docs/runbooks/weather_collector.md`, and `docs/runbooks/dataset.md`. The dataset builder's market→weather mappings are now derived automatically by a deterministic settlement parser (`docs/adr/0005-settlement-resolution.md`), with the config map retained as a manual override layer. Phase 3 (Research Framework) is also complete: ten pre-registered hypotheses (`HYPOTHESES.md`), the standard experiment protocol (`RESEARCH.md`), and the research program (`docs/research/2026-07-21-research-plan.md`); the recommended first experiment is H0002 (settlement-revision risk). The platform is operational for continuous unattended collection (`ops run`, `ops health`, `ops quality`, `ops snapshot`, `weather backfill` — see `docs/runbooks/operations.md`). No modeling, backtesting, or order-submission code exists anywhere in the repository.

## Initial deliverable

The first release collected and normalized Kalshi weather-market data. The second collected and normalized NWS weather forecasts and observations (the confirmed settlement source, `docs/adr/0003-weather-data-source.md`). The third joins them into versioned, point-in-time-correct research datasets (Parquet + reproducibility manifest, `docs/adr/0004-research-dataset.md`). None place orders. See `ROADMAP.md` for what comes after.
