"""Watermark envelope schema, validation, and reference encryptor.

The envelope is the JSON object that gets embedded into the screenshot and
decrypted on the server. It carries the ``keyId`` (for key rotation) plus a
hybrid-RSA/AES ciphertext:

.. code-block:: json

    {
      "v": 1,
      "keyId": "diagnostic-watermark-v1",
      "alg": "RSA-OAEP-256+AES-256-GCM",
      "data": {
        "ciphertext": "<base64 AES-GCM ciphertext of the payload JSON>",
        "iv": "<base64 12-byte nonce>",
        "tag": "<base64 16-byte GCM tag>",
        "wrappedKey": "<base64 RSA-OAEP-256 wrapped AES-256 key>"
      }
    }

``build_envelope`` is the reference encryptor. The app owns encryption and
embedding; bugpatrol keeps this implementation so tests can mint fixtures and
so the app team has a canonical behavior to match. It needs only the **public**
key — the private key never touches this module.
"""

from __future__ import annotations

import base64
import json
import os

ENVELOPE_VERSION = 1
ENVELOPE_ALG = "RSA-OAEP-256+AES-256-GCM"

_REQUIRED_DATA_KEYS = ("ciphertext", "iv", "tag", "wrappedKey")


class WatermarkBadEnvelope(Exception):
    """The envelope JSON is structurally invalid."""


def build_envelope(
    payload: dict[str, object],
    *,
    public_key_pem: str,
    key_id: str,
) -> dict[str, object]:
    """Encrypt a payload into a watermark envelope (reference implementation).

    Uses the public key for the given ``keyId`` only. Mirrors the contract the
    app must implement; the private key is never required here.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    public_key = load_pem_public_key(public_key_pem.encode("utf-8"))
    if not isinstance(public_key, RSAPublicKey):
        raise ValueError("watermark public key is not an RSA key (RSA-OAEP-256 requires RSA)")
    aes_key = os.urandom(32)
    iv = os.urandom(12)
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ciphertext_and_tag = AESGCM(aes_key).encrypt(iv, plaintext, None)
    wrapped_key = public_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return {
        "v": ENVELOPE_VERSION,
        "keyId": key_id,
        "alg": ENVELOPE_ALG,
        "data": {
            "ciphertext": _b64(ciphertext_and_tag[:-16]),
            "iv": _b64(iv),
            "tag": _b64(ciphertext_and_tag[-16:]),
            "wrappedKey": _b64(wrapped_key),
        },
    }


def parse_envelope(raw: object) -> dict[str, object]:
    """Validate an envelope dict and return it normalized.

    Raises ``WatermarkBadEnvelope`` when any required field is missing or
    malformed.
    """
    if not isinstance(raw, dict):
        raise WatermarkBadEnvelope("envelope must be a JSON object")
    version = raw.get("v")
    if version != ENVELOPE_VERSION:
        raise WatermarkBadEnvelope(f"unsupported envelope version: {version!r}")
    key_id = raw.get("keyId")
    if not isinstance(key_id, str) or not key_id:
        raise WatermarkBadEnvelope("envelope keyId is missing or not a string")
    alg = raw.get("alg")
    if alg != ENVELOPE_ALG:
        raise WatermarkBadEnvelope(f"unsupported envelope algorithm: {alg!r}")
    data = raw.get("data")
    if not isinstance(data, dict):
        raise WatermarkBadEnvelope("envelope data is missing")
    for key in _REQUIRED_DATA_KEYS:
        value = data.get(key)
        if not isinstance(value, str) or not value:
            raise WatermarkBadEnvelope(f"envelope data.{key} is missing or not base64")
    return {"v": ENVELOPE_VERSION, "keyId": key_id, "alg": ENVELOPE_ALG, "data": data}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
