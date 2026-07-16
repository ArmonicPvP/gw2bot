from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gw2bot.events.roles import EVENT_CREATE_ROLE_ID
from gw2bot.polls.commands import PollCommands
from gw2bot.polls.store import PollStore
from gw2bot.polls.views import (
    PollCompleteConfirmView,
    PollDeleteConfirmView,
    PollDetailsModal,
)


def make_interaction(
    *,
    role_ids: tuple[int, ...] = (),
    guild_id: int = 1,
    user_id: int = 42,
) -> Any:
    interaction = MagicMock()
    interaction.user = SimpleNamespace(
        id=user_id,
        roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
    )
    interaction.guild_id = guild_id
    interaction.message = None
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    return interaction


@pytest.fixture
def store(tmp_path: Path):
    store = PollStore(str(tmp_path / "gw2bot.db"))
    yield store
    store.close()


def make_bot(store: PollStore) -> Any:
    return cast(Any, SimpleNamespace(poll_store=store))


def create_posted_poll(store: PollStore, title: str = "Question") -> Any:
    poll = store.create_poll(
        guild_id=1,
        channel_id=2,
        creator_discord_id=3,
        title=title,
        options=("A", "B"),
        allow_multiple=False,
        duration_minutes=60,
    )
    store.set_poll_message(poll.poll_id, 2, 555)
    return store.get_poll(poll.poll_id)


def test_group_registers_poll_commands(store: PollStore) -> None:
    group = PollCommands(make_bot(store))

    commands = {command.name for command in group.commands}
    assert group.name == "poll"
    assert group.guild_only
    assert commands == {"create", "edit", "delete", "complete"}


async def test_create_rejects_users_without_role(store: PollStore) -> None:
    group = PollCommands(make_bot(store))
    interaction = make_interaction()

    await cast(Any, group.create.callback)(group, interaction)

    interaction.response.send_message.assert_awaited_once()
    interaction.response.send_modal.assert_not_awaited()


async def test_create_opens_modal_for_authorized_users(store: PollStore) -> None:
    group = PollCommands(make_bot(store))
    interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

    await cast(Any, group.create.callback)(group, interaction)

    interaction.response.send_modal.assert_awaited_once()
    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, PollDetailsModal)


async def test_delete_unknown_poll_reports_missing(store: PollStore) -> None:
    group = PollCommands(make_bot(store))
    interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

    await cast(Any, group.delete.callback)(group, interaction, 999)

    interaction.response.send_message.assert_awaited_once()
    content = interaction.response.send_message.await_args.args[0]
    assert "does not exist" in content


async def test_delete_existing_poll_opens_confirmation(store: PollStore) -> None:
    poll = create_posted_poll(store)
    group = PollCommands(make_bot(store))
    interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

    await cast(Any, group.delete.callback)(group, interaction, poll.poll_id)

    interaction.response.send_message.assert_awaited_once()
    kwargs = interaction.response.send_message.await_args.kwargs
    assert isinstance(kwargs["view"], PollDeleteConfirmView)
    assert kwargs["ephemeral"] is True


async def test_complete_existing_poll_opens_confirmation(store: PollStore) -> None:
    poll = create_posted_poll(store)
    group = PollCommands(make_bot(store))
    interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

    await cast(Any, group.complete.callback)(group, interaction, poll.poll_id)

    kwargs = interaction.response.send_message.await_args.kwargs
    assert isinstance(kwargs["view"], PollCompleteConfirmView)


async def test_edit_existing_poll_shows_preview(store: PollStore) -> None:
    poll = create_posted_poll(store)
    group = PollCommands(make_bot(store))
    interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

    await cast(Any, group.edit.callback)(group, interaction, poll.poll_id)

    interaction.response.send_message.assert_awaited_once()
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert len(kwargs["embeds"]) == 2


async def test_autocomplete_is_empty_without_role(store: PollStore) -> None:
    create_posted_poll(store)
    group = PollCommands(make_bot(store))
    interaction = make_interaction()

    choices = await group.active_poll_id_autocomplete(interaction, "")

    assert choices == []


async def test_autocomplete_lists_posted_polls(store: PollStore) -> None:
    poll = create_posted_poll(store, title="Best mount")
    group = PollCommands(make_bot(store))
    interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

    choices = await group.active_poll_id_autocomplete(interaction, "mount")

    assert [choice.value for choice in choices] == [poll.poll_id]
