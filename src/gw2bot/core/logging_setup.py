from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOGGER = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_FILE_NAME = "gw2bot.jsonl"
# Each midnight the day's file is renamed to gw2bot.<date>.jsonl, and the
# oldest is deleted once there are more than this many.
LOG_FILE_RETENTION_DAYS = 30
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


class JsonLinesFormatter(logging.Formatter):
    """One JSON object per record, for the log file.

    Each field is redacted before it is encoded rather than the finished line
    after: JSON escapes the quotes the secret patterns anchor on, so a
    payload like `{"token": "..."}` would no longer match once encoded, and
    redacting inside an escape sequence could break the line. Every field is,
    not just the message, because the console redacts its whole line - the
    logger name included - and a field added later is then covered too.
    """

    def __init__(self, secrets: Secrets = ()) -> None:
        super().__init__()
        self._secrets = secrets

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, timezone.utc)
            .astimezone()
            .isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            entry["exception"] = record.exc_text
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        return json.dumps(
            {
                field: redact_log_text(value, self._secrets)
                for field, value in entry.items()
            },
            ensure_ascii=False,
        )


def _dated_log_file_name(default_name: str) -> str:
    """Put the rotation date before the extension, so it stays `.jsonl`.

    The rotating handler's own spelling is `gw2bot.jsonl.2026-10-01`. It also
    matches old files against this namer when deciding which to delete, so
    the two cannot drift apart.
    """
    current, _, date = default_name.rpartition(".")
    stem, extension = os.path.splitext(current)
    return f"{stem}.{date}{extension}"


class _LogFileHandler(TimedRotatingFileHandler):
    def __init__(self, path: Path, secrets: Secrets) -> None:
        super().__init__(
            path,
            when="midnight",
            backupCount=LOG_FILE_RETENTION_DAYS,
            encoding="utf-8",
        )
        self.namer = _dated_log_file_name
        self.setFormatter(JsonLinesFormatter(secrets))

    def handleError(self, record: logging.LogRecord) -> None:
        # The default prints the record's raw message and arguments to stderr,
        # past every redacting formatter, and a full disk would do that for
        # every record. Name only what went wrong.
        error = sys.exc_info()[1]
        if not logging.raiseExceptions or not sys.stderr or error is None:
            return
        reason = type(error).__name__
        if isinstance(error, OSError) and error.strerror:
            reason = f"{reason}: {error.strerror}"
        sys.stderr.write(f"Could not write the log file. error={reason}\n")


def configure_logging(
    debug: bool,
    secrets: Secrets = (),
    log_directory: Path | None = None,
) -> None:
    """Log to the console, and to a daily JSON Lines file when given a directory.

    Both handlers hold the same secrets, so nothing reaches the file that the
    console would have redacted. A directory that cannot be written is warned
    about and skipped: the file is a diagnostic, and losing it is no reason to
    keep the bot from starting.
    """
    console = logging.StreamHandler()
    console.setFormatter(RedactingFormatter(LOG_FORMAT, secrets))
    handlers: list[logging.Handler] = [console]
    file_error: OSError | None = None
    if log_directory is not None:
        try:
            log_directory.mkdir(parents=True, exist_ok=True)
            handlers.append(_LogFileHandler(log_directory / LOG_FILE_NAME, secrets))
        except OSError as exc:
            file_error = exc
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    logging.getLogger("gw2bot").setLevel(logging.DEBUG if debug else logging.INFO)
    if log_directory is None:
        return
    if file_error is not None:
        LOGGER.warning(
            "Could not open the log file; logging to the console only. "
            "directory=%s error=%s",
            log_directory,
            file_error.strerror or type(file_error).__name__,
        )
    else:
        LOGGER.info("Writing log file %s", log_directory / LOG_FILE_NAME)
