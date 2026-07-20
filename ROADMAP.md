# Roadmap

Long-term, version-numbered view of the project. See `TASKS.md` for the task-level checklist within each version, `ARCHITECTURE.md` for the components each version introduces, and `RESEARCH.md`/`HYPOTHESES.md` for the process gating everything from 0.4 onward.

Versions below 1.0 involve no real money. `1.0` (live trading) is disabled by default regardless of version and requires explicit manual authorization every time (see `CLAUDE.md` non-negotiable rules) — reaching version 1.0 in this document does not itself enable it.

| Version | Name | Corresponds to | Trading logic? |
|---|---|---|---|
| 0.1 | Infrastructure | Phase 1 / Milestones 0-1 | No |
| 0.2 | Data Collection | Phase 2 / Milestones 2-3 | No |
| 0.3 | Historical Database | Phase 2 / Milestone 4, Phase 3 (Research Framework) | No |
| 0.4 | Feature Engineering | Phase 4 | No |
| 0.5 | Baseline Probability Models | Phase 5 / Milestone 5 | No |
| 0.6 | Advanced Models | Phase 5 / Milestone 5a (only if justified) | No |
| 0.7 | Backtesting | Phase 6 / Milestones 6-7 | Simulated only |
| 0.8 | Paper Trading | Phase 7 / Milestones 8-9 | Simulated funds only |
| 1.0 | Live Trading | Phase 8 / Milestone 10 | Real capital, disabled by default |

## 0.1 — Infrastructure (complete)

Read-only Kalshi market-data gateway: authenticated/public REST client, order-book reconstruction, raw-payload capture, normalized storage, CLI. No order-submission code exists.

## 0.2 — Data Collection

Weather-market discovery, settlement-rule mapping, and weather forecast/observation ingestion. Still no trading logic — this is collection and normalization only.

## 0.3 — Historical Database

Point-in-time research datasets joining market, settlement, forecast, and observation data without leakage, exported as versioned Parquet with a manifest. This version also stands up the research framework itself (`RESEARCH.md`, `HYPOTHESES.md`, dataset versioning and experiment-tracking conventions) — the historical database and the framework for using it rigorously ship together.

## 0.4 — Feature Engineering

Point-in-time feature snapshots (forecast error, ensemble spread, order-book imbalance, and similar — see `TASKS.md` Phase 4 for the full candidate list) built only for features an active hypothesis actually needs, versioned alongside datasets.

## 0.5 — Baseline Probability Models

Empirical residual, Gaussian, and Student-t models converting a forecast-error distribution into exact contract probabilities, evaluated primarily by calibration (Brier score, log loss, reliability curves) across walk-forward windows.

## 0.6 — Advanced Models

Gradient-boosted trees and Bayesian approaches, added only if 0.5's baselines are shown insufficient for a specific documented hypothesis. Not a default next step after 0.5.

## 0.7 — Backtesting

Event-driven backtester modeling latency, bid/ask spread, fees, slippage, partial fills, and capital usage. No strategy advances to 0.8 without passing a conservative backtest here, with results recorded against its `HYPOTHESES.md` entry.

## 0.8 — Paper Trading

Paper broker against live quotes, plus authenticated order submission in Kalshi's demo environment (simulated funds only, real order-flow mechanics). Only strategies that passed 0.7 reach this version.

## 1.0 — Live Trading

Real-money execution. Requires: months of uninterrupted paper/demo operation, reconciliation between predicted and actual fills, tested kill switch, tested daily loss/exposure limits, and explicit human approval per `TASKS.md` Milestone 10. Disabled by default via the flags described in `CLAUDE.md`, independent of what version this document says the project has reached.
