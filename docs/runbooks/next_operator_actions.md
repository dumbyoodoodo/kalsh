# Runbook: next operator actions (three milestones)

Written 2026-07-27 at `bc9a4bc`. Covers exactly: (A) Kalshi recovery →
one-contract paper-fill validation, (B) H0019 final-test readiness after
2026-08-25, (C) H0020 readiness through 2026-09-22 / 2026-10-06.

## Start here: `ops status`

```
uv run kalshi-weather ops status            # full dashboard
uv run kalshi-weather ops status --compact   # fast (skips observatory)
uv run kalshi-weather ops status --json       # machine-readable
uv run kalshi-weather ops status --section paper
```

One **read-only** command that summarizes every pending checkpoint — paper
settlement, SEA/PHX/MIA pilot, E0002 remeasurement, H0012r/H0019/H0020
readiness, backups, restore-drill, collector, recovery-watch, storage,
observatory — with, per area: state, blocker, earliest permitted timestamp,
the exact command, and whether human approval is required. It executes
nothing: no experiment, no settlement, no service restart, no exchange call,
no production write. Run it first, whenever you sit down, before any action.

**Interpreting states** (urgency order): `ACTION_REQUIRED` (integrity/kill
switch — stop and investigate) → `READY_FOR_HUMAN_APPROVAL` → `WAITING_FOR_EVIDENCE`
→ `WAITING_FOR_TIME` → `COLLECTING` → `HEALTHY`/`COMPLETE`; `BLOCKED` (final,
e.g. deferred / reserved-window) and `NOT_APPLICABLE` (status not readable this
run — run that area's own command) sit outside the flow. The header prints the
overall posture, the single next recommended action, and the standing
forbidden-action list.

**Two things `ops status` never means.** `READY_FOR_HUMAN_APPROVAL` is *not*
authorization — it means the time/structural gates are clear and a human may
now decide; every execution still needs the area's full gate sequence + explicit
approval. And scientific *readiness is never scientific support* — the research
rows pass through a counts-only readiness *state* only; the dashboard computes
and shows no Brier score, calibration, P&L, or any H0019/H0020 outcome. The
authoritative counts live behind each `experiment readiness` command.

## Safety invariants (read first — they override everything below)

- **NO exchange order may ever be submitted** (live or demo).
- **NO H0019/H0020 partial validation/test performance may be inspected** —
  readiness commands report counts and integrity only.
- **NO frozen artifact may be edited** (EXP-FUTURE-H0019/*, EXP-FUTURE-H0020/*,
  `experiments/h0020.py`, `docs/research/ledger.jsonl`, EXP-20260726-H0018/*).
- **NO test window may be reused**, extended off-rule, or moved.
- **NO paper strategy may consume experiment outputs** before a supported
  result AND its untouched replication (see `backtest_to_paper_bridge.md`).
- **NO automatic or recurring paper validation/strategy** — every paper run
  is one manual, human-approved invocation.
- **NO threshold weakening to force readiness** — a failing gate means wait.
- **NO live-trading enablement** (`ENABLE_LIVE_TRADING` stays false).
- Any hash/ledger/leakage mismatch: **STOP. Investigate. Change nothing.**

---

## A. Telegram says: PAPER_VALIDATION_READY

The recovery watch (runs automatically every 15 min inside the monitor
agent) sends this exactly once when collection has recovered and every
paper freshness gate passes. It never runs the validation itself.

Pre-checks (all must pass):

- [ ] `uv run kalshi-weather ops recovery-watch --state-path "$HOME/Library/Logs/kalshi-weather/recovery_watch_state.json" --history-path "$HOME/Library/Logs/kalshi-weather/recovery_watch_history.jsonl" --no-notify`
      → state `PAPER_VALIDATION_READY`, all gates PASS (recent successful
      collector run; snapshot ≤15 min; book ≤5 min; direct-poll ≤15 min)
- [ ] `uv run kalshi-weather paper reconcile` → "reconcile OK"
- [ ] `uv run kalshi-weather paper forward-status` → kill_switch inactive

Run exactly ONE one-contract validation (manual intent file; pick the
market by **freshest direct evidence and displayed depth only — never by
expected profitability**; do not touch H0019/H0020 anywhere):

- [ ] Write `signals.json`: one ticker, `"quantity": 1`, side + bounded
      `limit_price_cents` at the displayed best price, short `expires_at`,
      a `version` string (see `paper/signals.py` intent-file format)
- [ ] `uv run kalshi-weather paper forward-run --intent-file signals.json --bankroll-cents 10000`
- [ ] Verify: ≤1 fill, fee > 0, `execution_confidence=direct_fresh`,
      book-sourced fill ref
- [ ] Idempotency: rerun the SAME command → `duplicate_intent`, no second
      fill, equity unchanged
- [ ] `uv run kalshi-weather paper reconcile` → exact
- [ ] `uv run kalshi-weather paper forward-report --date <today>` → renders
- [ ] Record the filled ticker here / in ops notes for settlement follow-up

After the filled market shows an authoritative terminal result
(`result` yes/no on a finalized snapshot — typically the morning after):

- [ ] `uv run kalshi-weather paper settle` → exactly-once payout, then
      `paper reconcile` → exact

STOP if: any gate FAILs (state regressed — wait for the next READY alert),
reconcile mismatches (investigate before any run), or a second fill ever
appears (kill switch: `uv run kalshi-weather paper kill --reason ...`).

---

## A2. H0012r — settlement-label stability retry (earliest: 2026-08-19)

H0012r re-runs H0012 **claim B only** (post-settlement correction rate < 2%)
at the accrued sample. The original run left claim B inconclusive *for sample
size* (0/132 corrections; 95% upper bound 2.83% missed the 2% bar; zero events
need n ≥ 189 variable-dates). Single-pass measurement, NYC only
(KXHIGHNY/KXLOWTNYC), unit = (variable, target_date). No train/val/test split.
**Not ledger-registered or hash-pinned** — its authoritative spec is the
HYPOTHESES.md H0012 entry (pre-registered design + decision rule).

Counts-only readiness (safe any time — no outcome, no CI, no stage-difference
rate; **READY does NOT imply claim B will pass**):

```
uv run kalshi-weather experiment readiness h0012r        # human
uv run kalshi-weather experiment readiness h0012r --json  # machine
```

- States: `CALENDAR_GATED` (before 2026-08-19) / `COLLECTING` (n < 189 after
  the boundary) / `INTEGRITY_BLOCKED` / `SPECIFICATION_BLOCKED` / `READY`.
  Never `READY` before 2026-08-19 by construction.
- The preflight is env-agnostic by design (H0012r's settled population is
  mostly pre-cutover null/demo); `--environment X` restricts the close-time
  frame as a sensitivity check only.
- `CALENDAR_GATED`/`COLLECTING` → wait; re-run the preflight. It reports the
  exact coverage (n/189) and an earliest-ready estimate.

`READY` → H0012r execution requires, **in order** (readiness ≠ support):

  - [ ] 1. `experiment readiness h0012r` → READY (counts only; calendar
        boundary reached AND n ≥ 189 AND integrity clean)
  - [ ] 2. `research leakage-audit` → PASS
  - [ ] 3. close-time preflight if claim A's `value_at_close` is in scope —
        the readiness command already runs the close-time guard and reports
        `close_time=PASS`; any `REVIEW_REQUIRED` is a human decision, never
        auto-resolved
  - [ ] 4. verify partition consistency (strikes of one event agree on the
        timeline — readiness reports `partition=ok`) and payout-agreement
        integrity (reconstruction vs Kalshi payout, reported `payout=n/n`)
  - [ ] 5. explicit human approval
  - [ ] 6. only then the single registered retry (the E-A settlement-labels
        script over the accrued dataset), archived with a dataset-version +
        git-commit record per RESEARCH.md
- A negative or still-inconclusive result is **final for this retry** — record
  it; do not re-run after seeing the outcome. Readiness must never be used to
  peek at the correction rate early.

---

## B. H0019 — earliest action date: after 2026-08-25

- [ ] `uv run kalshi-weather experiment readiness h0019`
- **NOT_READY → wait.** Re-run daily; gates are frozen; never loosen them.
- **READY_FOR_FINAL_TEST →** proceed, in order:
  - [ ] Verify frozen hashes: `shasum -a 256 docs/research/experiments/EXP-FUTURE-H0019/config.json docs/research/experiments/EXP-FUTURE-H0019/registration.md`
        → `5d1f763dcf6b…` / `e8e1200ac3b9…` (full values in ledger seq 2)
  - [ ] `git status --short` clean; HEAD == origin/main
  - [ ] Full registered test window (2026-08-12 → 2026-08-25) elapsed —
        readiness enforces this; confirm it reports so
  - [ ] Backup fresh (`uv run kalshi-weather ops backup status`) and dataset
        provenance recorded (snapshot/manifest per RESEARCH.md) BEFORE running
  - [ ] Run the final test **exactly once** via the registered single-shot
        runner (implement it first if absent, mirroring `experiment h0018`,
        per the registration — never ad hoc)
  - [ ] Archive outputs + sha256 hashes into EXP-FUTURE-H0019/; update
        HYPOTHESES.md + ledger (append-only)
  - [ ] Classify strictly by the frozen decision rule. **Never rerun after
        seeing the outcome.**
- Integrity failure at any step (hash mismatch, dirty tree, leakage) → STOP.
- A negative result is **final** — record it and close.
- A supported result authorizes **a replication registration only** — not
  paper trading, not live trading.

---

## C. H0020 — timeline and progression

| Dates | Phase |
|---|---|
| → 2026-08-11 | train collecting (now) |
| 2026-08-12 → 08-25 | **excluded gap (H0019's test) — must stay unused** |
| 2026-08-26 → 09-08 | validation collecting |
| 2026-09-09 → 09-22 | initial test window |
| → 2026-09-29 | first extension end (automatic, frozen rule) |
| → 2026-10-06 | final extension end |

Check (any time, counts + integrity only — no predictive output exists):

```
uv run kalshi-weather experiment readiness h0020
```

- States COLLECTING_* / EXTENDED_TEST_COLLECTING → wait; nothing to do.
- Extensions are **automatic and frozen**: an elapsed test end with failing
  gates activates exactly one +7-day extension, at most twice. Never extend
  manually; never shrink; never re-date.
- Gates still failing after 2026-10-06 → `DEFERRED_INSUFFICIENT_DATA`,
  final: do not run, do not create a replacement window.
- `READY_FOR_FINAL_TEST` → H0020 final execution requires, **in order**:
  - [ ] 1. `uv run kalshi-weather experiment readiness h0020` →
        READY_FOR_FINAL_TEST (counts only)
  - [ ] 2. `uv run kalshi-weather research leakage-audit` → PASS
  - [ ] 3. `uv run kalshi-weather experiment preflight h0020-close-time
        --stage full` → PASS (metadata stability only — implies nothing
        about readiness or results)
  - [ ] 4. frozen hashes (`5d70303816f5…` / `434286966ed2…` / spec
        `1a2f63f7ebe7…`) + ledger chain verify
  - [ ] 5. explicit human approval
  - [ ] 6. only then the registered single-shot runner (Section B
        sequence with H0020's artifacts). None of steps 1–5 may be used
        to peek at performance early.
- `INVALID` → STOP; a frozen hash, ledger, availability, or leakage check
  failed; investigate without touching frozen files.
- A supported result authorizes **replication only** — no trading use.

---

## Decision table

| Trigger | Command | Allowed action | Forbidden action | Expected next state |
|---|---|---|---|---|
| Kalshi outage still active (Telegram OUTAGE) | `ops recovery-watch … --no-notify` | wait; watch auto-checks q15min | forcing a fill; weakening gates | RECOVERING → READY alert |
| PAPER_VALIDATION_READY | Section A checklist | ONE manual 1-contract paper run | profit-based market choice; recurring service; touching H0019/H0020 | fill validated; settlement pending |
| H0019 NOT_READY | `experiment readiness h0019` | wait | early run; metric peeking | READY after 08-25 + gates |
| H0019 READY_FOR_FINAL_TEST | Section B sequence | single final run + archive | rerun; rule change | supported → replication reg; else closed |
| H0020 COLLECTING_TRAIN | `experiment readiness h0020` | wait | anything else | COLLECTING_VALIDATION on 08-26 |
| H0020 COLLECTING_VALIDATION | same | wait | gap-data use | COLLECTING_TEST on 09-09 |
| H0020 COLLECTING_TEST | same | wait | early evaluation | READY or extension after 09-22 |
| H0020 EXTENDED_TEST_COLLECTING | same | wait (rule is automatic) | manual re-dating | READY or DEFERRED by 10-06 |
| H0020 READY_FOR_FINAL_TEST | Section B sequence w/ H0020 artifacts | single final run + archive | rerun; feature changes | supported → replication reg; else closed |
| H0020 DEFERRED_INSUFFICIENT_DATA | — | record; propose a NEW hypothesis | replacement window; forcing a run | final |
| Any integrity mismatch (hash/ledger/leakage/INVALID) | — | STOP; investigate read-only | editing frozen files; proceeding | human decision |
