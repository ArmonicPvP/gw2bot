from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gw2bot.polls.models import Poll, emoji_for_index
from gw2bot.polls.reactions import (
    enforce_single_choice,
    finalize_poll,
    handle_reaction_add,
    handle_reaction_remove,
    reconcile_poll,
)
from gw2bot.polls.store import PollStore

from factories import not_found_error

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
BOT_ID = 999_999
CHANNEL_ID = 2
MESSAGE_ID = 555


async def _aiter(items: Iterable[Any]) -> AsyncIterator[Any]:
    for item in items:
        yield item


class FakeReaction:
    def __init__(self, emoji: str, user_ids: Iterable[int]):
        self.emoji = emoji
        self._user_ids = list(user_ids)

    def users(self) -> AsyncIterator[Any]:
        return _aiter([SimpleNamespace(id=user_id) for user_id in self._user_ids])


class FakeMessage:
    def __init__(
        self,
        message_id: int = MESSAGE_ID,
        reactions: list[FakeReaction] | None = None,
    ):
        self.id = message_id
        self.reactions = reactions or []
        self.edit = AsyncMock()
        self.clear_reactions = AsyncMock()
        self.add_reaction = AsyncMock()
        self.remove_reaction = AsyncMock()
        self.delete = AsyncMock()


class FakeChannel:
    def __init__(self, channel_id: int = CHANNEL_ID):
        self.id = channel_id
        self.message = FakeMessage()
        self.partial = SimpleNamespace(
            edit=AsyncMock(),
            remove_reaction=AsyncMock(),
            delete=AsyncMock(),
            clear_reactions=AsyncMock(),
            add_reaction=AsyncMock(),
        )
        self.sent: list[dict[str, Any]] = []
        self.fetch_error: Exception | None = None

    async def fetch_message(self, message_id: int) -> Any:
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.message

    def get_partial_message(self, message_id: int) -> Any:
        return self.partial

    async def send(self, *, embed: Any = None) -> Any:
        message = FakeMessage(message_id=999)
        self.sent.append({"embed": embed, "message": message})
        return message


class FakeBot:
    def __init__(self, store: PollStore, channel: FakeChannel, bot_id: int = BOT_ID):
        self.poll_store = store
        self.user = SimpleNamespace(id=bot_id)
        self.poll_renderer = SimpleNamespace(schedule=MagicMock())
        self._channel = channel

    def get_channel(self, channel_id: int) -> Any:
        return self._channel if channel_id == self._channel.id else None

    async def fetch_channel(self, channel_id: int) -> Any:
        return self._channel


