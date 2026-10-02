import errno
import io
import json
import logging
import re
import sys
from collections.abc import Iterator
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gw2bot.core.logging_setup import (
    LOG_FILE_NAME,
    LOG_FILE_RETENTION_DAYS,
    JsonLinesFormatter,
    RedactingFormatter,
    SecretRegistry,
    configure_logging,
    redact_log_text,
)


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        "gw2bot.test", logging.INFO, __file__, 1, message, (), None
    )


@pytest.fixture
def basic_config() -> Iterator[MagicMock]:
    app_logger = logging.getLogger("gw2bot")
    previous_level = app_logger.level
    with patch("gw2bot.core.logging_setup.logging.basicConfig") as basic_config:
        yield basic_config
    app_logger.setLevel(previous_level)
    for handler in basic_config.call_args.kwargs.get("handlers", ()):
        handler.close()


def _file_handler(basic_config: MagicMock) -> TimedRotatingFileHandler:
    (handler,) = (
        handler
        for handler in basic_config.call_args.kwargs["handlers"]
        if isinstance(handler, TimedRotatingFileHandler)
    )
    return handler


class _FullDisk(io.StringIO):
    def write(self, s: str) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")


def _lines(path: Path) -> list[dict[str, str]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


class TestConfigureLogging:
    @patch("gw2bot.core.logging_setup.logging.basicConfig")
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


class TestLogFile:
    def test_writes_one_json_object_per_record(self) -> None:
        line = JsonLinesFormatter().format(_record("first line\nsecond line"))

        # A multi-line message stays on one line, so every line of the file
        # is one record a JSON parser can read on its own.
        assert "\n" not in line
        entry = json.loads(line)
        assert entry["level"] == "INFO"
        assert entry["logger"] == "gw2bot.test"
        assert entry["message"] == "first line\nsecond line"
        assert datetime.fromisoformat(entry["time"]).tzinfo is not None
        assert "exception" not in entry

    def test_redacts_each_field_before_encoding(self) -> None:
        # JSON escapes the quotes the secret patterns anchor on, so redacting
        # the encoded line would let the payload's token through.
        message = (
            'payload={"access_token": "json-secret"} configured-secret '
            "https://example.test/v2/account?access_token=query-secret"
        )
        try:
            raise RuntimeError("Authorization: Bearer traceback-secret")
        except RuntimeError:
            record = logging.LogRecord(
                "aiohttp.client",
                logging.ERROR,
                __file__,
                1,
                message,
                (),
                sys.exc_info(),
            )

        line = JsonLinesFormatter(("configured-secret",)).format(record)

        for secret in (
            "json-secret",
            "configured-secret",
            "query-secret",
            "traceback-secret",
        ):
            assert secret not in line
        entry = json.loads(line)
        assert "[REDACTED]" in entry["message"]
        assert entry["exception"].startswith("Traceback")
        assert "[REDACTED]" in entry["exception"]

    def test_redacts_a_secret_registered_after_the_formatter(self) -> None:
        # /settings can set a credential long after logging was configured.
        registry = SecretRegistry()
        formatter = JsonLinesFormatter(registry)
        registry.add("later-secret")

        line = formatter.format(_record("using later-secret"))

        assert "later-secret" not in line

    def test_configure_logging_writes_redacted_records_to_the_file(
        self,
        basic_config: MagicMock,
        tmp_path: Path,
    ) -> None:
        directory = tmp_path / "log"

        configure_logging(
            False,
            SecretRegistry(("configured-secret",)),
            log_directory=directory,
        )

        handlers = basic_config.call_args.kwargs["handlers"]
        assert len(handlers) == 2
        assert isinstance(handlers[0].formatter, RedactingFormatter)
        handler = _file_handler(basic_config)
        assert isinstance(handler.formatter, JsonLinesFormatter)
        assert handler.when == "MIDNIGHT"
        assert handler.backupCount == LOG_FILE_RETENTION_DAYS

        handler.handle(_record("token=configured-secret"))

        (entry,) = _lines(directory / LOG_FILE_NAME)
        assert entry["message"] == "token=[REDACTED]"

    def test_rotated_files_keep_the_extension_and_expire(
        self,
        basic_config: MagicMock,
        tmp_path: Path,
    ) -> None:
        old_days = [f"gw2bot.2026-08-{day:02d}.jsonl" for day in range(1, 32)]
        for name in old_days:
            (tmp_path / name).write_text("{}\n", encoding="utf-8")
        (tmp_path / "notes.txt").write_text("kept\n", encoding="utf-8")
        configure_logging(False, log_directory=tmp_path)
        handler = _file_handler(basic_config)
        handler.handle(_record("before midnight"))

        handler.doRollover()

        names = {path.name for path in tmp_path.iterdir()}
        (rotated,) = (
            name
            for name in names - set(old_days)
            if re.fullmatch(r"gw2bot\.\d{4}-\d{2}-\d{2}\.jsonl", name)
        )
        assert _lines(tmp_path / rotated)[0]["message"] == "before midnight"
        # The namer is what the handler matches old files against, so a
        # spelling it did not recognise would keep every day forever.
        dated = {name for name in names if name.startswith("gw2bot.20")}
        assert len(dated) == LOG_FILE_RETENTION_DAYS
        assert "gw2bot.2026-08-01.jsonl" not in names
        assert "gw2bot.2026-08-02.jsonl" not in names
        assert {LOG_FILE_NAME, "notes.txt"} <= names

    def test_an_unwritable_directory_logs_to_the_console_only(
        self,
        basic_config: MagicMock,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # A file where the folder should be fails the same way a read-only
        # mount does, and does so even for root.
        blocked = tmp_path / "log"
        blocked.write_text("", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="gw2bot"):
            configure_logging(False, log_directory=blocked)

        handlers = basic_config.call_args.kwargs["handlers"]
        assert len(handlers) == 1
        assert isinstance(handlers[0].formatter, RedactingFormatter)
        assert "logging to the console only" in caplog.text

    def test_a_failed_write_does_not_echo_the_record(
        self,
        basic_config: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The default handleError prints the raw message and its arguments to
        # stderr, where no formatter redacts them.
        configure_logging(False, log_directory=tmp_path)
        handler = _file_handler(basic_config)
        record = logging.LogRecord(
            "gw2bot.test",
            logging.INFO,
            __file__,
            1,
            "token=%s",
            ("raw-secret",),
            None,
        )

        with patch.object(handler, "stream", _FullDisk()):
            handler.handle(record)

        error = capsys.readouterr().err
        assert "raw-secret" not in error
        assert "No space left on device" in error
