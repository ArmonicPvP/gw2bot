from __future__ import annotations

import logging
import re

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


def redact_log_text(message: str, secrets: tuple[str, ...] = ()) -> str:
    message = LOG_URL_QUERY_PATTERN.sub(r"\1?[REDACTED]", message)
    for secret in sorted(
        (secret for secret in secrets if secret),
        key=len,
        reverse=True,
    ):
        message = message.replace(secret, "[REDACTED]")
    for pattern in LOG_SECRET_PATTERNS:
        message = pattern.sub(r"\1[REDACTED]", message)
    return message


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, secrets: tuple[str, ...] = ()):
        super().__init__(fmt)
        self._secrets = secrets

    def format(self, record: logging.LogRecord) -> str:
        return redact_log_text(super().format(record), self._secrets)


def configure_logging(debug: bool, secrets: tuple[str, ...] = ()) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(LOG_FORMAT, secrets))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("gw2bot").setLevel(logging.DEBUG if debug else logging.INFO)
