from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from factories import default_config, forbidden_error, not_found_error
from gw2bot.polls.commands import (
    POST_FAILED_NOTICE,
    PollCommands,
    PollCreateModal,
    PollDraft,
    PollRetryView,
)
from gw2bot.polls.formatting import closed_poll_embed, open_poll_embed
from gw2bot.polls.models import (
    MAX_POLL_OPTIONS,
    POLL_OPTION_MAX_LENGTH,
    Poll,
    PollOptionsError,
    PollResults,
    VoteOutcome,
    parse_poll_options,
)
from gw2bot.polls.store import PollStore
from gw2bot.polls.views import (
    NO_MENTIONS,
    POLL_ENDED_NOTICE,
    PollVoteSelect,
    VoteConfirmView,
    handle_vote_pick,
    vote_custom_id,
)

OFFICER_ROLE_ID = 103
OFFICER_ID = 42
VOTER_ID = 777
CHANNEL_ID = 9001
GUILD_ID = 5678
CREATED_AT = datetime(2026, 11, 2, 18, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[PollStore]:
    poll_store = PollStore(str(tmp_path / "gw2bot.db"))
    yield poll_store
    poll_store.close()


def make_poll(
    store: PollStore,
    *,
    name: str = "Raid night",
    options: tuple[str, ...] = ("Monday", "Wednesday", "Friday"),
    description: str = "",
    message_id: int | None = 555,
) -> Poll:
    poll = store.create_poll(
        name=name,
        description=description,
        options=options,
        channel_id=CHANNEL_ID,
        created_by=OFFICER_ID,
        now=CREATED_AT,
    )
    if message_id is not None:
        store.set_message(poll.poll_id, message_id)
    refreshed = store.get_poll(poll.poll_id)
    assert refreshed is not None
    return refreshed


class FakePost:
    """The poll's post as the bot addresses it: by id, without fetching."""

    def __init__(self) -> None:
        self.edit = AsyncMock()
        self.reply = AsyncMock()


def make_bot(store: PollStore, post: FakePost | None = None) -> Any:
    post = post or FakePost()
    channel = SimpleNamespace(get_partial_message=MagicMock(return_value=post))
    return SimpleNamespace(
        poll_store=store,
        _config=default_config(raffle_officer_role_id=OFFICER_ROLE_ID),
        get_partial_messageable=MagicMock(return_value=channel),
        post=post,
    )


class FakeResponse:
    def __init__(self) -> None:
        self._done = False
        self.send_message = AsyncMock(side_effect=self._finish)
        self.defer = AsyncMock(side_effect=self._finish)
        self.send_modal = AsyncMock(side_effect=self._finish)
        self.edit_message = AsyncMock(side_effect=self._finish)

    async def _finish(self, *args: Any, **kwargs: Any) -> None:
        self._done = True

    def is_done(self) -> bool:
        return self._done


def make_interaction(
    *,
    user_id: int = OFFICER_ID,
    role_ids: tuple[int, ...] = (OFFICER_ROLE_ID,),
    channel: Any = None,
) -> Any:
    return SimpleNamespace(
        user=SimpleNamespace(
            id=user_id,
            roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
        ),
        guild=SimpleNamespace(id=GUILD_ID),
        channel=channel,
        channel_id=None if channel is None else CHANNEL_ID,
        response=FakeResponse(),
        followup=SimpleNamespace(send=AsyncMock(return_value=MagicMock())),
    )


def make_channel(message_id: int = 555) -> Any:
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock(
        return_value=SimpleNamespace(
            id=message_id,
            jump_url=f"https://discord.com/channels/{GUILD_ID}/{CHANNEL_ID}/{message_id}",
            delete=AsyncMock(),
        )
    )
    return channel


def notice(interaction: Any) -> str:
    """The one private reply a flow sent, wherever it went."""
    if interaction.followup.send.await_count:
        interaction.followup.send.assert_awaited_once()
        call = interaction.followup.send.await_args
    else:
        interaction.response.send_message.assert_awaited_once()
        call = interaction.response.send_message.await_args
    assert call.kwargs["ephemeral"] is True
    return call.args[0]


def fill_modal(modal: PollCreateModal, name: str, options: str, description: str = "") -> None:
    modal.name_input._value = name
    modal.options_input._value = options
    modal.description_input._value = description


class TestParseOptions:
    def test_splits_on_commas_and_trims(self) -> None:
        assert parse_poll_options(" Monday ,Wednesday,  Friday ") == (
            "Monday",
            "Wednesday",
            "Friday",
        )

    def test_drops_blank_entries_and_collapses_whitespace(self) -> None:
        assert parse_poll_options("Raid\n night, , Fractals,") == (
            "Raid night",
            "Fractals",
        )

    def test_accepts_exactly_nine(self) -> None:
        text = ", ".join(f"Option {number}" for number in range(1, 10))

        assert len(parse_poll_options(text)) == MAX_POLL_OPTIONS

    @pytest.mark.parametrize(
        ("text", "reason"),
        [
            ("Only one", "at least 2"),
            (", ,", "at least 2"),
            (", ".join(str(number) for number in range(10)), "at most 9"),
            (f"Short, {'x' * (POLL_OPTION_MAX_LENGTH + 1)}", "option 2 is longer"),
            ("Yes, no, YES", "more than once"),
        ],
    )
    def test_refuses_what_cannot_make_a_poll(self, text: str, reason: str) -> None:
        with pytest.raises(PollOptionsError, match=reason):
            parse_poll_options(text)


class TestResults:
    def _results(self, counts: tuple[int, ...]) -> PollResults:
        poll = Poll(
            poll_id=1,
            name="Raid night",
            description="",
            options=("Monday", "Wednesday", "Friday")[: len(counts)],
            channel_id=CHANNEL_ID,
            message_id=555,
            created_by=OFFICER_ID,
            created_at=CREATED_AT,
            closed_at=CREATED_AT,
        )
        return PollResults(poll=poll, counts=counts)

    def test_a_single_leader_wins(self) -> None:
        assert self._results((1, 4, 2)).leaders == (1,)

    def test_a_shared_top_count_is_a_tie(self) -> None:
        assert self._results((3, 1, 3)).leaders == (0, 2)

    def test_nobody_leads_when_nobody_voted(self) -> None:
        assert self._results((0, 0, 0)).leaders == ()


class TestStore:
    def test_a_created_poll_reads_back_with_its_options_in_order(
        self,
        store: PollStore,
    ) -> None:
        poll = make_poll(store, description="Pick a night")

        assert poll.name == "Raid night"
        assert poll.description == "Pick a night"
        assert poll.options == ("Monday", "Wednesday", "Friday")
        assert poll.channel_id == CHANNEL_ID
        assert poll.message_id == 555
        assert poll.created_at == CREATED_AT
        assert poll.is_open

    def test_open_polls_lists_newest_first_and_leaves_closed_ones_out(
        self,
        store: PollStore,
    ) -> None:
        first = make_poll(store, name="First")
        second = make_poll(store, name="Second")
        third = make_poll(store, name="Third")
        store.close_poll(second.poll_id, OFFICER_ID)

        assert [poll.poll_id for poll in store.open_polls()] == [
            third.poll_id,
            first.poll_id,
        ]

    def test_a_second_pick_replaces_the_first(self, store: PollStore) -> None:
        poll = make_poll(store)

        assert store.record_vote(poll.poll_id, VOTER_ID, 0) is VoteOutcome.RECORDED
        assert store.record_vote(poll.poll_id, VOTER_ID, 0) is VoteOutcome.UNCHANGED
        assert store.record_vote(poll.poll_id, VOTER_ID, 2) is VoteOutcome.CHANGED

        assert store.current_vote(poll.poll_id, VOTER_ID) == 2
        assert store.voter_count(poll.poll_id) == 1

    def test_votes_are_counted_per_poll(self, store: PollStore) -> None:
        poll = make_poll(store)
        other = make_poll(store, name="Other")
        store.record_vote(poll.poll_id, 1, 0)
        store.record_vote(poll.poll_id, 2, 1)
        store.record_vote(other.poll_id, 1, 1)

        assert store.voter_count(poll.poll_id) == 2
        assert store.voter_count(other.poll_id) == 1

    def test_an_option_the_poll_does_not_have_is_refused(
        self,
        store: PollStore,
    ) -> None:
        poll = make_poll(store)

        with pytest.raises(ValueError):
            store.record_vote(poll.poll_id, VOTER_ID, 3)

    def test_closing_freezes_the_tallies_and_forgets_the_voters(
        self,
        store: PollStore,
    ) -> None:
        poll = make_poll(store)
        for voter, position in ((1, 2), (2, 2), (3, 0), (4, 2)):
            store.record_vote(poll.poll_id, voter, position)

        results = store.close_poll(poll.poll_id, OFFICER_ID, now=CREATED_AT)

        assert results is not None
        assert results.counts == (1, 0, 3)
        assert results.total == 4
        assert results.poll.closed_at == CREATED_AT
        assert results.poll.closed_by == OFFICER_ID
        assert not results.poll.is_open
        # Who voted for what does not outlive the poll.
        assert store.voter_count(poll.poll_id) == 0
        assert store.current_vote(poll.poll_id, 1) is None

    def test_a_closed_poll_takes_no_votes_and_cannot_close_again(
        self,
        store: PollStore,
    ) -> None:
        poll = make_poll(store)
        store.close_poll(poll.poll_id, OFFICER_ID)

        assert store.record_vote(poll.poll_id, VOTER_ID, 0) is VoteOutcome.CLOSED
        assert store.close_poll(poll.poll_id, OFFICER_ID) is None

    def test_a_missing_poll_takes_no_votes(self, store: PollStore) -> None:
        assert store.record_vote(404, VOTER_ID, 0) is VoteOutcome.CLOSED
        assert store.close_poll(404, OFFICER_ID) is None

    def test_results_read_back_the_frozen_tallies(self, store: PollStore) -> None:
        poll = make_poll(store)
        store.record_vote(poll.poll_id, 1, 1)
        store.record_vote(poll.poll_id, 2, 1)

        assert store.results(poll.poll_id) is None
        closed = store.close_poll(poll.poll_id, OFFICER_ID)

        results = store.results(poll.poll_id)
        assert results is not None
        assert closed is not None
        assert results.counts == closed.counts == (0, 2, 0)
        assert results.poll == closed.poll
        assert store.results(404) is None

    def test_deleting_a_poll_removes_it(self, store: PollStore) -> None:
        poll = make_poll(store, message_id=None)

        store.delete_poll(poll.poll_id)

        assert store.get_poll(poll.poll_id) is None

    def test_the_vote_log_line_never_names_the_choice(
        self,
        store: PollStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        poll = make_poll(store, options=("Alpha", "Bravo"))
        caplog.set_level(logging.DEBUG, logger="gw2bot.polls")

        store.record_vote(poll.poll_id, VOTER_ID, 1)

        text = caplog.text
        assert "Bravo" not in text
        assert "position" not in text


class TestFormatting:
    def test_the_open_post_lists_options_and_only_the_total(
        self,
        store: PollStore,
    ) -> None:
        poll = make_poll(store, description="Pick a night")

        embed = open_poll_embed(poll, 7)

        assert embed.title == "Raid night"
        description = embed.description or ""
        assert description.startswith("Pick a night\n\n")
        assert "1️⃣ Monday" in description
        assert "3️⃣ Friday" in description
        assert "vote" not in description
        assert [(field.name, field.value) for field in embed.fields] == [
            ("Votes cast", "7")
        ]
        assert f"#{poll.poll_id}" in (embed.footer.text or "")

    def test_the_closed_post_shows_every_tally_and_the_winner(
        self,
        store: PollStore,
    ) -> None:
        poll = make_poll(store)
        for voter, position in ((1, 1), (2, 1), (3, 0)):
            store.record_vote(poll.poll_id, voter, position)
        results = store.close_poll(poll.poll_id, OFFICER_ID)
        assert results is not None

        embed = closed_poll_embed(results)

        description = embed.description or ""
        assert embed.title == "Raid night (closed)"
        assert "**1 vote** (33%)" in description
        assert "**2 votes** (67%)" in description
        assert "**0 votes** (0%)" in description
        assert "Winner:** Wednesday" in description
        assert [(field.name, field.value) for field in embed.fields] == [
            ("Votes cast", "3")
        ]

    def test_a_tie_names_every_leader(self, store: PollStore) -> None:
        poll = make_poll(store)
        store.record_vote(poll.poll_id, 1, 0)
        store.record_vote(poll.poll_id, 2, 2)
        results = store.close_poll(poll.poll_id, OFFICER_ID)
        assert results is not None

        assert "Tie:** Monday, Friday" in (closed_poll_embed(results).description or "")

    def test_a_poll_nobody_voted_in_says_so(self, store: PollStore) -> None:
        poll = make_poll(store)
        results = store.close_poll(poll.poll_id, OFFICER_ID)
        assert results is not None

        assert "No votes were cast." in (closed_poll_embed(results).description or "")


class TestVoteSelect:
    async def test_the_custom_id_round_trips_through_the_template(self) -> None:
        select = PollVoteSelect(12, ("Monday", "Friday"))
        custom_id = select.item.custom_id
        assert custom_id == vote_custom_id(12)
        match = PollVoteSelect.__discord_ui_compiled_template__.fullmatch(custom_id)
        assert match is not None

        rebuilt = await PollVoteSelect.from_custom_id(
            cast(Any, None),
            cast(Any, None),
            match,
        )

        assert rebuilt.poll_id == 12

    async def test_each_option_carries_its_position_and_keycap(self) -> None:
        select = PollVoteSelect(12, ("Monday", "Friday"))

        assert [
            (option.label, option.value, str(option.emoji))
            for option in select.item.options
        ] == [("Monday", "0", "1️⃣"), ("Friday", "1", "2️⃣")]
        assert select.item.max_values == 1


class TestVotePick:
    async def test_a_pick_asks_for_confirmation_and_records_nothing(
        self,
        store: PollStore,
    ) -> None:
        poll = make_poll(store)
        bot = make_bot(store)
        interaction = make_interaction(user_id=VOTER_ID, role_ids=())

        await handle_vote_pick(bot, interaction, poll.poll_id, ["1"])

        # The dropdown is re-sent so the pick clears on the voter's screen.
        interaction.response.edit_message.assert_awaited_once()
        assert interaction.response.edit_message.await_args.kwargs["view"] is not None
        call = interaction.followup.send.await_args
        assert call.kwargs["ephemeral"] is True
        assert isinstance(call.kwargs["view"], VoteConfirmView)
        assert call.kwargs["allowed_mentions"] is NO_MENTIONS
        assert "2️⃣ Wednesday" in call.args[0]
        assert "replaces" not in call.args[0]
        assert store.current_vote(poll.poll_id, VOTER_ID) is None

    async def test_a_new_pick_says_it_replaces_the_current_vote(
        self,
        store: PollStore,
    ) -> None:
        poll = make_poll(store)
        store.record_vote(poll.poll_id, VOTER_ID, 0)
        interaction = make_interaction(user_id=VOTER_ID, role_ids=())

        await handle_vote_pick(make_bot(store), interaction, poll.poll_id, ["2"])

        content = interaction.followup.send.await_args.args[0]
        assert "1️⃣ Monday" in content
        assert "replaces it" in content

    async def test_picking_the_current_vote_needs_no_confirmation(
        self,
        store: PollStore,
    ) -> None:
        poll = make_poll(store)
        store.record_vote(poll.poll_id, VOTER_ID, 1)
        interaction = make_interaction(user_id=VOTER_ID, role_ids=())

        await handle_vote_pick(make_bot(store), interaction, poll.poll_id, ["1"])

        call = interaction.followup.send.await_args
        assert "view" not in call.kwargs
        assert "already voted" in call.args[0]

    async def test_a_closed_poll_refuses_the_pick(self, store: PollStore) -> None:
        poll = make_poll(store)
        store.close_poll(poll.poll_id, OFFICER_ID)
        interaction = make_interaction(user_id=VOTER_ID, role_ids=())

        await handle_vote_pick(make_bot(store), interaction, poll.poll_id, ["0"])

        assert notice(interaction) == POLL_ENDED_NOTICE
        interaction.response.edit_message.assert_not_awaited()

    async def test_a_value_outside_the_options_is_refused(
        self,
        store: PollStore,
    ) -> None:
        poll = make_poll(store)
        interaction = make_interaction(user_id=VOTER_ID, role_ids=())

        await handle_vote_pick(make_bot(store), interaction, poll.poll_id, ["9"])

        assert "not on this poll" in notice(interaction)


class TestVoteConfirm:
    async def test_confirming_records_the_vote_and_updates_the_count(
        self,
        store: PollStore,
    ) -> None:
        poll = make_poll(store)
        bot = make_bot(store)
        view = VoteConfirmView(bot, poll, 2, VOTER_ID)
        interaction = make_interaction(user_id=VOTER_ID, role_ids=())

        await view.confirm.callback(interaction)

        assert store.current_vote(poll.poll_id, VOTER_ID) == 2
        kwargs = interaction.response.edit_message.await_args.kwargs
        assert kwargs["view"] is None
        assert "is recorded" in kwargs["content"]
        bot.get_partial_messageable.assert_called_with(CHANNEL_ID)
        embed = bot.post.edit.await_args.kwargs["embed"]
        assert embed.fields[0].value == "1"

    async def test_changing_a_vote_leaves_the_count_alone(
        self,
        store: PollStore,
    ) -> None:
        poll = make_poll(store)
        store.record_vote(poll.poll_id, VOTER_ID, 0)
        bot = make_bot(store)
        view = VoteConfirmView(bot, poll, 1, VOTER_ID, current=0)
        interaction = make_interaction(user_id=VOTER_ID, role_ids=())

        await view.confirm.callback(interaction)

        assert store.current_vote(poll.poll_id, VOTER_ID) == 1
        assert "is now" in interaction.response.edit_message.await_args.kwargs["content"]
        bot.post.edit.assert_not_awaited()

    async def test_a_double_click_records_once(self, store: PollStore) -> None:
        poll = make_poll(store)
        view = VoteConfirmView(make_bot(store), poll, 0, VOTER_ID)
        first = make_interaction(user_id=VOTER_ID, role_ids=())
        second = make_interaction(user_id=VOTER_ID, role_ids=())

        await view.confirm.callback(first)
        await view.confirm.callback(second)

        second.response.edit_message.assert_not_awaited()
        assert store.voter_count(poll.poll_id) == 1

    async def test_a_poll_ended_while_confirming_does_not_count_it(
        self,
        store: PollStore,
    ) -> None:
        poll = make_poll(store)
        bot = make_bot(store)
        view = VoteConfirmView(bot, poll, 0, VOTER_ID)
        store.close_poll(poll.poll_id, OFFICER_ID)
        interaction = make_interaction(user_id=VOTER_ID, role_ids=())

        await view.confirm.callback(interaction)

        content = interaction.response.edit_message.await_args.kwargs["content"]
        assert "not counted" in content
        bot.post.edit.assert_not_awaited()

    async def test_cancelling_records_nothing(self, store: PollStore) -> None:
        poll = make_poll(store)
        store.record_vote(poll.poll_id, VOTER_ID, 0)
        view = VoteConfirmView(make_bot(store), poll, 1, VOTER_ID, current=0)
        interaction = make_interaction(user_id=VOTER_ID, role_ids=())

        await view.cancel.callback(interaction)

        assert store.current_vote(poll.poll_id, VOTER_ID) == 0
        content = interaction.response.edit_message.await_args.kwargs["content"]
        assert "Nothing was recorded" in content
        assert "1️⃣ Monday" in content

    async def test_only_the_voter_can_answer(self, store: PollStore) -> None:
        poll = make_poll(store)
        view = VoteConfirmView(make_bot(store), poll, 0, VOTER_ID)

        assert await view.interaction_check(make_interaction(user_id=VOTER_ID))
        assert not await view.interaction_check(make_interaction(user_id=1))

    async def test_an_expired_confirmation_says_nothing_was_recorded(
        self,
        store: PollStore,
    ) -> None:
        poll = make_poll(store)
        view = VoteConfirmView(make_bot(store), poll, 0, VOTER_ID)
        message = MagicMock()
        message.edit = AsyncMock()
        view.message = message

        await view.on_timeout()

        kwargs = message.edit.await_args.kwargs
        assert kwargs["view"] is None
        assert "expired" in kwargs["content"]


class TestCreateCommand:
    async def test_a_member_without_the_role_is_refused(
        self,
        store: PollStore,
    ) -> None:
        commands = PollCommands(make_bot(store))
        interaction = make_interaction(role_ids=())

        await cast(Any, commands.create.callback)(commands, interaction)

        assert "required role" in notice(interaction)
        interaction.response.send_modal.assert_not_awaited()

    async def test_an_officer_gets_the_form(self, store: PollStore) -> None:
        commands = PollCommands(make_bot(store))
        interaction = make_interaction()

        await cast(Any, commands.create.callback)(commands, interaction)

        modal = interaction.response.send_modal.await_args.args[0]
        assert isinstance(modal, PollCreateModal)

    async def test_the_form_posts_the_poll_in_the_channel(
        self,
        store: PollStore,
    ) -> None:
        channel = make_channel(message_id=4242)
        modal = PollCreateModal(make_bot(store))
        fill_modal(modal, "  Raid   night ", "Monday, Wednesday, Friday", " Pick one ")
        interaction = make_interaction(channel=channel)

        await modal.on_submit(interaction)

        kwargs = channel.send.await_args.kwargs
        assert kwargs["embed"].title == "Raid night"
        assert kwargs["allowed_mentions"] is NO_MENTIONS
        select = kwargs["view"].children[0]
        assert isinstance(select, PollVoteSelect)
        [poll] = store.open_polls()
        assert poll.name == "Raid night"
        assert poll.description == "Pick one"
        assert poll.options == ("Monday", "Wednesday", "Friday")
        assert poll.message_id == 4242
        assert poll.created_by == OFFICER_ID
        assert select.poll_id == poll.poll_id
        assert "Posted **Raid night** with 3 options" in notice(interaction)

    async def test_bad_options_offer_to_try_again_with_what_was_typed(
        self,
        store: PollStore,
    ) -> None:
        channel = make_channel()
        modal = PollCreateModal(make_bot(store))
        fill_modal(modal, "Raid night", "Only one")
        interaction = make_interaction(channel=channel)

        await modal.on_submit(interaction)

        channel.send.assert_not_awaited()
        assert store.open_polls() == []
        call = interaction.response.send_message.await_args
        assert "at least 2" in call.args[0]
        view = call.kwargs["view"]
        assert isinstance(view, PollRetryView)
        retry = make_interaction()
        await view.retry.callback(retry)
        reopened = retry.response.send_modal.await_args.args[0]
        assert reopened.name_input.default == "Raid night"
        assert reopened.options_input.default == "Only one"

    async def test_a_blank_name_is_refused(self, store: PollStore) -> None:
        modal = PollCreateModal(make_bot(store))
        fill_modal(modal, "   ", "Yes, No")
        interaction = make_interaction(channel=make_channel())

        await modal.on_submit(interaction)

        assert "needs a name" in interaction.response.send_message.await_args.args[0]

    async def test_a_role_lost_while_the_form_was_open_posts_nothing(
        self,
        store: PollStore,
    ) -> None:
        channel = make_channel()
        modal = PollCreateModal(make_bot(store))
        fill_modal(modal, "Raid night", "Yes, No")
        interaction = make_interaction(role_ids=(), channel=channel)

        await modal.on_submit(interaction)

        channel.send.assert_not_awaited()
        assert "required role" in notice(interaction)

    async def test_a_post_discord_refuses_leaves_no_poll_behind(
        self,
        store: PollStore,
    ) -> None:
        channel = make_channel()
        channel.send.side_effect = forbidden_error(50013)
        modal = PollCreateModal(make_bot(store))
        fill_modal(modal, "Raid night", "Yes, No")
        interaction = make_interaction(channel=channel)

        await modal.on_submit(interaction)

        assert store.open_polls() == []
        assert notice(interaction) == POST_FAILED_NOTICE

    async def test_a_channel_that_takes_no_messages_is_refused(
        self,
        store: PollStore,
    ) -> None:
        modal = PollCreateModal(make_bot(store))
        fill_modal(modal, "Raid night", "Yes, No")
        interaction = make_interaction(channel=SimpleNamespace())

        await modal.on_submit(interaction)

        assert "text channel" in notice(interaction)
        assert store.open_polls() == []


class TestEndCommand:
    async def test_a_member_without_the_role_is_refused(
        self,
        store: PollStore,
    ) -> None:
        poll = make_poll(store)
        commands = PollCommands(make_bot(store))
        interaction = make_interaction(role_ids=())

        await cast(Any, commands.end.callback)(commands, interaction, poll.poll_id)

        assert "required role" in notice(interaction)
        assert store.get_poll(poll.poll_id).is_open  # type: ignore[union-attr]

    async def test_ending_shows_the_results_and_removes_the_dropdown(
        self,
        store: PollStore,
    ) -> None:
        poll = make_poll(store, name="@everyone vote")
        store.record_vote(poll.poll_id, 1, 1)
        bot = make_bot(store)
        commands = PollCommands(bot)
        interaction = make_interaction()

        await cast(Any, commands.end.callback)(commands, interaction, poll.poll_id)

        assert not store.get_poll(poll.poll_id).is_open  # type: ignore[union-attr]
        edit = bot.post.edit.await_args.kwargs
        assert edit["view"] is None
        assert edit["embed"].title == "@everyone vote (closed)"
        reply = bot.post.reply.await_args
        assert "has ended" in reply.args[0]
        assert "Winner: **Wednesday** with 1 vote." in reply.args[0]
        # The name is free text, so the announcement must not ping anyone.
        assert reply.kwargs["allowed_mentions"] is NO_MENTIONS
        followup = interaction.followup.send.await_args
        assert followup.kwargs["ephemeral"] is True
        assert followup.kwargs["embed"].title == "@everyone vote (closed)"
        assert (
            f"https://discord.com/channels/{GUILD_ID}/{CHANNEL_ID}/555"
            in followup.args[0]
        )

    async def test_a_deleted_post_still_gives_the_officer_the_results(
        self,
        store: PollStore,
    ) -> None:
        poll = make_poll(store)
        bot = make_bot(store)
        bot.post.edit.side_effect = not_found_error()
        commands = PollCommands(bot)
        interaction = make_interaction()

        await cast(Any, commands.end.callback)(commands, interaction, poll.poll_id)

        bot.post.reply.assert_not_awaited()
        followup = interaction.followup.send.await_args
        assert "only shown here" in followup.args[0]
        assert followup.kwargs["embed"].title == "Raid night (closed)"

    async def test_an_unknown_poll_is_reported(self, store: PollStore) -> None:
        commands = PollCommands(make_bot(store))
        interaction = make_interaction()

        await cast(Any, commands.end.callback)(commands, interaction, 404)

        assert notice(interaction) == "There is no poll with id 404."

    async def test_ending_an_ended_poll_puts_its_results_back_without_announcing(
        self,
        store: PollStore,
    ) -> None:
        poll = make_poll(store)
        store.record_vote(poll.poll_id, 1, 2)
        store.close_poll(poll.poll_id, OFFICER_ID)
        bot = make_bot(store)
        commands = PollCommands(bot)
        interaction = make_interaction()

        await cast(Any, commands.end.callback)(commands, interaction, poll.poll_id)

        edit = bot.post.edit.await_args.kwargs
        assert edit["view"] is None
        assert "Winner:** Friday" in (edit["embed"].description or "")
        bot.post.reply.assert_not_awaited()
        followup = interaction.followup.send.await_args
        assert followup.args[0].startswith("**Raid night** had already ended.")
        assert followup.kwargs["embed"].fields[0].value == "1"

    async def test_a_failed_post_edit_is_put_right_by_ending_again(
        self,
        store: PollStore,
    ) -> None:
        poll = make_poll(store)
        store.record_vote(poll.poll_id, 1, 0)
        bot = make_bot(store)
        bot.post.edit.side_effect = [forbidden_error(50013), None]
        commands = PollCommands(bot)

        first = make_interaction()
        await cast(Any, commands.end.callback)(commands, first, poll.poll_id)
        assert "only shown here" in first.followup.send.await_args.args[0]

        second = make_interaction()
        await cast(Any, commands.end.callback)(commands, second, poll.poll_id)

        assert bot.post.edit.await_count == 2
        assert bot.post.edit.await_args.kwargs["view"] is None
        # The tallies were frozen by the first run, not recounted from the
        # votes it deleted.
        embed = second.followup.send.await_args.kwargs["embed"]
        assert "Winner:** Monday" in (embed.description or "")
        assert "The results are on the poll" in second.followup.send.await_args.args[0]


class TestEndAutocomplete:
    async def test_officers_see_open_polls_matching_what_they_typed(
        self,
        store: PollStore,
    ) -> None:
        raid = make_poll(store, name="Raid night")
        make_poll(store, name="Guild hall colour")
        closed = make_poll(store, name="Raid leader")
        store.close_poll(closed.poll_id, OFFICER_ID)
        commands = PollCommands(make_bot(store))

        choices = await commands.open_poll_autocomplete(make_interaction(), "raid")

        assert choices == [
            app_commands.Choice(name=f"Raid night — id {raid.poll_id}", value=raid.poll_id)
        ]

    async def test_a_long_name_is_cut_but_keeps_its_id(
        self,
        store: PollStore,
    ) -> None:
        poll = make_poll(store, name="x" * 100)
        commands = PollCommands(make_bot(store))

        [choice] = await commands.open_poll_autocomplete(make_interaction(), "")

        assert len(choice.name) <= 100
        assert choice.name.endswith(f"— id {poll.poll_id}")

    async def test_members_without_the_role_are_offered_nothing(
        self,
        store: PollStore,
    ) -> None:
        make_poll(store)
        commands = PollCommands(make_bot(store))

        assert await commands.open_poll_autocomplete(
            make_interaction(role_ids=()),
            "",
        ) == []
