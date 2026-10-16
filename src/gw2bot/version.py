"""The version this copy of the bot was released as.

A release is a `vX.Y.Z` tag, and nothing in the source tree carries the
number: `.github/workflows/release.yml` builds the published image with the
version written into `VERSION` beside this module. Anything else - a checkout,
`docker compose up --build`, the CI image - has no stamp and reports
`UNRELEASED_VERSION`. See `docs/versioning.md`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

# A Semantic Versioning pre-release of nothing, so it sorts below every real
# release and cannot be mistaken for one.
UNRELEASED_VERSION: Final = "0.0.0-dev"
VERSION_FILE: Final = Path(__file__).with_name("VERSION")


def read_version(path: Path = VERSION_FILE) -> str:
    """The version stamped at `path`, or `UNRELEASED_VERSION` without one."""
    try:
        stamped = path.read_text(encoding="utf-8").strip()
    except OSError:
        # The number is for people reading the log; failing to find it must
        # never be the reason the bot does not start.
        return UNRELEASED_VERSION
    return stamped or UNRELEASED_VERSION


__version__: Final = read_version()
