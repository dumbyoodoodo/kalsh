# Station-Expansion Report (Task 3B)

Data through: 2026-07-21T21:45:25.897102+00:00

| Station | Series | Obs days (tmax/tmin) | Missing % | Dup | Anomalies/late-corr | Forecast issuances | Readiness |
|---|---|---|---|---|---|---|---|
| CHI | KXHIGHCHI KXLOWTCHI | 1282/1282 | 1.31 | 0 | 0/4 | 1 | READY WITH DOCUMENTED LIMITATIONS |
| DEN | KXHIGHDEN KXLOWTDEN | 1294/1294 | 0.38 | 0 | 0/8 | 1 | READY WITH DOCUMENTED LIMITATIONS |
| LAX | KXHIGHLAX KXLOWTLAX | 1281/1281 | 1.31 | 0 | 0/2 | 1 | READY WITH DOCUMENTED LIMITATIONS |
| NYC | KXHIGHNY KXLOWTNYC | 1283/1283 | 1.23 | 0 | 0/2 | 3 | READY |

## Readiness reasons

- **CHI**: primary checks pass; secondary limitations: forecast_multi_issuance_observed, market_snapshots_present
- **DEN**: primary checks pass; secondary limitations: forecast_multi_issuance_observed, market_snapshots_present
- **LAX**: primary checks pass; secondary limitations: forecast_multi_issuance_observed, market_snapshots_present
- **NYC**: all primary and secondary checks pass

## Unresolved questions

- Prospective Kalshi market/candle collection for the new series requires the authenticated collector runner; the local .env private-key formatting currently fails PEM parsing (MalformedFraming), so live collector cycles are blocked in this environment until the operator repairs the key material. Discovery is category-wide and the settlement parser resolves all three new cities, so no code change is needed once the runner is restored.
- Forecast cadence for the new stations has exactly one captured issuance; completeness claims require the continuously scheduled weather collector plus ops forecast-cadence observation over multiple days.
