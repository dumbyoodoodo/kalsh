# Runbook: Operational monitoring & alerting

Turns the Data Quality Observatory (`docs/runbooks/data_quality_observatory.md`)
from a report someone has to remember to run into a scheduled check that
notifies a human when something needs attention. This closes the gap the
2026-07-22 Research Program Audit found: the observatory correctly detects
failures, but nothing was scheduling it and nothing was alerting on it -- a
real ~6-hour collector outage went unnoticed until an unrelated audit
happened to run it by hand.

**Detection and notification only.** Nothing here repairs anything, retries
a collection, or restarts a process. A human reads the alert and decides
what to do -- see "Failure recovery" below and
`docs/runbooks/operations.md`'s "Recovering from failures" table.

## Architecture

- `kalshi_weather.observatory` (existing) -- runs every check, produces an
  `ObservatoryReport` with an overall `Severity` (`info`/`warning`/`critical`).
  Unchanged by this task.
- `kalshi_weather.ops.monitor` (new) -- the alert **state machine**: given
  the observatory's current severity and the last-persisted state, decides
  whether to alert this run, and persists the new state. Pure decision
  logic (`decide_alert`), independent of any transport or database.
- `kalshi_weather.ops.alerting` (new) -- **delivery**: sends a message via
  whichever transport is configured. One transport today (Telegram bot
  API); adding another is a new function plus one dispatch branch, not a
  redesign.
- `ops monitor` (CLI command) -- orchestrates the three: runs the
  observatory, asks `monitor.decide_alert`, calls `alerting.send_alert` if
  the decision says to, and appends one line to the alert-history log.
- `scripts/service/monitor.sh` + the `com.kalshi-weather.monitor` launchd
  agent -- runs `ops monitor` on a schedule.

## Installation

```bash
scripts/service/install.sh
```

