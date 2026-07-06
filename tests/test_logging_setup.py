import logging
import sys
from unittest.mock import MagicMock, patch

from gw2bot.logging_setup import (
    RedactingFormatter,
    configure_logging,
    redact_log_text,
)


class TestConfigureLogging:
    @patch("gw2bot.logging_setup.logging.basicConfig")
    def test_configures_application_debug_logging_only(
        self,
        basic_config: MagicMock,
    ) -> None:
        app_logger = logging.getLogger("gw2bot")
        previous_level = app_logger.level
        try:
            configure_logging(True)
            assert app_logger.level == logging.DEBUG

            configure_logging(False)
            assert app_logger.level == logging.INFO
        finally:
            app_logger.setLevel(previous_level)

        assert basic_config.call_args.kwargs["level"] == logging.INFO
        assert basic_config.call_args.kwargs["force"]
        handlers = basic_config.call_args.kwargs["handlers"]
        assert len(handlers) == 1
        assert isinstance(handlers[0].formatter, RedactingFormatter)


class TestRedaction:
    def test_redacts_credentials_from_http_request_and_response_logs(self) -> None:
        message = (
            "GET https://example.test/v2/account?access_token=query-secret "
            "headers={'Authorization': 'Bearer header-secret'} "
            "response={'subtoken': 'response-secret'} configured-secret"
        )

        redacted = redact_log_text(message, ("configured-secret",))

        for secret in (
            "query-secret",
            "header-secret",
            "response-secret",
            "configured-secret",
        ):
            assert secret not in redacted
        assert redacted.count("[REDACTED]") == 4

    def test_strips_complete_url_query_strings_with_unknown_parameters(self) -> None:
        message = (
            "request failed: https://example.test/log?since=42&opaque=mystery-secret "
            "and HTTP://OTHER.TEST/path?custom=another-secret"
        )

        redacted = redact_log_text(message)

        assert redacted == (
            "request failed: https://example.test/log?[REDACTED] "
            "and HTTP://OTHER.TEST/path?[REDACTED]"
        )
        assert "mystery-secret" not in redacted
        assert "another-secret" not in redacted

    def test_redacting_formatter_sanitizes_exception_tracebacks(self) -> None:
        secret = "configured-secret"
        try:
            raise RuntimeError(
                "request failed with Authorization: Bearer configured-secret"
            )
        except RuntimeError:
            record = logging.LogRecord(
                "aiohttp.client",
                logging.ERROR,
                __file__,
                1,
                "HTTP request failed",
                (),
                sys.exc_info(),
            )

        formatted = RedactingFormatter("%(message)s", (secret,)).format(record)

        assert secret not in formatted
        assert "[REDACTED]" in formatted
