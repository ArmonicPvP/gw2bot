from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from gw2bot.polls.models import emoji_for_index
from gw2bot.polls.scheduler import run_poll_maintenance
from gw2bot.polls.store import PollStore

from test_poll_reactions import (
    CHANNEL_ID,
    NOW,
    FakeBot,
    FakeChannel,
    FakeReaction,
    make_poll,
)


@pytest.fixture
def store(tmp_path: Path):
    store = PollStore(str(tmp_path / "gw2bot.db"))
    yield store
    store.close()


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def bot(store: PollStore, channel: FakeChannel) -> Any:
    return cast(Any, FakeBot(store, channel))


async def test_maintenance_finalizes_expired_poll(
    bot: Any,
    store: PollStore,
    channel: FakeChannel,
) -> None:
    poll = make_poll(store)
    channel.message.reactions = [FakeReaction(emoji_for_index(0), [10, 11])]

    await run_poll_maintenance(bot, now=NOW + timedelta(hours=2))

    assert store.get_poll(poll.poll_id) is None
    channel.message.edit.assert_awaited()
    channel.message.clear_reactions.assert_awaited_once()


async def test_maintenance_reconciles_active_poll(
    bot: Any,
    store: PollStore,
    channel: FakeChannel,
) -> None:
    poll = make_poll(store)
    # A stale stored vote plus live reactions that disagree with it.
    store.add_vote(poll.poll_id, 2, 99, now=NOW)
    channel.message.reactions = [FakeReaction(emoji_for_index(0), [10])]

    await run_poll_maintenance(bot, now=NOW)

    assert store.get_poll(poll.poll_id) is not None
    assert store.get_vote_counts(poll.poll_id) == {0: 1}
    channel.message.edit.assert_awaited_once()


async def test_maintenance_skips_unposted_poll(
    bot: Any,
    store: PollStore,
    channel: FakeChannel,
) -> None:
    unposted = store.create_poll(
        guild_id=1,
        channel_id=CHANNEL_ID,
        creator_discord_id=3,
        title="Draft",
        options=("A", "B"),
        allow_multiple=False,
        duration_minutes=30,
        now=NOW,
    )

    await run_poll_maintenance(bot, now=NOW + timedelta(hours=2))

    # An unposted poll is left for the creation flow; it is neither finalized
    # nor reconciled.
    assert store.get_poll(unposted.poll_id) is not None
    channel.message.edit.assert_not_awaited()
