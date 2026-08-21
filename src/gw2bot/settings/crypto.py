from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from gw2bot.config import ConfigurationError

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
            return cls._from_key(
                configured.encode("utf-8"),
                f"{ENCRYPTION_KEY_VARIABLE} must be 32 url-safe "
                "base64-encoded bytes. Generate one with: python -c "
                '"from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"',
            )
        path = key_file_path(database_path)
        return cls._from_key(
            _load_or_create_key(path),
            f"{path} is not a usable encryption key. Restore it from a "
            "backup, or delete it to start with a new one and set the "
            "encrypted settings again.",
        )

    @classmethod
    def _from_key(cls, key: bytes, remedy: str) -> SettingsCipher:
        """Build the cipher, reporting an unusable key as configuration.

        Both sources come through here so a corrupt key file fails the same
        way a malformed variable does - with a message naming the remedy,
        rather than a ValueError several frames deep that main() does not
        catch. The key itself is never echoed.
        """
        try:
            return cls(key)
        except (ValueError, TypeError) as exc:
            raise ConfigurationError(remedy) from exc

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
    # Every inspection of an existing key file is guarded together: exists(),
    # stat() and the read all go to the same filesystem, and any of them
    # failing means the file cannot be trusted to be both private and usable.
    # Letting one through as a raw OSError would stop the bot with a traceback
    # that names no remedy.
    try:
        exists = path.exists()
    except OSError as exc:
        raise _unreadable_key(path, exc) from exc
    if exists:
        LOGGER.debug("Settings encryption key source=file")
        _require_private(path)
        try:
            return path.read_bytes().strip()
        except OSError as exc:
            raise _unreadable_key(path, exc) from exc
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


def _unreadable_key(path: Path, exc: OSError) -> ConfigurationError:
    """One message for a key file that cannot be checked or read.

    All of these mean the same thing to an operator: the bot cannot confirm
    the key is private and usable, so it will not fall back to trusting it.
    """
    return ConfigurationError(
        f"{path} could not be read and checked for owner-only permissions "
        f"({type(exc).__name__}), so it cannot be trusted to keep the "
        "encrypted settings private. Fix its ownership and permissions, or "
        f"move the key off this filesystem by setting {ENCRYPTION_KEY_VARIABLE}."
    )


def _require_private(path: Path) -> None:
    """Narrow a key file the bot did not create to owner-only.

    The bot writes the file 0600, but one restored from a backup or dropped in
    by hand arrives with whatever mode it had, and a group- or world-readable
    key hands every stored credential to any other local user. Tightening it
    beats refusing to start over a permissions bit; a file whose permissions
    cannot even be inspected cannot be trusted and is refused.
    """
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise _unreadable_key(path, exc) from exc
    if not mode & ~KEY_FILE_MODE:
        return
    LOGGER.warning(
        "The settings encryption key file was readable beyond its owner; "
        "narrowing it. path=%s previous_mode=%s",
        path,
        oct(mode),
    )
    try:
        path.chmod(KEY_FILE_MODE)
    except OSError as exc:
        raise ConfigurationError(
            f"{path} is readable beyond its owner and its permissions could "
            f"not be narrowed ({type(exc).__name__}). Every encrypted setting "
            "would be readable by anyone who can read that file. Set its mode "
            "to 0600, or move the key off this filesystem by setting "
            f"{ENCRYPTION_KEY_VARIABLE}."
        ) from exc
