from __future__ import annotations

import logging

import aiohttp

from gw2bot.logging_setup import redact_log_text

LOGGER = logging.getLogger(__name__)


def format_poll_error(error: Exception, secrets: tuple[str, ...] = ()) -> str:
    if isinstance(error, aiohttp.ClientResponseError):
        status = f"HTTP {error.status}" if error.status else type(error).__name__
        detail = error.message.strip()
        message = f"{status}: {detail}" if detail else status
    else:
        message = str(error) or type(error).__name__

    return redact_log_text(message, secrets)


class PollStatusTracker:
    # Poll status is operational noise (timeouts, transient API errors), so it
    # stays in the console and is never posted to the logging channel.

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        self._secrets = secrets
        self._last_errors: dict[str, str] = {}

    @property
    def last_errors(self) -> dict[str, str]:
        return dict(self._last_errors)

    def record_success(self, source: str) -> None:
        LOGGER.debug("%s poll reported success", source)
        if source in self._last_errors:
            LOGGER.info("%s polling recovered.", source)
            del self._last_errors[source]

    def record_error(self, source: str, error: Exception) -> None:
        message = format_poll_error(error, self._secrets)
        LOGGER.warning("%s polling failed: %s", source, message)
        self._last_errors[source] = message
