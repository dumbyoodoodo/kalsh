from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_weather.config import Settings
from kalshi_weather.kalshi.auth import load_private_key_from_setting


@pytest.fixture()
def _isolated_env(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """Run in a temp cwd with no storage env vars and no real .env in reach,
    so tests exercise Settings defaults, not the developer's machine."""
    for var in ("KALSHI_DATA_DIR", "DATASET_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


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


def test_dataset_root_defaults_under_data_dir(_isolated_env) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.data_dir == Path("data")
    assert settings.dataset_root == Path("data/datasets")


def test_dataset_root_derives_from_kalshi_data_dir(_isolated_env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    external = _isolated_env / "external"
    monkeypatch.setenv("KALSHI_DATA_DIR", str(external))
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.dataset_root == external / "datasets"


def test_explicit_dataset_root_overrides_data_dir(_isolated_env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("KALSHI_DATA_DIR", str(_isolated_env / "external"))
    monkeypatch.setenv("DATASET_ROOT", str(_isolated_env / "elsewhere"))
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.dataset_root == _isolated_env / "elsewhere"


def test_empty_storage_env_values_mean_unset(_isolated_env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The .env.example placeholder style (`KALSHI_DATA_DIR=`) must behave
    exactly like an absent variable, not like Path(".")."""
    monkeypatch.setenv("KALSHI_DATA_DIR", "")
    monkeypatch.setenv("DATASET_ROOT", "")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.dataset_root == Path("data/datasets")
    # And the default (implicitly-configured) root is still created on demand.
    root = settings.ensure_dataset_root()
    assert root.is_dir()


def test_ensure_dataset_root_creates_default_on_demand(_isolated_env) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    root = settings.ensure_dataset_root()
    assert root == _isolated_env / "data" / "datasets"
    assert root.is_dir()


def test_ensure_dataset_root_rejects_missing_configured_root(_isolated_env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """An explicitly configured root whose parent doesn't exist means the
    external drive isn't mounted -- refuse loudly, never mkdir the wrong disk."""
    monkeypatch.setenv("KALSHI_DATA_DIR", str(_isolated_env / "not-mounted" / "kalshi-data"))
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(RuntimeError, match="does not exist"):
        settings.ensure_dataset_root()


def test_ensure_dataset_root_creates_datasets_subdir_when_root_exists(  # type: ignore[no-untyped-def]
    _isolated_env, monkeypatch
) -> None:
    mounted = _isolated_env / "mounted-drive" / "kalshi-data"
    mounted.mkdir(parents=True)
    monkeypatch.setenv("KALSHI_DATA_DIR", str(mounted))
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    root = settings.ensure_dataset_root()
    assert root == mounted / "datasets"
    assert root.is_dir()


def test_initial_trade_bootstrap_lookback_days_defaults_to_30(_isolated_env) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.initial_trade_bootstrap_lookback_days == 30


def test_initial_trade_bootstrap_lookback_days_parses_from_env(_isolated_env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("INITIAL_TRADE_BOOTSTRAP_LOOKBACK_DAYS", "7")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.initial_trade_bootstrap_lookback_days == 7


def test_ensure_dataset_root_rejects_unwritable_root(_isolated_env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    readonly = _isolated_env / "readonly"
    (readonly / "datasets").mkdir(parents=True)
    monkeypatch.setenv("KALSHI_DATA_DIR", str(readonly))
    (readonly / "datasets").chmod(0o500)
    try:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        with pytest.raises(RuntimeError, match="not writable"):
            settings.ensure_dataset_root()
    finally:
        (readonly / "datasets").chmod(0o700)
