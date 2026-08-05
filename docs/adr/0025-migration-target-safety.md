# ADR 0025: Migration-target safety (2026-08-04 production migration incident)

Date: 2026-08-05
Status: Accepted

## Incident

While implementing the weather-attempt evidence layer, a disposable-database
validation script was written to check that migration `0012` applied cleanly. It:

1. built an Alembic `Config()`,
2. set `sqlalchemy.url` to a temporary SQLite file,
3. called `command.upgrade(cfg, "0012")`.

**It migrated production instead.** `alembic/env.py` contained:

```python
def get_url() -> str:
    return get_settings().database_url
```

`get_url()` never consulted `config.get_main_option("sqlalchemy.url")`, so the
caller-supplied disposable URL was **silently discarded** and the migration ran
against the configured production database, creating
`weather_collection_attempts` there.

This was not a one-off typo. The failure is general: **every** Alembic
`command.*` invocation in this repository targeted production regardless of the
URL passed to it. Any future test, script, or helper would have hit the same
trap.

## Impact

Assessed immediately, and it was contained — but it was a real unintended
production write:

- The table was created **empty**: `SELECT count(*)` returned 0. The running
  collector (PID 3035, commit `3dbebb0`) predates the attempt code and can
  neither read nor write that table, so no row could have been produced.
- The migration is purely additive; no existing table was altered.
  `weather_observations` (27,032) and `collector_runs` (2,795) were identical
  before and after.
- Detected within the same command's output — the run printed
  `Context impl PostgresqlImpl` and `Running upgrade 0011 -> 0012`, which is
  what surfaced it.
- Reverted with `alembic downgrade 0011`. The empty table was dropped, row
  counts re-verified unchanged, revision restored to `0011`.
- The collector was never restarted; no backup was needed or taken.

No data was lost or corrupted. The exposure was that production schema state
diverged from the intended state for the duration of one command, and that the
revert itself was a second unplanned production DDL operation.

## Root cause

`env.py` treated the Settings-derived URL as the only possible target and
silently overrode caller intent. There was no notion of a "disposable" target,
no way to inject a connection, and no check that a migration was pointed
somewhere safe.

## Decision

Three controls, all failing closed.

**1. Silent replacement is impossible.** `env.py` now resolves the target
explicitly. A caller-supplied `sqlalchemy.url` is either honoured (disposable
mode, approved target) or **refused loudly**. The exact incident shape — caller
URL supplied, disposable mode off — raises `MigrationTargetError` naming the
risk, rather than quietly preferring Settings.

**2. Disposable targets must prove themselves.** An opt-in flag
(`KALSHI_MIGRATION_DISPOSABLE`) is *necessary but not sufficient*; the incident
script would have happily set one. The target itself must be verifiably
disposable: in-memory SQLite, a SQLite file resolved to live under the OS temp
directory, or a Postgres database whose name starts with `test_`. Path
containment is resolved on both sides so `/tmp/../etc/x.db` cannot masquerade
as temporary. A target whose non-secret identity fingerprint matches production
is refused even if it satisfies a naming rule.

**3. Connection injection.** Disposable validation passes an already-created
connection through `config.attributes["connection"]`. `env.py` uses it directly,
never creates a second engine, and never resolves Settings on that path.

Identity comparison uses a sha256 fingerprint over
`(scheme-family, host, port, database)` only. Passwords are never read, stored,
logged, or included in any refusal message.

## Validation approach and its limits

`kalshi-weather dev validate-migration` creates a uniquely named
`test_migration_<uuid>` database on the configured Postgres server, upgrades it
through the real migration scripts via an injected connection, inspects the
resulting schema, and drops the database.

Stated limits rather than hidden ones:

- **SQLite could not be used.** An earlier migration in this repo's chain alters
  a constraint, which SQLite cannot do (`No support for ALTER of constraints in
  SQLite dialect`), so the chain is unreplayable there.
- It therefore creates and drops a database **on the production Postgres
  server**. It never connects to, reads, or writes the production *database*,
  and both the name guard and the identity fingerprint make that impossible —
  but a crash between create and drop could leave a stray `test_migration_*`
  database behind.
- It requires `CREATE DATABASE` privileges.
- It proves the upgrade applies and yields the expected structure. It does not
  prove production performance, locking, or duration under load.

## Consequences

The true schema drift is preserved: `EXPECTED_DB_REVISION` stays `0012` while
production stays `0011`, so `ops quality` reports one `unexpected_schema_change`
error until the separate deployment task applies the migration. **That finding
is correct and must not be suppressed.**
