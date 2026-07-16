from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from gw2bot.polls.formatting import (
    build_poll_embed,
    parse_poll_duration,
    parse_poll_options,
    parse_poll_title,
)
from gw2bot.polls.models import (
    Poll,
    emoji_for_index,
    index_for_emoji,
    option_percentage,
)

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _poll(**overrides: object) -> Poll:
    base: dict[str, Any] = dict(
        poll_id=7,
        guild_id=1,
        channel_id=2,
        creator_discord_id=3,
        title="Favourite mount?",
        options=("Griffon", "Skyscale"),
        allow_multiple=False,
        created_at=NOW,
        end_time=NOW + timedelta(hours=1),
        message_id=555,
    )
    base.update(overrides)
    return Poll(**base)  # type: ignore[arg-type]


def test_emoji_roundtrip_covers_ten_options() -> None:
    for index in range(10):
        assert index_for_emoji(emoji_for_index(index)) == index
    # The tenth option uses the "0" keycap, per the request.
    assert emoji_for_index(9) == "0️⃣"
    assert index_for_emoji("🎉") is None


def test_parse_poll_title_strips_and_validates() -> None:
    assert parse_poll_title("  Best mount?  ") == "Best mount?"
    with pytest.raises(ValueError):
        parse_poll_title("   ")
    with pytest.raises(ValueError):
        parse_poll_title("x" * 400)


def test_parse_poll_options_filters_blank_lines() -> None:
    options = parse_poll_options("Griffon\n\n  Skyscale \n\nRoller Beetle\n")
    assert options == ("Griffon", "Skyscale", "Roller Beetle")


def test_parse_poll_options_enforces_bounds() -> None:
    with pytest.raises(ValueError):
        parse_poll_options("Only one")
    with pytest.raises(ValueError):
        parse_poll_options("\n".join(str(index) for index in range(11)))
    with pytest.raises(ValueError):
        parse_poll_options("ok\n" + "x" * 200)


def test_parse_poll_duration_accepts_large_hours() -> None:
    assert parse_poll_duration("01:30") == 90
    assert parse_poll_duration("48:00") == 48 * 60


def test_option_percentage_uses_total_selections() -> None:
    counts = {0: 3, 1: 1}
    assert option_percentage(counts, 0) == 75.0
    assert option_percentage(counts, 1) == 25.0
    assert option_percentage({}, 0) == 0.0


def test_build_poll_embed_shows_counts_and_percentages() -> None:
    embed = build_poll_embed(_poll(), {0: 3, 1: 1}, now=NOW)

    assert embed.title is not None and embed.title.startswith("📊")
    assert embed.description is not None
    assert "Griffon" in embed.description
    assert "3 votes (75%)" in embed.description
    assert "1 vote (25%)" in embed.description
    assert "4 total votes" in embed.description
    assert embed.footer.text == "pollID: 7"


def test_build_poll_embed_ended_shows_winner() -> None:
    embed = build_poll_embed(_poll(), {0: 5, 1: 2}, ended=True, now=NOW)

    assert embed.title is not None and embed.title.startswith("🔒")
    assert embed.description is not None
    assert "This poll has ended." in embed.description
    assert "Winner:" in embed.description
    assert "Griffon" in embed.description
    assert "Voting is closed." in embed.description
