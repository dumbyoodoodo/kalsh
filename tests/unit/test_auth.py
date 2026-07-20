import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_weather.kalshi.auth import (
    ACCESS_KEY_HEADER,
    ACCESS_SIGNATURE_HEADER,
    ACCESS_TIMESTAMP_HEADER,
    PrivateKeyLoadError,
    load_private_key,
    load_private_key_from_setting,
    parse_private_key,
    sign_request,
)


@pytest.fixture
def throwaway_keypair(tmp_path):  # type: ignore[no-untyped-def]
    """A locally generated RSA keypair for tests only -- never a real credential."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "test_key.pem"
    key_path.write_bytes(pem_bytes)
    return private_key, key_path, pem_bytes.decode("ascii")


def test_load_private_key_reads_pem(throwaway_keypair) -> None:  # type: ignore[no-untyped-def]
    _private_key, key_path, _pem_text = throwaway_keypair
    loaded = load_private_key(key_path)
    assert isinstance(loaded, rsa.RSAPrivateKey)


def test_parse_private_key_reads_raw_pem_text(throwaway_keypair) -> None:  # type: ignore[no-untyped-def]
    _private_key, _key_path, pem_text = throwaway_keypair
    loaded = parse_private_key(pem_text)
    assert isinstance(loaded, rsa.RSAPrivateKey)


def test_parse_private_key_rejects_non_rsa() -> None:
    from cryptography.hazmat.primitives.asymmetric import ed25519

    key = ed25519.Ed25519PrivateKey.generate()
    pem_text = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")

    with pytest.raises(PrivateKeyLoadError):
        parse_private_key(pem_text)


def test_load_private_key_from_setting_detects_raw_pem_text(throwaway_keypair) -> None:  # type: ignore[no-untyped-def]
    _private_key, _key_path, pem_text = throwaway_keypair
    loaded = load_private_key_from_setting(pem_text)
    assert isinstance(loaded, rsa.RSAPrivateKey)


def test_load_private_key_from_setting_detects_path(throwaway_keypair) -> None:  # type: ignore[no-untyped-def]
    _private_key, key_path, _pem_text = throwaway_keypair
    loaded = load_private_key_from_setting(str(key_path))
    assert isinstance(loaded, rsa.RSAPrivateKey)


def test_load_private_key_from_setting_handles_leading_whitespace(throwaway_keypair) -> None:  # type: ignore[no-untyped-def]
    _private_key, _key_path, pem_text = throwaway_keypair
    loaded = load_private_key_from_setting(f"  \n{pem_text}")
    assert isinstance(loaded, rsa.RSAPrivateKey)


def test_load_private_key_rejects_non_rsa(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from cryptography.hazmat.primitives.asymmetric import ed25519

    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "ed25519_key.pem"
    key_path.write_bytes(pem)

    with pytest.raises(PrivateKeyLoadError):
        load_private_key(key_path)


def test_sign_request_produces_headers_and_valid_signature(throwaway_keypair) -> None:  # type: ignore[no-untyped-def]
    private_key, _key_path, _pem_text = throwaway_keypair
    public_key = private_key.public_key()

    signed = sign_request(
        key_id="test-key-id",
        private_key=private_key,
        method="GET",
        path="/trade-api/v2/series",
        timestamp_ms="1700000000000",
    )
    headers = signed.as_headers()

    assert headers[ACCESS_KEY_HEADER] == "test-key-id"
    assert headers[ACCESS_TIMESTAMP_HEADER] == "1700000000000"
    assert headers[ACCESS_SIGNATURE_HEADER]

    signature = base64.b64decode(headers[ACCESS_SIGNATURE_HEADER])
    message = b"1700000000000GET/trade-api/v2/series"
    public_key.verify(
        signature,
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
        hashes.SHA256(),
    )


def test_sign_request_defaults_timestamp_to_now(throwaway_keypair) -> None:  # type: ignore[no-untyped-def]
    private_key, _key_path, _pem_text = throwaway_keypair
    signed = sign_request(key_id="k", private_key=private_key, method="GET", path="/x")
    assert signed.timestamp_ms.isdigit()
