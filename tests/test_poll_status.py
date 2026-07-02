import logging
from types import SimpleNamespace

import aiohttp
import pytest

from gw2bot.poll_status import PollStatusTracker, format_poll_error


class TestFormatPollError:
    def test_formats_client_response_error_with_status_and_detail(self) -> None:
        error = aiohttp.ClientResponseError(
            SimpleNamespace(
                real_url="https://example.test/log?access_token=secret"
            ),  # type: ignore[arg-type]
            (),
            status=502,
            message="Bad Gateway",
        )

        assert format_poll_error(error) == "HTTP 502: Bad Gateway"

    def test_falls_back_to_exception_type_name_for_empty_message(self) -> None:
        assert format_poll_error(TimeoutError()) == "TimeoutError"


class TestPollStatusTracker:
    def test_bad_gateway_does_not_leak_api_key(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        api_key = "secret-api-key"
        tracker = PollStatusTracker((api_key, "secret-discord-token"))
        error = aiohttp.ClientResponseError(
            SimpleNamespace(
                real_url=f"https://example.test/log?access_token={api_key}"
            ),  # type: ignore[arg-type]
            (),
            status=502,
            message="Bad Gateway",
        )

        with caplog.at_level(logging.WARNING, logger="gw2bot.poll_status"):
            tracker.record_error("Guild Log", error)

        assert tracker.last_errors == {"Guild Log": "HTTP 502: Bad Gateway"}
        assert api_key not in caplog.text

    def test_redacts_configured_credentials_from_poll_error(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        api_key = "secret-api-key"
        tracker = PollStatusTracker((api_key, "secret-discord-token"))

        with caplog.at_level(logging.WARNING, logger="gw2bot.poll_status"):
            tracker.record_error(
                "Guild Log",
                TimeoutError(f"Request failed with Bearer {api_key}"),
            )

        assert (
            "Guild Log polling failed: Request failed with Bearer [REDACTED]"
            in caplog.text
        )

    def test_report_failure_does_not_log_credentials(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "raffle-report-secret"
        tracker = PollStatusTracker((secret, "discord-secret"))

        with caplog.at_level(logging.WARNING, logger="gw2bot.poll_status"):
            tracker.record_error(
                "Raffle Contributions",
                aiohttp.ClientError(f"request failed with access_token={secret}"),
            )

        assert secret not in caplog.text
        assert (
            "Raffle Contributions polling failed: "
            "request failed with access_token=[REDACTED]"
            in caplog.text
        )
        assert tracker.last_errors == {
            "Raffle Contributions": (
                "request failed with access_token=[REDACTED]"
            )
        }

    def test_poll_error_is_logged_to_console(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        tracker = PollStatusTracker()
        error = TimeoutError("API unavailable")

        with caplog.at_level(logging.WARNING, logger="gw2bot.poll_status"):
            tracker.record_error("Guild Storage", error)

        assert "Guild Storage polling failed: API unavailable" in caplog.text
        assert tracker.last_errors == {"Guild Storage": "API unavailable"}

    def test_poll_recovery_is_logged_to_console(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        tracker = PollStatusTracker()
        tracker.record_error("Guild Storage", TimeoutError("API unavailable"))

        with caplog.at_level(logging.INFO, logger="gw2bot.poll_status"):
            tracker.record_success("Guild Storage")

        assert "Guild Storage polling recovered." in caplog.text
        assert tracker.last_errors == {}

    def test_success_without_prior_error_does_not_log_recovery(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        tracker = PollStatusTracker()

        with caplog.at_level(logging.INFO, logger="gw2bot.poll_status"):
            tracker.record_success("Guild Log")

        assert "recovered" not in caplog.text
        assert tracker.last_errors == {}
