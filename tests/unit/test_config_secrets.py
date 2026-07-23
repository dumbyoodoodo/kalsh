"""Secret-masking guarantees for `Settings` (2026-07-23 hardening).

These tests exist because of a real disclosure: a SQLAlchemy `ArgumentError`
formatted a whole `Settings` instance into its message after a mistyped call
argument, printing a live Telegram bot token. The leak was in a third-party
library's *failure* path, triggered by a call that looked entirely benign, so
no call-site convention could have caught it.

Every assertion here therefore checks that the **actual synthetic secret
string is absent** from some rendering, rather than that asterisks are
present -- "contains `**********`" would pass even if the plaintext were
sitting right next to it.

All secrets used here are synthetic. No real credential file is read, and no
assertion message embeds a secret value (`assert x not in y` prints only the
operands' repr on failure, and the masked renderings are what we assert on).
"""

import json
import logging
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from pydantic import SecretStr, ValidationError

from kalshi_weather.config import Settings
from kalshi_weather.kalshi.auth import load_private_key_from_setting

#: Shaped like a real Telegram token so a naive regex scan would flag it, but
#: not a real one.
SYNTHETIC_TOKEN = "1234567890:AA-SYNTHETIC-TOKEN-NOT-A-REAL-CREDENTIAL-000"
SYNTHETIC_KEY_ID = "00000000-dead-beef-0000-synthetickeyid"
#: Distinctive marker searched for inside inline-PEM assertions.
PEM_MARKER = "BEGIN PRIVATE KEY"


