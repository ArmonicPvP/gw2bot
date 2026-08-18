from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_URL_QUERY_PATTERN = re.compile(
    r"(?i)\b(https?://[^\s?\"'<>]+)\?[^\s\"'<>]*"
)
LOG_SECRET_PATTERNS = (
    re.compile(
        r"(?i)([?&](?:access_token|api[_-]?key|discord_token|gw2_api_key|"
        r"subtoken|token)=)[^&\s]+"
    ),
    re.compile(
        r"""(?ix)
        (
            ["']?
            (?:authorization|access_token|api[_-]?key|discord_token|
               gw2_api_key|subtoken|token)
            ["']?
            \s*[:=]\s*
            ["']?
            (?:(?:bearer|bot)\s+)?
        )
        [^"',}\s&]+
        """
    ),
)


class SecretRegistry:
    """The secrets the console formatter must never print.

    /settings can hand the bot a new API key or session secret while it is
    running, long after configure_logging installed its handler, so the set has
    to be shared and mutable rather than frozen into the formatter at startup.
    Registering a secret is one-way: a value that has ever been live stays
    redacted, because log records written before it was replaced may still be
    in flight.
    """

    def __init__(self, secrets: Iterable[str | None] = ()) -> None:
        self._secrets: set[str] = set()
        self.update(secrets)

    def add(self, secret: str | None) -> None:
        if secret:
            self._secrets.add(secret)

    def update(self, secrets: Iterable[str | None]) -> None:
        for secret in secrets:
            self.add(secret)

    def current(self) -> tuple[str, ...]:
        return tuple(self._secrets)

    def __len__(self) -> int:
        return len(self._secrets)


Secrets = SecretRegistry | Sequence[str]


def _secret_values(secrets: Secrets) -> tuple[str, ...]:
    if isinstance(secrets, SecretRegistry):
        return secrets.current()
    return tuple(secrets)


def redact_log_text(message: str, secrets: Secrets = ()) -> str:
    message = LOG_URL_QUERY_PATTERN.sub(r"\1?[REDACTED]", message)
    for secret in sorted(
        (secret for secret in _secret_values(secrets) if secret),
        key=len,
        reverse=True,
    ):
        message = message.replace(secret, "[REDACTED]")
    for pattern in LOG_SECRET_PATTERNS:
        message = pattern.sub(r"\1[REDACTED]", message)
    return message


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, secrets: Secrets = ()):
        super().__init__(fmt)
        # Held by reference when it is a registry, so a secret added later is
        # redacted by the handler that is already installed.
        self._secrets = secrets

    def format(self, record: logging.LogRecord) -> str:
        return redact_log_text(super().format(record), self._secrets)


def configure_logging(debug: bool, secrets: Secrets = ()) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(LOG_FORMAT, secrets))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("gw2bot").setLevel(logging.DEBUG if debug else logging.INFO)
