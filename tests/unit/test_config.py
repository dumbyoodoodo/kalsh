from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_weather.config import Settings
from kalshi_weather.kalshi.auth import load_private_key_from_setting


def test_env_file_supports_multiline_quoted_pem_value(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Confirms a private key pasted directly into .env (quoted, multi-line)
    round-trips correctly through pydantic-settings' dotenv parsing -- this is
    the exact scenario a user pasting a key from Kalshi's dashboard hits."""
    monkeypatch.delenv("KALSHI_DEMO_PRIVATE_KEY", raising=False)

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_text = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")

    env_path = tmp_path / ".env"
    env_path.write_text(f'KALSHI_DEMO_PRIVATE_KEY="{pem_text}"\n')

    settings = Settings(_env_file=env_path)  # type: ignore[call-arg]

    assert settings.kalshi_demo_private_key is not None
    assert settings.kalshi_demo_private_key.strip() == pem_text.strip()

    loaded = load_private_key_from_setting(settings.kalshi_demo_private_key)
    assert isinstance(loaded, rsa.RSAPrivateKey)
