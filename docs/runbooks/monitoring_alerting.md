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
agents -- idempotent, safe to re-run. It now also generates and bootstraps
`~/Library/LaunchAgents/com.kalshi-weather.monitor.plist`. Because the
installer reloads every registered agent (bootout + bootstrap) to stay
idempotent, running it **also restarts the already-running collector**
gracefully (current cycles finish first) -- expected, not a bug, but worth
knowing before running it against a live collector.

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

**Known limitation**: this system has no meta-monitor -- nothing alerts if
the `com.kalshi-weather.monitor` launchd agent itself stops running (the
same class of blind spot the observatory closed for the collectors, one
level up). `scripts/service/status.sh` surfaces this on manual inspection;
there is no automated escalation beyond it. Treated as an acceptable,
disclosed residual gap rather than solved here -- an infinite regress of
"what watches the watcher" has to stop somewhere, and launchd itself
(`KeepAlive`-free, `StartInterval`-scheduled, OS-level) is the base of that
chain.