@pytest.fixture()
def synthetic_pem() -> str:
    """A throwaway RSA key generated in-process. Never touches disk, never
    read from the developer's real `secrets/` directory."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "ALERT_TELEGRAM_BOT_TOKEN": SYNTHETIC_TOKEN,
        "ALERT_TELEGRAM_CHAT_ID": "99999",
        "KALSHI_DEMO_API_KEY_ID": SYNTHETIC_KEY_ID,
        "DATABASE_URL": "postgresql+psycopg://u:sup3rsecret@localhost:5432/db",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg,arg-type]


# --- Field typing -----------------------------------------------------------


def test_sensitive_fields_are_secretstr() -> None:
    s = _settings()
    assert isinstance(s.alert_telegram_bot_token, SecretStr)
    assert isinstance(s.kalshi_demo_api_key_id, SecretStr)
    s2 = _settings(KALSHI_DEMO_PRIVATE_KEY="secrets/x.pem")
    assert isinstance(s2.kalshi_demo_private_key, SecretStr)


def test_non_sensitive_fields_stay_plain_and_readable() -> None:
    """Deliberate scope limit: masking a delivery destination or a public URL
    buys nothing and makes routing problems harder to diagnose."""
    s = _settings()
    assert s.alert_telegram_chat_id == "99999"
    assert s.kalshi_demo_base_url.startswith("https://")
    assert "99999" in repr(s)  # chat id must remain visible for diagnostics


# --- repr / str -------------------------------------------------------------


def test_token_absent_from_repr_and_str() -> None:
    s = _settings()
    assert SYNTHETIC_TOKEN not in repr(s)
    assert SYNTHETIC_TOKEN not in str(s)
    assert SYNTHETIC_TOKEN not in f"{s}"
    assert SYNTHETIC_TOKEN not in "{}".format(s)  # noqa: UP032


def test_key_id_absent_from_repr() -> None:
    s = _settings()
    assert SYNTHETIC_KEY_ID not in repr(s)
    assert SYNTHETIC_KEY_ID not in str(s)


def test_secret_field_repr_is_masked() -> None:
    s = _settings()
    assert s.alert_telegram_bot_token is not None
    assert SYNTHETIC_TOKEN not in repr(s.alert_telegram_bot_token)
    assert SYNTHETIC_TOKEN not in str(s.alert_telegram_bot_token)


def test_inline_pem_absent_from_repr(synthetic_pem: str) -> None:
    """The highest-severity case: a PEM pasted directly into .env."""
    s = _settings(KALSHI_DEMO_PRIVATE_KEY=synthetic_pem)
    assert PEM_MARKER not in repr(s)
    assert PEM_MARKER not in str(s)
    assert s.kalshi_demo_private_key is not None
    assert PEM_MARKER not in repr(s.kalshi_demo_private_key)


def test_database_password_absent_from_repr() -> None:
    """`database_url` stays a plain `str` for call-site compatibility, so the
    name-pattern guard in __repr_args__ is what protects it."""
    s = _settings()
    assert "sup3rsecret" not in repr(s)
    assert "sup3rsecret" not in str(s)


def test_repr_guard_masks_an_untyped_future_secret_field() -> None:
    """Defence-in-depth: a contributor adding a credential as a plain `str`
    should be masked by default rather than silently regressing the original
    disclosure."""

    class FutureSettings(Settings):
        some_vendor_api_token: str = "PLAINTEXT-FUTURE-SECRET"

    s = FutureSettings(_env_file=None)  # type: ignore[call-arg]
    assert "PLAINTEXT-FUTURE-SECRET" not in repr(s)
    assert s.some_vendor_api_token == "PLAINTEXT-FUTURE-SECRET"  # value still usable


# --- Third-party formatting (the actual 2026-07-23 leak vector) -------------


def test_secret_absent_when_a_library_formats_settings_into_an_exception(
    synthetic_pem: str,
) -> None:
    """Reproduces the disclosure's shape: an unrelated library interpolating a
    misused Settings object into its own error message."""
    s = _settings(KALSHI_DEMO_PRIVATE_KEY=synthetic_pem)
    try:
        raise TypeError(f"AsyncEngine expected, got {s}")
    except TypeError as exc:
        message = str(exc)
    assert SYNTHETIC_TOKEN not in message
    assert SYNTHETIC_KEY_ID not in message
    assert PEM_MARKER not in message
    assert "sup3rsecret" not in message


# --- Serialization ----------------------------------------------------------


def test_model_dump_keeps_secrets_wrapped_not_plaintext(synthetic_pem: str) -> None:
    """Pydantic leaves SecretStr wrapped in `model_dump()`, so a dict that is
    logged or formatted still masks. Asserted explicitly because it is a
    behaviour this project now relies on."""
    s = _settings(KALSHI_DEMO_PRIVATE_KEY=synthetic_pem)
    dumped = s.model_dump()
    assert isinstance(dumped["alert_telegram_bot_token"], SecretStr)
    assert SYNTHETIC_TOKEN not in str(dumped)
    assert PEM_MARKER not in str(dumped)
    assert SYNTHETIC_TOKEN not in repr(dumped)


def test_model_dump_json_masks_secrets(synthetic_pem: str) -> None:
    s = _settings(KALSHI_DEMO_PRIVATE_KEY=synthetic_pem)
    payload = s.model_dump_json()
    assert SYNTHETIC_TOKEN not in payload
    assert SYNTHETIC_KEY_ID not in payload
    assert PEM_MARKER not in payload
    json.loads(payload)  # still valid JSON


def test_model_dump_json_does_not_leak_via_round_trip(synthetic_pem: str) -> None:
    """Guard against a future `model_dump(mode="json")` helper quietly
    unwrapping secrets during serialization."""
    s = _settings(KALSHI_DEMO_PRIVATE_KEY=synthetic_pem)
    as_json = s.model_dump(mode="json")
    assert SYNTHETIC_TOKEN not in json.dumps(as_json)
    assert PEM_MARKER not in json.dumps(as_json)


# --- Logging ----------------------------------------------------------------


def test_settings_in_a_log_record_is_masked(
    synthetic_pem: str, caplog: pytest.LogCaptureFixture
) -> None:
    s = _settings(KALSHI_DEMO_PRIVATE_KEY=synthetic_pem)
    logger = logging.getLogger("kalshi_weather.test.secrets")
    with caplog.at_level(logging.INFO):
        logger.info("settings loaded: %s", s)
        logger.info("settings repr: %r", s)
    rendered = "\n".join(r.getMessage() for r in caplog.records)
    assert SYNTHETIC_TOKEN not in rendered
    assert SYNTHETIC_KEY_ID not in rendered
    assert PEM_MARKER not in rendered
    assert "sup3rsecret" not in rendered


# --- Validation errors ------------------------------------------------------


def test_validation_error_does_not_echo_secret_values() -> None:
    """A bad value elsewhere must not drag the valid secrets into the error
    text. `live_trading_confirm` outside production is this project's own
    model-level failure."""
    with pytest.raises(ValidationError) as excinfo:
        _settings(ENABLE_LIVE_TRADING="true", LIVE_TRADING_CONFIRM="true")
    text = str(excinfo.value)
    assert SYNTHETIC_TOKEN not in text
    assert SYNTHETIC_KEY_ID not in text
    assert "sup3rsecret" not in text


def test_validation_error_on_the_secret_field_itself_hides_the_input() -> None:
    """Pydantic embeds the rejected `input_value` in ValidationError text by
    default -- for a secret field that echoes the raw value. `Settings` sets
    `hide_input_in_errors=True` to suppress it. This test fails without that
    config flag (verified), so it pins the protection rather than assuming it.
    """
    marker = "SYNTHETIC-REJECTED-INPUT"
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, ALERT_TELEGRAM_BOT_TOKEN=[marker])  # type: ignore[call-arg,arg-type]
    text = str(excinfo.value)
    assert marker not in text
    assert "ALERT_TELEGRAM_BOT_TOKEN" in text  # names the env var, so still actionable


def test_validation_error_hides_input_for_non_secret_fields_too() -> None:
    """`hide_input_in_errors` is model-wide. Confirmed here so the trade-off is
    explicit: error messages name the offending setting but never quote what
    was supplied -- accepted deliberately, since a settings object is exactly
    where a stray credential is most likely to land in the wrong field."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, COLLECTOR_INTERVAL_SECONDS="SYNTHETIC-NOT-A-FLOAT")  # type: ignore[call-arg,arg-type]
    text = str(excinfo.value)
    assert "SYNTHETIC-NOT-A-FLOAT" not in text
    assert "COLLECTOR_INTERVAL_SECONDS" in text


