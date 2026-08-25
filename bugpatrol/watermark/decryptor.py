"""Decrypt a watermark envelope into its JSON payload.

Ownership boundary: decryption is the ONLY job here. Extraction (finding the
envelope in an image) lives in ``extractor``; turning the payload into triage
context lives in ``reporter``. Unknown ``keyId``, wrong keys, and corrupt
ciphertexts all fail loudly with distinct error codes.
"""

from __future__ import annotations

import base64
import json
from typing import cast

from bugpatrol.watermark.envelope import parse_envelope
from bugpatrol.watermark.keys import WatermarkKeyStore
from bugpatrol.watermark.types import ERROR_BAD_PAYLOAD, ERROR_DECRYPT, PAYLOAD_REQUIRED_FIELDS


class WatermarkDecryptError(Exception):
    """The ciphertext could not be decrypted (wrong key / corruption / tamper)."""

    code = ERROR_DECRYPT


class WatermarkBadPayload(Exception):
    """Decryption succeeded but the payload is not a valid diagnostic payload."""

    code = ERROR_BAD_PAYLOAD


def decrypt_envelope(envelope: dict[str, object], key_store: WatermarkKeyStore) -> dict[str, object]:
    """Decrypt and validate an envelope, returning the payload dict.

    Raises ``WatermarkBadEnvelope`` (schema), ``WatermarkKeyNotFound``
    (unknown keyId), ``WatermarkDecryptError``, or ``WatermarkBadPayload``.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    normalized = parse_envelope(envelope)
    key_id = str(normalized["keyId"])
    pem = key_store.resolve(key_id)
    data = cast(dict[str, object], normalized["data"])
    wrapped_key = _b64decode(data["wrappedKey"])
    iv = _b64decode(data["iv"])
    ciphertext = _b64decode(data["ciphertext"])
    tag = _b64decode(data["tag"])
    try:
        private_key = load_pem_private_key(pem.encode("utf-8"), password=None)
    except (TypeError, ValueError) as exc:
        raise WatermarkDecryptError(f"cannot load private key for keyId {key_id!r}: {exc}") from exc
    if not isinstance(private_key, RSAPrivateKey):
        raise WatermarkDecryptError(f"private key for keyId {key_id!r} is not an RSA key")
    try:
        aes_key = private_key.decrypt(
            wrapped_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        plaintext = AESGCM(aes_key).decrypt(iv, ciphertext + tag, None)
    except Exception as exc:
        raise WatermarkDecryptError(f"envelope decrypt failed for keyId {key_id!r}: {exc}") from exc
    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WatermarkBadPayload("decrypted payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise WatermarkBadPayload("decrypted payload must be a JSON object")
    validate_payload(payload, key_id=key_id)
    return payload


def validate_payload(payload: dict[str, object], *, key_id: str) -> None:
    """Check a decrypted payload against the app's core schema contract."""
    if payload.get("schemaVersion") != 1:
        raise WatermarkBadPayload(
            f"unsupported payload schemaVersion: {payload.get('schemaVersion')!r}"
        )
    for field in PAYLOAD_REQUIRED_FIELDS:
        value = payload.get(field)
        if value is None or value == "":
            raise WatermarkBadPayload(f"payload missing required field: {field}")
    embedded_key_id = payload.get("keyId")
    if embedded_key_id is not None and embedded_key_id != key_id:
        raise WatermarkBadPayload(
            f"payload keyId {embedded_key_id!r} does not match envelope keyId {key_id!r}"
        )


def _b64decode(value: object) -> bytes:
    if not isinstance(value, str):
        raise WatermarkDecryptError("envelope data field is not base64 text")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise WatermarkDecryptError("envelope data field is not valid base64") from exc
