"""Collector service preflight: fail loudly, before the collector starts, if
the environment the service depends on is unavailable.

Checks, in order:
  1. Settings load (.env parse).
  2. The configured data root exists and is writable (ensure_dataset_root:
     an explicitly configured external root that's missing means the drive
     isn't mounted -- refuse to start rather than strand writes).
  3. Demo API credentials are present and the private key actually loads
     (never printed; only pass/fail is reported).
  4. The database answers SELECT 1 (5s timeout) -- after a reboot this is
     what fails until Docker Desktop has brought Postgres up, and the
     service's crash-loop-with-throttle turns that into "retry until up".

Exit 0 = safe to start. Exit 1 = do not start (launchd will retry).
"""

import sys
from typing import NoReturn


def fail(msg: str) -> NoReturn:
    """Always exits. Typed `NoReturn` so type checkers narrow the optional
    settings guarded by it -- e.g. `kalshi_demo_private_key` is provably
    non-None at the `.get_secret_value()` call below."""
    print(f"PREFLIGHT FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    try:
        from kalshi_weather.config import get_settings
    except Exception as exc:
        fail(f"cannot import kalshi_weather (venv broken?): {exc}")

    try:
        settings = get_settings()
    except Exception as exc:
        fail(f"settings failed to load (.env problem?): {exc}")

    try:
        root = settings.ensure_dataset_root()
    except Exception as exc:
        fail(str(exc))

    if not settings.kalshi_demo_api_key_id:
        fail("KALSHI_DEMO_API_KEY_ID is not set")
    if not settings.kalshi_demo_private_key:
        fail("KALSHI_DEMO_PRIVATE_KEY is not set")
    try:
        from kalshi_weather.kalshi.auth import load_private_key_from_setting

        # Unwrapped here and nowhere else: the path-or-inline PEM parser is
        # the only thing in preflight that needs plaintext. The failure branch
        # below reports the exception *type* only, never the value.
        load_private_key_from_setting(settings.kalshi_demo_private_key.get_secret_value())
    except Exception as exc:
        # Never echo key material or paths derived from it beyond the setting name.
        fail(f"KALSHI_DEMO_PRIVATE_KEY did not load: {type(exc).__name__}")

    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(
            settings.database_url, connect_args={"connect_timeout": 5}
        )
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception as exc:
        fail(
            "database not reachable (is Postgres up? "
            f"docker compose up -d postgres): {type(exc).__name__}"
        )

    print(f"PREFLIGHT OK: data root {root}, credentials loaded, database reachable")


if __name__ == "__main__":
    main()
