# API Verification Report

## 1. Date and environment tested

- **Date:** 2026-07-20 (UTC)
- **Environment tested:** `demo` (`KALSHI_ENV=demo`, base URL `https://demo-api.kalshi.co/trade-api/v2`), confirmed from `.env` before any request was made. Production was never targeted with these credentials or write requests.
- **No write operations occurred.** Only `GET` requests were issued throughout. No order-submission, cancellation, key-rotation, or transfer endpoint exists in this codebase or was called.

## 2. Git commit tested

No commits exist yet in this repository (`git log` reports no history — the working tree has never been committed). This verification was run against the working tree as of the date above; see "Code changes made" (§8) for the diff produced during this task.

## 3. Official specifications consulted

- `docs.kalshi.com` (including `/openapi.yaml`, `/asyncapi.yaml`) was **unreachable from this environment** — both the WebFetch tool and direct HTTPS requests from this sandbox to `docs.kalshi.com` fail with `ECONNRESET`/`ConnectError` consistently. The live API host (`demo-api.kalshi.co`) and `raw.githubusercontent.com` were reachable normally, so this is specific to the docs host, not a general network outage.
- In place of the primary docs, this verification relied on:
  - **`github.com/Kalshi/kalshi-starter-code-python`** (`clients.py`, fetched directly via raw.githubusercontent.com) — this is Kalshi's own official reference client and was treated as authoritative. It confirmed base URLs, the RSA-PSS signing algorithm and parameters, the exact signed-message format (including the `/trade-api/v2` path prefix), the WebSocket URL/suffix, and the WebSocket subscribe message shape.
  - Web search summaries of `docs.kalshi.com` pages (rate limits, WebSocket channels) for supplementary context, clearly lower-confidence than the official starter code or live behavior.
  - **Live demo API responses** as ground truth wherever they conflicted with search-summarized documentation, per this task's own instruction to treat live read-only behavior as operational evidence.

## 4. Commands executed

```
uv run --no-editable ruff check .        # via .venv/bin/ruff directly (see §14 note)
uv run --no-editable mypy src            # via .venv/bin/mypy directly
uv run --no-editable pytest -v           # via PYTHONPATH=src .venv/bin/pytest -v
alembic upgrade head / downgrade base    # via python -m alembic
python -m kalshi_weather.cli market <ticker>
python -m kalshi_weather.cli orderbook show <ticker>
```
Plus a series of one-off Python scripts (not committed) making direct `GET` calls through `KalshiClient` to `/exchange/status`, `/series`, `/events`, `/markets`, `/markets/{ticker}`, `/markets/{ticker}/orderbook`, `/markets/trades`, and `/portfolio/{balance,positions,orders,fills}`.

## 5. Endpoints tested

| Endpoint | Auth | Result |
|---|---|---|
| `GET /exchange/status` | none | 200, parsed |
| `GET /series` | none | 200, parsed (12,208 items, single page, no cursor) |
| `GET /events` | none | 200, parsed |
| `GET /markets` | none | 200, parsed, real multi-page cursor pagination exercised |
| `GET /markets/{ticker}` | none | 200, parsed |
| `GET /markets/{ticker}/orderbook` | none | 200, parsed (empty and one-sided books observed) |
| `GET /markets/trades` | none | 200, parsed |
| `GET /portfolio/balance` | signed | ~~401~~ **200, parsed** (resolved, see below) |
| `GET /portfolio/positions` | signed | ~~401~~ **200, parsed** (resolved, see below) |
| `GET /portfolio/orders` | signed | ~~401~~ **200, parsed** (resolved, see below) |
| `GET /portfolio/fills` | signed | ~~401~~ **200, parsed** (resolved, see below) |
| `GET /series` (signed, credentials attached) | signed | 200, parsed — confirms signing mechanism itself is functional |

