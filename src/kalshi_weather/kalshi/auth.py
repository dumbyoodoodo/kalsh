"""RSA-PSS request signing for Kalshi's authenticated endpoints.

# CONFIRMED (docs/API_VERIFICATION.md): signature message format and headers
# were cross-checked against the official Kalshi/kalshi-starter-code-python
# reference client. Sign `f"{timestamp_ms}{method}{full_path}"` with RSA-PSS
# (SHA-256, MGF1-SHA256, salt length = digest length), base64-encode the
# signature, and send it alongside the timestamp and key id as headers.
# `full_path` includes the "/trade-api/v2" prefix and excludes the query
# string -- KalshiClient builds this from the configured base URL (see
# `_signing_path_prefix` in kalshi/client.py). Confirmed against a live demo
# request: the signing format itself produces a well-formed request (no
# malformed-signature error), though the specific credentials tested were
# still rejected by Kalshi for an unrelated reason -- see the report.

This module is isolated from the HTTP client so it can be unit-tested with a
locally generated throwaway keypair -- no real credentials ever appear here
or in tests.
"""

import base64
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ACCESS_KEY_HEADER = "KALSHI-ACCESS-KEY"
ACCESS_TIMESTAMP_HEADER = "KALSHI-ACCESS-TIMESTAMP"
ACCESS_SIGNATURE_HEADER = "KALSHI-ACCESS-SIGNATURE"


class PrivateKeyLoadError(ValueError):
    """Raised when a private key cannot be read/parsed or is not an RSA key."""


def parse_private_key(pem_text: str) -> rsa.RSAPrivateKey:
    """Parse an RSA private key from raw PEM text (e.g. pasted directly into `.env`)."""
    key = serialization.load_pem_private_key(pem_text.encode("utf-8"), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise PrivateKeyLoadError("provided PEM text is not an RSA private key")
    return key


def load_private_key(path: str | Path) -> rsa.RSAPrivateKey:
    """Load an RSA private key from a PEM file on disk."""
    key_bytes = Path(path).read_bytes()
    key = serialization.load_pem_private_key(key_bytes, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise PrivateKeyLoadError(f"{path} is not an RSA private key")
    return key


def load_private_key_from_setting(value: str) -> rsa.RSAPrivateKey:
    """Load a private key from a single config value that's either raw PEM text
    (pasted directly into `.env`) or a path to a PEM file on disk."""
    if value.strip().startswith("-----BEGIN"):
        return parse_private_key(value)
    return load_private_key(value)


@dataclass(frozen=True, slots=True)
class SignedRequestHeaders:
    key_id: str
    timestamp_ms: str
    signature_b64: str

    def as_headers(self) -> dict[str, str]:
        return {
            ACCESS_KEY_HEADER: self.key_id,
            ACCESS_TIMESTAMP_HEADER: self.timestamp_ms,
            ACCESS_SIGNATURE_HEADER: self.signature_b64,
        }


def sign_request(
    *,
    key_id: str,
    private_key: rsa.RSAPrivateKey,
    method: str,
    path: str,
    timestamp_ms: str | None = None,
) -> SignedRequestHeaders:
    """Sign a request per Kalshi's RSA-PSS scheme.

    `path` must be the request path Kalshi expects in the signed message
    (see module docstring for the unverified assumption about its exact form).
    """
    ts = timestamp_ms if timestamp_ms is not None else str(int(time.time() * 1000))
    message = f"{ts}{method.upper()}{path}".encode()
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
        hashes.SHA256(),
    )
    return SignedRequestHeaders(
        key_id=key_id,
        timestamp_ms=ts,
        signature_b64=base64.b64encode(signature).decode("ascii"),
    )
