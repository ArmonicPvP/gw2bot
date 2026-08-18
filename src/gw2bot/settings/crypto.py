from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

LOGGER = logging.getLogger(__name__)

ENCRYPTION_KEY_VARIABLE = "SETTINGS_ENCRYPTION_KEY"
KEY_FILE_NAME = "settings.key"
# Only the owner may read the key: it is the one thing standing between a
# copied database file and the credentials inside it.
KEY_FILE_MODE = 0o600


class SettingsCipher:
    """Encrypts the settings that carry credentials.

    The key comes from ``SETTINGS_ENCRYPTION_KEY`` when it is set, and
    otherwise from a file beside the database, generated on first use. The
    environment variable is what an operator reaches for when they want the key
    off the data volume; the file is what makes the feature work with no
    configuration at all.

    Decryption failures are reported rather than raised. Losing the key must
    degrade to "set the secret again with /settings", not to a bot that cannot
    start.
    """

    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    @classmethod
    def for_database(
        cls,
        database_path: str,
        env: os._Environ[str] | dict[str, str] | None = None,
    ) -> SettingsCipher:
        values = os.environ if env is None else env
        configured = values.get(ENCRYPTION_KEY_VARIABLE, "").strip()
        if configured:
            LOGGER.debug("Settings encryption key source=env")
            return cls(configured.encode("utf-8"))
        return cls(_load_or_create_key(key_file_path(database_path)))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str, setting_name: str) -> str | None:
        """Return the plaintext, or None when this key cannot read it."""
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, TypeError) as exc:
            # Never log the token: it is the ciphertext of a live credential.
            LOGGER.error(
                "Could not decrypt a stored secret; it is treated as unset "
                "until it is set again. setting=%s error_type=%s",
                setting_name,
                type(exc).__name__,
            )
            return None


def key_file_path(database_path: str) -> Path:
    return Path(database_path).parent / KEY_FILE_NAME


def _load_or_create_key(path: Path) -> bytes:
    if path.exists():
        LOGGER.debug("Settings encryption key source=file")
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    # Create the file already private rather than writing first and narrowing
    # the mode after, which would leave the key world-readable in between.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, KEY_FILE_MODE)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    LOGGER.info(
        "Generated a settings encryption key; keep %s with the database or "
        "set %s to supply your own",
        path.name,
        ENCRYPTION_KEY_VARIABLE,
    )
    LOGGER.debug("Settings encryption key source=generated")
    return key
