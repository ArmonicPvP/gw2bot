"""Fixtures every test module shares."""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _restore_process_timezone() -> Iterator[None]:
    # bootstrap_from_env re-reads TZ once it has loaded .env, and the tests
    # that run main() patch the environment around it. The environment is put
    # back after each test, but the zone the C library read is process-wide,
    # so read it again rather than let one test's TZ reach the next.
    yield
    if sys.platform != "win32":
        time.tzset()