def make_payload(message_id: int, user_id: int, emoji: str) -> Any:
    return SimpleNamespace(
        message_id=message_id,
        user_id=user_id,
        emoji=emoji,
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


def make_poll(
    store: PollStore,
    *,
    allow_multiple: bool = False,
    options: tuple[str, ...] = ("Griffon", "Skyscale", "Roller Beetle"),
) -> Poll:
    poll = store.create_poll(
        guild_id=1,
        channel_id=CHANNEL_ID,
        creator_discord_id=3,
        title="Favourite mount?",
        options=options,
        allow_multiple=allow_multiple,
        duration_minutes=60,
        now=NOW,
    )
    store.set_poll_message(poll.poll_id, CHANNEL_ID, MESSAGE_ID)
    refreshed = store.get_poll(poll.poll_id)
    assert refreshed is not None
    return refreshed


async def test_reaction_add_records_vote(bot: Any, store: PollStore) -> None:
    poll = make_poll(store)

    await handle_reaction_add(bot, make_payload(MESSAGE_ID, 10, emoji_for_index(0)))

    assert store.get_vote_counts(poll.poll_id) == {0: 1}
    bot.poll_renderer.schedule.assert_called_once()


async def test_reaction_add_ignores_bot_own_reaction(
    bot: Any,
    store: PollStore,
) -> None:
    poll = make_poll(store)

    await handle_reaction_add(
        bot,
        make_payload(MESSAGE_ID, BOT_ID, emoji_for_index(0)),
    )

    assert store.get_vote_counts(poll.poll_id) == {}


async def test_reaction_add_ignores_unknown_message(
    bot: Any,
    store: PollStore,
) -> None:
    poll = make_poll(store)

    await handle_reaction_add(bot, make_payload(404, 10, emoji_for_index(0)))

    assert store.get_vote_counts(poll.poll_id) == {}


async def test_reaction_add_ignores_out_of_range_option(
    bot: Any,
    store: PollStore,
) -> None:
    poll = make_poll(store, options=("Griffon", "Skyscale"))

    # The poll only has two options, so the third keycap is not a valid vote.
    await handle_reaction_add(bot, make_payload(MESSAGE_ID, 10, emoji_for_index(2)))
    # A non-poll emoji is ignored too.
    await handle_reaction_add(bot, make_payload(MESSAGE_ID, 10, "🎉"))

    assert store.get_vote_counts(poll.poll_id) == {}


async def test_single_choice_switches_to_newest(
    bot: Any,
    store: PollStore,
    channel: FakeChannel,
) -> None:
    poll = make_poll(store, allow_multiple=False)

    await handle_reaction_add(bot, make_payload(MESSAGE_ID, 10, emoji_for_index(0)))
    await handle_reaction_add(bot, make_payload(MESSAGE_ID, 10, emoji_for_index(1)))

    assert store.get_user_options(poll.poll_id, 10) == [1]
    channel.partial.remove_reaction.assert_awaited_once()
    removed_emoji = channel.partial.remove_reaction.await_args.args[0]
    assert removed_emoji == emoji_for_index(0)


async def test_multiple_choice_keeps_all_votes(
    bot: Any,
    store: PollStore,
    channel: FakeChannel,
) -> None:
    poll = make_poll(store, allow_multiple=True)

    await handle_reaction_add(bot, make_payload(MESSAGE_ID, 10, emoji_for_index(0)))
    await handle_reaction_add(bot, make_payload(MESSAGE_ID, 10, emoji_for_index(1)))

    assert store.get_user_options(poll.poll_id, 10) == [0, 1]
    channel.partial.remove_reaction.assert_not_called()


async def test_reaction_remove_deletes_vote(bot: Any, store: PollStore) -> None:
    poll = make_poll(store)
    store.add_vote(poll.poll_id, 0, 10, now=NOW)

    await handle_reaction_remove(
        bot,
        make_payload(MESSAGE_ID, 10, emoji_for_index(0)),
    )

    assert store.get_vote_counts(poll.poll_id) == {}


async def test_reconcile_rewrites_votes_from_live_reactions(
    bot: Any,
    store: PollStore,
    channel: FakeChannel,
) -> None:
    poll = make_poll(store)
    # A stale stored vote that no longer matches the live reactions.
    store.add_vote(poll.poll_id, 2, 99, now=NOW)
    channel.message.reactions = [
        FakeReaction(emoji_for_index(0), [10, BOT_ID]),
        FakeReaction(emoji_for_index(1), [11]),
    ]

    result = await reconcile_poll(bot, poll)

    assert result is not None
    # Bot's own reaction is excluded and the stale vote is dropped.
    assert store.get_vote_counts(poll.poll_id) == {0: 1, 1: 1}
    channel.message.edit.assert_awaited_once()


async def test_reconcile_deletes_poll_when_message_gone(
    bot: Any,
    store: PollStore,
    channel: FakeChannel,
) -> None:
    poll = make_poll(store)
    channel.fetch_error = not_found_error()

    result = await reconcile_poll(bot, poll)

    assert result is None
    assert store.get_poll(poll.poll_id) is None


async def test_finalize_locks_clears_and_deletes(
    bot: Any,
    store: PollStore,
    channel: FakeChannel,
) -> None:
    poll = make_poll(store)
    channel.message.reactions = [
        FakeReaction(emoji_for_index(0), [10, 11]),
        FakeReaction(emoji_for_index(1), [12]),
    ]

    finished = await finalize_poll(bot, poll, reason="manual")

    assert finished is True
    channel.message.edit.assert_awaited_once()
    assert channel.message.edit.await_args is not None
    ended_embed = channel.message.edit.await_args.kwargs["embed"]
    assert ended_embed.title.startswith("🔒")
    channel.message.clear_reactions.assert_awaited_once()
    assert store.get_poll(poll.poll_id) is None


async def test_finalize_deletes_even_when_message_gone(
    bot: Any,
    store: PollStore,
    channel: FakeChannel,
) -> None:
    poll = make_poll(store)
    channel.fetch_error = not_found_error()

    finished = await finalize_poll(bot, poll, reason="expired")

    assert finished is True
    assert store.get_poll(poll.poll_id) is None


async def test_enforce_single_choice_keeps_newest(
    bot: Any,
    store: PollStore,
    channel: FakeChannel,
) -> None:
    poll = make_poll(store, allow_multiple=False)
    store.add_vote(poll.poll_id, 0, 10, now=NOW)
    store.add_vote(poll.poll_id, 1, 10, now=NOW + timedelta(minutes=1))

    await enforce_single_choice(bot, poll)

    assert store.get_user_options(poll.poll_id, 10) == [1]
    channel.partial.remove_reaction.assert_awaited_once()
    assert channel.partial.remove_reaction.await_args.args[0] == emoji_for_index(0)


async def test_handlers_do_not_log_poll_text(
    bot: Any,
    store: PollStore,
    channel: FakeChannel,
    caplog: pytest.LogCaptureFixture,
) -> None:
    poll = make_poll(store, options=("SecretOptionAlpha", "SecretOptionBeta"))
    channel.message.reactions = [FakeReaction(emoji_for_index(0), [10])]

    with caplog.at_level("DEBUG", logger="gw2bot"):
        await handle_reaction_add(
            bot,
            make_payload(MESSAGE_ID, 10, emoji_for_index(0)),
        )
        await reconcile_poll(bot, poll)

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "SecretOptionAlpha" not in log_text
    assert "SecretOptionBeta" not in log_text
    assert "Favourite mount?" not in log_text