# --- Path-versus-inline private key behaviour (unchanged) -------------------


def test_private_key_path_mode_still_works(tmp_path: Path, synthetic_pem: str) -> None:
    key_path = tmp_path / "synthetic.pem"
    key_path.write_text(synthetic_pem)
    s = _settings(KALSHI_DEMO_PRIVATE_KEY=str(key_path))

    assert s.kalshi_demo_private_key is not None
    loaded = load_private_key_from_setting(s.kalshi_demo_private_key.get_secret_value())
    assert isinstance(loaded, rsa.RSAPrivateKey)


def test_private_key_inline_mode_still_works(synthetic_pem: str) -> None:
    s = _settings(KALSHI_DEMO_PRIVATE_KEY=synthetic_pem)

    assert s.kalshi_demo_private_key is not None
    loaded = load_private_key_from_setting(s.kalshi_demo_private_key.get_secret_value())
    assert isinstance(loaded, rsa.RSAPrivateKey)


def test_malformed_inline_pem_error_excludes_the_source_value() -> None:
    """A parse failure must report the failure, not the material. Mirrors
    `scripts/service/preflight.py`, which reports only the exception type."""
    bogus = "-----BEGIN PRIVATE KEY-----\nSYNTHETIC-GARBAGE-abc123\n-----END PRIVATE KEY-----\n"
    s = _settings(KALSHI_DEMO_PRIVATE_KEY=bogus)
    assert s.kalshi_demo_private_key is not None
    with pytest.raises(Exception) as excinfo:
        load_private_key_from_setting(s.kalshi_demo_private_key.get_secret_value())
    assert "SYNTHETIC-GARBAGE-abc123" not in str(excinfo.value)


def test_non_rsa_inline_key_error_excludes_the_source_value() -> None:
    """auth.parse_private_key rejects non-RSA keys with its own message; it
    must not quote the key material when doing so."""
    ec_pem = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode("ascii")
    )
    s = _settings(KALSHI_DEMO_PRIVATE_KEY=ec_pem)
    assert s.kalshi_demo_private_key is not None
    with pytest.raises(Exception) as excinfo:
        load_private_key_from_setting(s.kalshi_demo_private_key.get_secret_value())
    body = "".join(ec_pem.splitlines()[1:-1])[:32]
    assert body not in str(excinfo.value)
