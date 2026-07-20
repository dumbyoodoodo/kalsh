# Paste this into Claude Code

Read every Markdown file in this repository before changing code.

Act as a senior Python quantitative systems engineer. Build this project incrementally and safely. The long-term objective is a Kalshi weather-contract research and trading system, but the immediate objective is only **Milestone 0 and Milestone 1** from `TASKS.md`.

Requirements for this session:

1. Create the repository foundation and exact package structure described in `CLAUDE.md`.
2. Use Python 3.12+, `uv`, Pydantic settings, HTTPX, SQLAlchemy 2.x, Alembic, Typer, Ruff, mypy, pytest, pytest-asyncio, and Hypothesis.
3. Implement a read-only Kalshi market-data client.
4. Support:
   - public production market-data requests without credentials where supported
   - authenticated demo requests through an isolated RSA signing module
5. Implement typed models and pagination for series, events, markets, trades, and order books.
6. Correctly derive asks from the binary order book:
   - best YES ask = 100 - best NO bid
   - best NO ask = 100 - best YES bid
7. Persist immutable raw payloads and normalized market/order-book snapshots.
8. Add CLI commands to list series/markets and display one market/order book.
9. Add comprehensive unit tests, including empty-side order books and price boundaries.
10. Add opt-in integration tests that skip cleanly when demo credentials are absent.
11. Do not implement any order-submission endpoint.
12. Do not add ML, dashboards, Redis, Celery, Kubernetes, or cloud deployment.

Before coding, output:

- proposed file tree
- implementation sequence
- acceptance criteria
- any API assumptions that must be confirmed against current official Kalshi documentation

Then implement. After implementation, run all checks and provide a concise engineering report with commands, results, limitations, and the next task.