**Update (same day, after initial report):** the 401s above were caused by the
configured API key belonging to a production Kalshi account rather than a
demo-specific one — confirmed by the user and resolved by generating a new
key from the demo account's own dashboard. `.env` needed the same
unquoted-multi-line-key fix as before (`python-dotenv` truncates an
unquoted multi-line value to its first line) after the key was replaced;
fixed the same way, without printing key contents. With the corrected demo
key, all four `/portfolio/*` endpoints now return 200 with the expected
top-level shape (`balance`/`balance_breakdown`/`balance_dollars`/
`portfolio_value`/`updated_ts` for balance; `cursor` + a list field for the
other three) — response bodies were not inspected further and no field
values are reproduced here per this report's own rule against including
account information. This confirms the RSA-PSS signing implementation
(including the `/trade-api/v2` path-prefix fix from §7-8) is fully correct;
the earlier failure was entirely credential/account state, not a code
defect, exactly as diagnosed in the original report.

## 6. Confirmed assumptions

- Demo base URL `https://demo-api.kalshi.co/trade-api/v2` and production `https://api.elections.kalshi.com/trade-api/v2` — confirmed correct against the official starter code.
- Query strings are excluded from the signed message — confirmed against the official starter code.
- 429 responses require exponential backoff without relying on `Retry-After` (Kalshi's rate limiter does not send it) — our client already didn't depend on it; confirmed correct as-is.
- `KALSHI-ACCESS-KEY` / `KALSHI-ACCESS-TIMESTAMP` / `KALSHI-ACCESS-SIGNATURE` header names — confirmed correct.
- Order book endpoint returns only resting bids per side (no direct asks) — confirmed; our derivation `yes_ask = 100 - no_bid`, `no_ask = 100 - yes_bid` is exercised correctly against live one-sided and empty books.
- WebSocket suffix `/trade-api/ws/v2`, demo host `demo-api.kalshi.co` — confirmed against the official starter code (not live-connection-tested; see §11).
- Pagination cursor semantics (empty/absent cursor = last page) — confirmed against real, genuinely multi-page `/markets` responses.

## 7. Incorrect assumptions found

1. **Signed path was missing the `/trade-api/v2` prefix.** The client signed only the path relative to `base_url` (e.g. `/portfolio/balance`); the official reference signs the full path including the API version prefix (e.g. `/trade-api/v2/portfolio/balance`). Fixed (§8).
2. **Order book envelope/field names have changed.** Live responses use `orderbook_fp: {yes_dollars, no_dollars}` with `[price_dollar_string, quantity_string]` levels, not the assumed `orderbook: {yes, no}` with `[price_cents_int, quantity_int]` levels. Fixed via a normalizing validator (§8).
3. **Market/Trade price and quantity fields have been renamed.** Live responses use `yes_bid_dollars`/`yes_ask_dollars`/`no_bid_dollars`/`no_ask_dollars`/`last_price_dollars` (decimal-dollar strings) and `volume_fp`/`open_interest_fp`/`count_fp` (fixed-point strings) instead of the plain integer fields (`yes_bid`, `volume`, `count`, ...) the models assumed — the plain fields are **absent from the payload entirely**, not merely null. Fixed via normalizing validators (§8).
4. **`market_snapshots.event_ticker` had an enforced foreign key to `events.event_ticker`.** Since no current CLI command populates `events`, this made `market show` crash with `ForeignKeyViolation` against any real market. Fixed by dropping the constraint (§8) — this was caught only by live end-to-end testing, not by unit tests against synthetic fixtures.
5. **`.env`'s multi-line private key was unquoted**, so `python-dotenv` silently read only its first line, producing a malformed-PEM error at signing time. Fixed by rewrapping the value in quotes directly in `.env` (not committed; local file only).
6. **Minor:** the hand-crafted `series_list.json` fixture used category `"Weather"`; the real category value is `"Climate and Weather"`. Not a code defect (nothing in `src/` hardcodes the old value), but fixed for fixture accuracy.
7. **Pagination had no safety bound.** `paginate()` would loop forever against a server bug that repeated or never emptied its cursor. Added `max_pages` (default 1000) and repeated-cursor detection (§8) — not triggered by any real response observed, added defensively per this task's explicit requirement.

## 8. Code changes made

All changes are the smallest fix that addresses the confirmed defect; no unrelated redesign.

- `src/kalshi_weather/kalshi/client.py` — sign the full path (base-URL path prefix + relative path), not just the relative path.
- `src/kalshi_weather/kalshi/auth.py` — docstring updated from `# ASSUMPTION` to `# CONFIRMED`, documenting the now-verified signing format.
- `src/kalshi_weather/kalshi/models.py` — added `model_validator(mode="before")` normalizers on `Market`, `OrderbookResponse`, and `Trade` that populate the existing integer-cents/integer-count fields from the live `*_dollars`/`*_fp` wire fields when the plain fields are absent. Downstream code (order-book reconstruction, CLI, storage) is unchanged — the normalization happens entirely inside model parsing, preserving backward compatibility with the original (still-supported) integer-based wire shape.
- `src/kalshi_weather/kalshi/pagination.py` — added `max_pages` safety limit (default 1000) and repeated/malformed-cursor detection (`PaginationSafetyError`).
- `src/kalshi_weather/storage/models.py` and `src/kalshi_weather/alembic/versions/0001_initial.py` — dropped the foreign key from `market_snapshots.event_ticker` to `events.event_ticker` (column retained, unconstrained). This migration has never been run against real persistent data outside this session's local testing, so it was corrected in place rather than patched with a new migration.
- `tests/fixtures/kalshi/series_list.json`, `tests/unit/test_kalshi_models.py` — corrected the example category value.
- `.env` (not committed; local only) — rewrapped the private key value in quotes so it parses as a complete multi-line PEM instead of being silently truncated to its first line.

## 9. Tests added or updated

- `tests/unit/test_kalshi_live_schema.py` (new) — 6 tests parsing real, sanitized fixtures captured from the live demo API (`tests/fixtures/kalshi/live/`), confirming the `*_dollars`/`*_fp` → integer-cents/count normalization and the `orderbook_fp` envelope handling against actual wire data, not just synthetic assumptions.
- `tests/unit/test_kalshi_client.py` — 4 new tests: timeout-triggers-retry, connection-error-exhausts-retries, malformed-JSON-response, unexpected-content-type (HTML error page). All use `httpx.MockTransport`, no live calls.
- `tests/unit/test_pagination.py` — 2 new tests: repeated-cursor safety error, `max_pages` safety error.
- All new fixtures under `tests/fixtures/kalshi/live/` contain only public market data (tickers, prices, quantities, timestamps) — no account identifiers, balances, positions, or credentials. Reviewed individually before being written to the repo.

## 10. Test results

Baseline (before any change, §Step 3): **56 passed, 1 skipped**, ruff clean, mypy clean.

Final (after all fixes, actually executed):
```
ruff check .        -> All checks passed!
mypy src             -> Success: no issues found in 19 source files
pytest -v            -> 68 passed, 1 skipped in 1.29s
```
The 1 skip is `tests/integration/test_kalshi_demo_client.py`, which skips by design unless `KALSHI_DEMO_API_KEY_ID`/`KALSHI_DEMO_PRIVATE_KEY` are present in the process environment (as opposed to just `.env`). It was separately run with those variables exported into a subprocess environment (never printed) and **passed** against the live demo API.

Alembic: `downgrade base` then `upgrade head` both ran cleanly against a real local Postgres (via `docker compose up -d postgres`) after the foreign-key fix.

CLI end-to-end (against live data, with local Postgres): `kalshi-weather market <ticker>` and `kalshi-weather orderbook show <ticker>` both ran successfully against real tickers discovered dynamically from `/markets` (not hardcoded), including a case where the order book was genuinely empty (confirmed correct `None` output) and a case with real resting bids (confirmed correct derived ask).

## 11. WebSocket verification results

**No WebSocket client exists in this codebase** (`src/kalshi_weather/kalshi/` has no `websocket.py`; `TASKS.md` places it in Milestone 1's original file tree but ADR 0001 already documents it as deliberately deferred — no consumer yet). Per this task's explicit instruction, this is documented rather than treated as a gap to fill or expanded into a new milestone.

For when it is built (Milestone 3+ or later), the reference details confirmed during this task:
- URL: `wss://demo-api.kalshi.co/trade-api/ws/v2` (demo), `wss://api.elections.kalshi.com/trade-api/ws/v2` (production).
- Auth: same RSA-PSS signing as REST, sent as connection headers during the handshake, signing `method="GET"`, `path="/trade-api/ws/v2"`.
- Subscribe message: `{"id": <int>, "cmd": "subscribe", "params": {"channels": [...], "market_tickers": [...]}}`.
- `orderbook_delta` channel sends an initial `orderbook_snapshot` then incremental `orderbook_delta` messages.

No live WebSocket connection was attempted (nothing to test).

## 12. Rate-limit findings

- Kalshi uses a token-bucket rate limiter with per-tier budgets (`GET /account/limits`, `GET /account/endpoint_costs` for introspection — not tested, both require the same portfolio-scoped auth that's currently blocked, see §7 item and §13).
- 429 responses do **not** include `Retry-After` or `X-RateLimit-*` headers. Our client's retry logic already used fixed exponential backoff without depending on either header — confirmed correct, no change needed.
- No 429 was intentionally provoked (per this task's explicit instruction not to flood the API); 429/5xx retry behavior was verified via `httpx.MockTransport`, not live traffic.

## 13. Remaining unverified areas

- ~~`/portfolio/*` endpoints return 401 for the configured demo credentials~~ **Resolved** — the original key belonged to a production account; a key generated from the demo account's own dashboard works correctly on the first try. See the update note under §5.
- **A genuinely deep, two-sided liquid order book was not found** in the demo markets scanned (~30+ markets checked); only empty and one-sided books were observed. The reconstruction logic is exhaustively unit-tested for the two-sided case already (`tests/unit/test_orderbook.py`, synthetic), but this specific live shape wasn't available to sample during this session.
- **Fractional quantities** (`count_fp: "0.91"` observed on one real trade) are rounded to the nearest whole contract by the current fix. If Kalshi genuinely supports fractional contract sizes, this rounding could distort volume/depth figures. Not resolved further — flagged for follow-up before this data is used for real analysis.
- **Sub-cent ("deci_cent") prices**: all live prices observed during this session were exact cent multiples even in 4-decimal-place dollar-string form; no genuinely sub-cent price was observed to confirm or refute how the current rounding-to-nearest-cent fix would behave in that case.
- **`docs.kalshi.com` is unreachable from this environment** (§3) — anything not cross-checked against the official starter-code repo or live behavior remains a best-effort search-summary claim (e.g. exact rate-limit tier numbers, `/account/limits` response shape).
- **`markets list` with no narrowing filter can page through the entire exchange** (observed hanging past 60s against `--status open` alone, likely hundreds+ of pages). Not a correctness bug, but a CLI usability gap worth a `--limit`/max-pages flag in a future task.

## 14. Notes on tooling

`uv run` continues to be unreliable in this specific local environment (hangs indefinitely on trivial commands, unrelated to this task's changes — a pre-existing, previously-reported issue). All verification in this task was run directly via `.venv/bin/{ruff,mypy,pytest}` and `PYTHONPATH=src`, which are unaffected and produce identical results.

## Overall status: Passed

All completion-standard criteria are now met: authentication succeeds (with a
correctly-scoped demo key), multiple real REST responses deserialize
successfully, pagination is confirmed against real multi-page data,
order books parse correctly (empty and one-sided cases observed live;
two-sided covered by exhaustive synthetic unit tests), typed models match
observed live payloads after the fixes in §8, all tests pass, no secrets
were exposed, no write operations occurred, and remaining uncertainties
(§13) are explicitly documented rather than papered over.

## Recommended next action

Proceed to Phase 2 (Historical Data Platform) per `TASKS.md`/`ROADMAP.md` —
the read-only market-data gateway it depends on is now verified against live
data (not just synthetic assumptions) with working authentication.