This is the same installer that manages the collector and log-rotation
agents -- idempotent, safe to re-run. It generates and bootstraps
`~/Library/LaunchAgents/com.kalshi-weather.monitor.plist`. The installer now
performs a **per-agent conditional reload**: each freshly-generated plist is
compared against what is already installed, and only an agent whose plist
actually changed (or that isn't loaded yet) is bootout+bootstrapped. Running
it therefore does **not** restart the already-running collector unless the
collector's own plist changed. (Earlier versions reloaded every agent
unconditionally, which is what briefly restarted the collector the first time
the monitor agent was added; that class of problem is fixed for every agent --
see `docs/runbooks/backup_recovery.md`'s "Installation".)

`scripts/service/uninstall.sh` removes all three agents (collector,
log-rotation, monitor) and leaves logs, `monitor_state.json`, and
`alert_history.jsonl` in place.

## Configuration

All in `.env` (see `.env.example`'s "Monitoring & alerting" section):

| Variable | Default | Meaning |
|---|---|---|
| `ALERT_TRANSPORT` | `none` | `telegram` or `none`. `none` means the monitor still runs, decides, and logs history -- it just never calls out. |
| `ALERT_TELEGRAM_BOT_TOKEN` | unset | Bot token from [@BotFather](https://t.me/BotFather). |
| `ALERT_TELEGRAM_CHAT_ID` | unset | The chat id to send to -- message your bot once, then `GET https://api.telegram.org/bot<TOKEN>/getUpdates` and read `message.chat.id` from the response. |
| `MONITOR_INTERVAL_SECONDS` | `900` (15 min) | How often launchd runs `ops monitor`. Read by `scripts/service/common.sh` **at install time** -- changing it requires `scripts/service/install.sh`, not `restart.sh`. |

Bot token and chat id are never logged, echoed, or included in any
delivery-status message -- `ops/alerting.py`'s `DeliveryResult.detail`
reports HTTP status codes and exception types only (verified in
`tests/unit/test_ops_alerting.py::test_send_telegram_alert_non_200_is_not_delivered`).

## Alert policy (state machine)

`ops.monitor.decide_alert(current_severity, prior_state, now)` is a pure
function of those three arguments -- deterministic by construction, no
hidden state. Transitions:

| Prior severity | Current severity | Transition | Alert? |
|---|---|---|---|
| (none -- first run) | INFO | `no_change` | No |
| (none -- first run) | WARNING/CRITICAL | `new_problem` | **Yes** (a fresh baseline that's already unhealthy is still worth surfacing) |
| INFO | WARNING or CRITICAL | `new_problem` | **Yes** |
| WARNING/CRITICAL | same severity | `no_change` | No -- never repeats |
| WARNING | CRITICAL (or vice versa) | `severity_changed` | **Yes** -- an escalation or de-escalation is new information |
| WARNING/CRITICAL | INFO | `recovered` | **Yes** -- recovery notification |
| INFO | INFO | `no_change` | No |

In short: exactly one alert per state transition, no repeats while the
state is stable, and a recovery notification when it clears. This is
enforced structurally (comparing only `current` against `prior.severity`),
not by any time-based cooldown or rate limit -- there is nothing to tune
and nothing that can silently suppress a second, different problem.

**Do not manually edit or delete `monitor_state.json` to force a recovery.**
The state file is the *only* record of the prior severity; the exactly-once
semantics are computed by comparing the current severity against it. Clearing
it resets the baseline, so the next run treats whatever it finds as a fresh
first-run problem (re-alerting) rather than a transition -- and it cannot
manufacture a genuine `recovered` notification. The correct way to recover is
to fix the underlying finding and let the next scheduled monitor cycle observe
the severity drop and emit the de-escalation/recovery alert naturally. This is
exactly how the 2026-07-23 observatory severity fix de-escalated: the next real
monitor cycle recomputed CRITICAL→WARNING and sent exactly one alert, with no
state-file surgery.

State persists in `<KALSHI_LOG_DIR>/monitor_state.json`:

```json
{
  "schema": 1,
  "severity": "critical",
  "since": "2026-07-22T23:05:00+00:00",
  "last_alert_at": "2026-07-22T23:05:00+00:00"
}
```

Written with an atomic rename (matches `ops/restart_policy.write_manifest`)
so a crash mid-write never leaves a corrupt state file that would derive a
wrong transition on the next run.

## Alert history log

Every `ops monitor` run appends exactly one line to
`<KALSHI_LOG_DIR>/alert_history.jsonl` -- append-only, never rewritten or
truncated, whether or not an alert actually fired:

```json
{"timestamp": "2026-07-22T23:05:00+00:00", "severity": "critical", "transition": "new_problem", "reason": "platform/missed_collection_cycles_kalshi, forecast/cadence_collector_outage", "delivered": true, "detail": "telegram: delivered"}
```

- `delivered: true` -- the transport reported success.
- `delivered: false` -- an alert was decided but delivery failed or no
  transport is configured (`detail` says which).
- `delivered: null` -- no alert was even attempted (a suppressed,
  `no_change` run) -- this is the common case at the default 15-minute
  cadence, and is expected, not a gap in coverage.

## Running it by hand

```bash
uv run kalshi-weather ops monitor \
  --state-path ~/Library/Logs/kalshi-weather/monitor_state.json \
  --history-path ~/Library/Logs/kalshi-weather/alert_history.jsonl
uv run kalshi-weather ops monitor --state-path ... --history-path ... --json
```

Exit code is non-zero iff the observatory's current status is CRITICAL
(the same convention as `ops observatory`). A non-blocking lock at
`<state-path>.lock` means a manual run while the scheduled one is mid-flight
skips cleanly (prints a message, exits 0) rather than running concurrently
or blocking.

`scripts/service/status.sh` shows the monitor agent's launchd state, the
current persisted alert state, and the last 3 history entries in one place.

## Testing

- `tests/unit/test_ops_monitor.py` -- the state machine (every transition
  in the table above, plus determinism: identical `(current, prior, now)`
  always yields an identical `AlertDecision`), message formatting, the
  atomic state file, the append-only history log, and the lock guard
  (acquire, blocked-while-held, available again after release).
- `tests/unit/test_ops_alerting.py` -- Telegram delivery success, non-200
  response, and network-error paths, all via `httpx.MockTransport` (no
  real network calls; matches this project's established
  `tests/unit/test_kalshi_client.py` pattern) -- plus confirmation that no
  transport misconfiguration ever raises.
- `tests/unit/test_ops_monitor_scenarios.py` -- **the four simulated
  failure modes this task's validation section names**, each built from the
  *actual* production detection function (not a hand-invented finding) and
  driven through the real state machine:
  - **Collector stopped** -- `observatory.continuity.find_missed_run_cycles`
    with a run 6 hours stale (CRITICAL).
  - **Missing collection** -- `observatory.drift.adapt_cadence_alert` on a
    `missing_issuance` cadence alert (WARNING).
  - **Continuity gap** -- `observatory.continuity.find_continuity_gaps` on
    a 10-hour-stale market stream (WARNING).
  - **Parser failure** -- `observatory.parsing.summarize_weather_parser_failures`
    on a run of cycles at a 75% failure rate, above the CRITICAL threshold.

  Each scenario's test asserts, in order: the fresh problem alerts once;
  the same problem persisting on the next run is suppressed; recovery to
  INFO sends a recovery notification; the healthy state afterward stays
  quiet -- then repeats the whole sequence a second time and asserts every
  decision matches, verifying determinism.

Run just this task's tests:

```bash
uv run pytest tests/unit/test_ops_monitor.py tests/unit/test_ops_alerting.py tests/unit/test_ops_monitor_scenarios.py -v
```

## Alert semantics reference

| Transition | Meaning | Example message header |
|---|---|---|
| `new_problem` | Was INFO (or this is the first-ever run), now WARNING/CRITICAL | `[NEW_PROBLEM] observatory status: CRITICAL` |
| `severity_changed` | Was already unhealthy, severity level changed | `[SEVERITY_CHANGED] observatory status: WARNING` (was CRITICAL) |
| `recovered` | Was WARNING/CRITICAL, now INFO | `[RECOVERED] observatory status: INFO` -- body says "no action needed" |
| `no_change` | Same severity as last run | never alerted; visible only in the history log |

The message body (for anything but a recovery) lists CRITICAL findings
first, then WARNING, each as `domain/check: message`, capped at 10 per
severity with a "... and N more" tail -- see `monitor.format_alert_message`.

## Failure recovery

This system detects and notifies; it does not fix anything. Once alerted,
follow `docs/runbooks/operations.md`'s "Recovering from failures" table and
`docs/runbooks/data_quality_observatory.md`'s per-check guidance for what
the underlying finding means and what to actually do about it.

Failure modes of the monitor itself:

| Symptom | Likely cause | Action |
|---|---|---|
| No alerts ever arrive, even during a known outage | `ALERT_TRANSPORT=none` (the default) | Set `ALERT_TRANSPORT=telegram` and both Telegram variables in `.env`, then `scripts/service/install.sh` |
| `delivered: false` in the history log with `detail` mentioning an HTTP status | Bad bot token/chat id, or the bot was blocked | Re-verify credentials via `getUpdates`; token/chat id are never logged, so re-derive them fresh rather than searching logs |
| `com.kalshi-weather.monitor` not loaded | Never installed, or `uninstall.sh` was run | `scripts/service/install.sh` |
| Alerts stop arriving entirely, no history growth | The monitor agent itself is down (a meta-monitoring gap -- see "Known limitation" below) | `scripts/service/status.sh`; check `MONITOR_ERR_LOG` |
| Same problem alerted twice in a row | Should not happen -- `monitor_state.json` was deleted/corrupted between runs, resetting the baseline | Inspect `MONITOR_ERR_LOG`; the state file is written atomically, so this indicates the file was removed externally, not a crash mid-write |

**Meta-monitoring**: `ops monitor` and its Telegram alerting run *on* the
collector host, so they cannot fire when the host itself is off (the
2026-07-26 power-loss incident: the box was dead ~14.6 h and nothing
off-device noticed). The off-device complement is the heartbeat below. There
is still no meta-monitor for the *monitor agent specifically* while the host
is up -- `scripts/service/status.sh` surfaces that on manual inspection -- but
a total host outage now escalates externally.

## External uptime heartbeat (dead-man's-switch)

`ops heartbeat` (launchd agent `com.kalshi-weather.heartbeat`, every
`HEARTBEAT_INTERVAL_SECONDS`, default 300s) pings an **external** monitor's
push URL (`HEARTBEAT_URL`) **only while the collector is healthy** -- i.e. the
newest `market_snapshot` is younger than `HEARTBEAT_STALE_AFTER_SECONDS`. If
the host is powered off (nothing runs), the process is wedged, or the DB is
unreachable, the ping is withheld and the external service raises the alert
after its own grace period. One mechanism therefore covers power loss
(primary), software wedge, and DB outage, from a system that fails
independently of this host.

Setup (one-time):

1. Create a check on any "alert me when the pings stop" service --
   [healthchecks.io](https://healthchecks.io) is free; Better Uptime and
   Cronitor also work. Set its **period** to `HEARTBEAT_INTERVAL_SECONDS` and
   its **grace** a little above that (e.g. period 5 min, grace 10 min) so one
   skipped ping doesn't false-alarm.
2. Put its ping URL in `.env` as `HEARTBEAT_URL=` (treat it as a secret -- it
   is `SecretStr`, never logged; anyone holding it can suppress your alert by
   pinging it).
3. `scripts/service/install.sh` (installs/loads the heartbeat agent).

Verify: `ops heartbeat` prints `outcome=pinged_healthy emitted=True` when the
collector is fresh, and `withheld_stale` / `withheld_no_data` /
`withheld_db_error` (`emitted=False`) otherwise. With `HEARTBEAT_URL` unset the
agent no-ops (`skipped_not_configured`), so installing it before configuring
the URL is harmless.

Status: `scripts/service/status.sh` has a read-only "heartbeat" section showing
whether the URL is configured (never the value), the agent's installed/loaded/
running state, and the last attempt's timestamp, outcome, collector-health, and
one of `NOT_CONFIGURED` / `AGENT_NOT_RUNNING` / `NO_ATTEMPT` /
`LAST_ATTEMPT_FAILED` / `HEALTHY` (with a staleness `WARNING` if no ping has
succeeded within the ping interval plus grace). The default status check sends
**no** network request -- it reads the local `heartbeat_state.json` that
`ops heartbeat` writes. `status.sh --ping-heartbeat` additionally fires one real
ping as a manual end-to-end test.

## Kalshi recovery watch (paper-validation trigger)

`ops recovery-watch` answers "is it safe to rerun the pending one-contract
paper-fill validation?" with a four-state machine — `HEALTHY`,
`OUTAGE_ACTIVE`, `RECOVERING`, `PAPER_VALIDATION_READY` — whose freshness
gates are `PaperRiskPolicy`'s own thresholds (no competing constants).
Exactly-once notifications per state transition via the same
`ALERT_TRANSPORT` as every other alert, one optional escalation after a
6-hour outage, and atomic JSON state + append-only JSONL history (the
`ops monitor` conventions), so deduplication survives restarts:

```bash
uv run kalshi-weather ops recovery-watch \
  --state-path ~/Library/Logs/kalshi-weather/recovery_watch_state.json \
  --history-path ~/Library/Logs/kalshi-weather/recovery_watch_history.jsonl
```

Output lists each gate PASS/FAIL with measured ages, the current state,
outage start, last notification, and `paper_validation_ready`. The ready
notification reads: "Kalshi production collection has recovered and all
paper freshness gates pass. The pending one-contract paper-fill validation
may now be rerun." **It never runs the validation itself — human approval
remains required.** Run it manually or on a schedule (e.g. alongside
`ops monitor`); `--no-notify` gives a read-only status check, `--json`
machine output. The endpoint probe is an unauthenticated GET of the public
exchange-status URL — never an order, never credentialed.
