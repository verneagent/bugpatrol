"""Private-key store for watermark payload decryption.

The private key lives in the environment only — never in the repository.
Two sources, both optional:

- ``FIVED_WATERMARK_PRIVATE_KEY_PEM`` — the current/primary key, which serves
  the default ``keyId`` (``diagnostic-watermark-v1``).
- ``FIVED_WATERMARK_KEYS_JSON`` — a JSON object mapping ``keyId`` to PEM for
  key rotation, so older payloads encrypted under retired keys still decrypt
  while newer payloads use the current key.

GitHub Actions ships ``FIVED_WATERMARK_PRIVATE_KEY_PEM`` as a repository
secret; local development sets the same variable (e.g. in ``~/.zshrc`` or the
watcher launchd EnvironmentVariables).
"""

from __future__ import annotations

import json
import os

from bugpatrol.watermark.types import DEFAULT_KEY_ID, ENV_KEYS_JSON, ENV_PRIVATE_KEY


class WatermarkKeyError(Exception):
    """Base class for key-store failures."""

    code = "watermark_key_error"


class WatermarkBadKeyConfig(WatermarkKeyError):
    """The environment declared keys but they were unreadable/empty."""

    code = "watermark_private_key_missing"


class WatermarkKeyNotFound(WatermarkKeyError):
    """No private key exists for the envelope's keyId (unknown key / rotation gap)."""

    code = "watermark_key_not_found"

    def __init__(self, key_id: str) -> None:
        super().__init__(f"no private key for watermark keyId {key_id!r}")
        self.key_id = key_id


class WatermarkKeyStore:
    """Resolve a private key PEM by ``keyId``.

    ``from_env()`` reads the environment on every call, so tests and hot
    reloads see fresh values; instances are cheap and stateless.
    """

    def __init__(self, keys: dict[str, str] | None = None) -> None:
        # keyId -> PEM. An explicit keys mapping wins over env (used by tests).
        self._keys = dict(keys or {})

    @classmethod
    def from_env(cls) -> WatermarkKeyStore:
        keys: dict[str, str] = {}
        primary = os.environ.get(ENV_PRIVATE_KEY, "").strip()
        if primary:
            keys[DEFAULT_KEY_ID] = primary
        raw_mapping = os.environ.get(ENV_KEYS_JSON, "").strip()
        if raw_mapping:
            try:
                parsed = json.loads(raw_mapping)
            except json.JSONDecodeError as exc:
                raise WatermarkBadKeyConfig(
                    f"{ENV_KEYS_JSON} is not valid JSON"
                ) from exc
            if not isinstance(parsed, dict):
                raise WatermarkBadKeyConfig(f"{ENV_KEYS_JSON} must be a JSON object")
            for key_id, pem in parsed.items():
                if isinstance(pem, str) and pem.strip():
                    keys[str(key_id)] = pem.strip()
        # No keys configured is a valid "feature off" state; callers check
        # has_keys() and report watermark_private_key_missing.
        return cls(keys=keys)

    def has_keys(self) -> bool:
        return bool(self._keys)

    def key_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._keys))

    def resolve(self, key_id: str) -> str:
        pem = self._keys.get(key_id)
        if not pem:
            raise WatermarkKeyNotFound(key_id)
        return pem
