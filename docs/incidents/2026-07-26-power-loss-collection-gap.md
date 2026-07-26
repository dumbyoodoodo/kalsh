# Incident 2026-07-26: power-loss collection gap (partially unrecoverable)

## Status

Closed. Collection resumed 2026-07-26T18:48Z. Recoverable streams backfilled;
the unrecoverable losses are recorded here as permanent, explained gaps in the
archive so they are never mistaken for a collector bug or silently
"interpolated" by a future consumer.

## Summary

The collector host (a single Mac) lost power at approximately
**2026-07-26T04:14Z**. PostgreSQL and the collector process survived as
suspended state through the outage but the collector came back **wedged**
(launchd still reported it "running"; it produced no cycle after 04:14). It was
manually restarted at **2026-07-26T18:48Z**.

**Collection gap: 2026-07-26T04:14Z → 2026-07-26T18:48Z (~14.6 hours).** No
data already in PostgreSQL was lost or corrupted; the database was intact and a
fresh verified backup exists on and off machine
(`kalshi_weather-20260726T184436Z.dump`, local + S3).

## Per-stream impact and recoverability

| Stream | In the gap | Recoverable? | Action taken |
|---|---|---|---|
| Market candlesticks (production prices) | no candles collected | **Yes** — Kalshi serves elapsed periods within its retention window | 583 candles backfilled for markets that had already settled; the ~360 markets that were open during the gap self-heal when they settle (the settlement price-sync fetches full history). Spot-verified: an open gap-active market returned 32 in-gap candles on demand. |
| Weather observations | no observations 04:14–18:48Z | **No** | `weather backfill --no-skip-covered` for all 4 stations for 2026-07-26 recovered **0** rows: the NWS observations endpoint only serves a short recent window, and the gap-hour observations had already rolled off by the time of backfill (~14 h later). **Permanent gap.** |
| NWS forecast issuances | ~12–13 issuance cycles missed (≈21/day across 4 stations) | **No** | NWS serves only the *current* gridpoint forecast, not historical issuances. **Permanent gap** in the forecast time series for the gap window. |
| Demo market snapshots / trades / order books | not collected | trades/candles partially backfillable; live order-book depth not | **Deliberately not backfilled.** The collector reads the *demo* venue, which carries ~zero real liquidity (ADR 0013): demo volume/OI is 0, order books are empty, the trade feed is near-empty. The research value of this window is negligible, so effort was not spent on it. |

## Unrecoverable data (permanent, explained gaps)

For any consumer or future researcher: the following are known-missing for
**2026-07-26T04:14Z – 18:48Z** and must be treated as absent, not zero and not
interpolated:

- **Weather observations** for stations CHI, DEN, LAX, NYC.
- **NWS forecast issuances** for all stations (the point-in-time forecast
  series has a hole here; forecast-error features over this window are
  undefined, not zero).

These are inherent to the providers' short retention of point-in-time data, not
a defect in the collector. The candlestick series has no permanent gap once the
open gap-active markets settle.

## Root cause

A single-host deployment with no defenses against the host being *off*:

1. The Postgres container had `RestartPolicy=no` — it would not have come back
   after a reboot even though the collector's launchd agent (`RunAtLoad=true`)
   would have.
2. On-device alerting only (`ops monitor` → Telegram runs *on* the dead host),
   so nothing off-device noticed the outage for ~14.6 h.
3. The collector wedged rather than exiting on resume, so `KeepAlive` did not
   restart it (secondary; a local watchdog is deprioritized because it would
   not have helped while the host was powered off).

## Remediation (this change)

- **DB auto-restart:** `docker-compose.yml` Postgres now `restart:
  unless-stopped`; applied live to the running container. The DB now returns
  with Docker after a reboot.
- **Off-device uptime monitoring:** new `ops heartbeat` dead-man's-switch pings
  an external monitor (`HEARTBEAT_URL`) only while the collector is healthy, so
  a power-off / wedge / DB-outage stops the pings and the external service
  alerts. Launchd agent `com.kalshi-weather.heartbeat` (every
  `HEARTBEAT_INTERVAL_SECONDS`). See `docs/runbooks/monitoring_alerting.md`.
- **Reboot recovery checklist** (Docker Desktop "start at login", macOS
  auto-login, and — desktop hardware only — power-failure auto-restart):
  `docs/runbooks/collector_service.md`.

Deprioritized (per the same decision): a local watchdog for software wedges —
useful, but orthogonal to the power-off failure this incident was about; the
external heartbeat already trips on a wedge as a side effect.
