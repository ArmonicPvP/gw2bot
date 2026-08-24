import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest
from discord.utils import MISSING
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.events.commands import EventCommands
from gw2bot.events.posting import (
    OccurrenceCancellation,
    cancel_occurrence,
    post_occurrence,
)
from gw2bot.config import DEFAULT_EVENT_CREATE_ROLE_ID as EVENT_CREATE_ROLE_ID
from gw2bot.events.formatting import (
    format_event_datetime,
    next_occurrence_start,
)
from gw2bot.events.scheduler import run_event_maintenance
from gw2bot.events.models import (
    AutoSignupChoice,
    MAX_PING_ROLES,
    STATUS_COLORS,
    EventCategory,
    EventRole,
    EventStatus,
    PreferenceMode,
    RepeatFrequency,
)
from gw2bot.events.store import EventStore
from gw2bot.logging_setup import SecretRegistry, configure_logging
from gw2bot.events.views import (
    EVENT_CHANNEL_TYPES,
    PING_ROLE_OPTION_LIMIT,
    AutoSignupChoiceView,
    CategoryPickView,
    ChangeFieldSelect,
    ChangeFieldView,
    ChannelMoveConfirmView,
    ChannelPickSelect,
    ChannelPickView,
    DisableAutoSignupView,
    EditSignupFlow,
    EditWaitlistConfirmView,
    EventCancelConfirmView,
    EventConfirmView,
    EventDeleteConfirmView,
    EventDetailsConfirmView,
    EventDetailsModal,
    EventDraft,
    EventEditConfirmView,
    EventFieldEditModal,
    EventRosterEditView,
    EventRepeatModal,
    EventScheduleModal,
    EventSignOutButton,
    EventSignUpButton,
    FlexRolesSelect,
    ADD_SELECT_MAX_MEMBERS,
    AddSignupsRoleSelect,
    AddSignupsRoleView,
    AddSignupsSelect,
    AddSignupsView,
    PingRolesPickView,
    PingRolesSelect,
    RemoveSignupsSelect,
    RemoveSignupsView,
    RememberChoiceView,
    RepeatChoiceView,
    RolePickSelect,
    RolePickView,
    SignOutConfirmView,
    SignupFlow,
    SignupSettingsView,
    UpdateRememberedRolesView,
    _departed_summary,
    _describe_signup_settings,
    _ping_role_options,
    _signup_summary,
    build_event_preview,
    build_signup_view,
    draft_from_event,
    pingable_roles,
    send_event_preview,
    start_signup_flow,
)

from factories import default_config, forbidden_error, not_found_error
from test_event_posting import FakeBot, FakeChannel, FakeThread, FakeUser

FUTURE_START_TEXT = "01.30.2107 20:00"


def make_bot() -> Any:
    return cast(
        Any,
        SimpleNamespace(
            event_timezone=ZoneInfo("UTC"),
            event_store=None,
            _config=default_config(),
        ),
    )


def make_interaction(
    *,
    role_ids: tuple[int, ...] = (),
    message: Any = None,
    guild: Any = None,
) -> Any:
    interaction = MagicMock()
    interaction.user = SimpleNamespace(
        id=42,
        roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
    )
    interaction.message = message
    # Explicitly None rather than a MagicMock: display-name resolution reads
    # the guild, and an auto-created mock would answer every member lookup.
    interaction.guild = guild
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    # Commands that cannot answer inside the three-second window defer and
    # follow up instead: /event edit checks the roster against the server
    # first, and /event remind sends the ping before it reports back.
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def ephemeral_message() -> Any:
    return SimpleNamespace(flags=SimpleNamespace(ephemeral=True))


def preview_kwargs(interaction: Any) -> Any:
    """The preview /event edit sent; it defers first, so it is a follow-up."""
    assert interaction.followup.send.await_args is not None
    return interaction.followup.send.await_args.kwargs


class FakeGuild:
    """Answers member lookups the way Discord does for a bot without the intent.

    The member cache is always empty, so every lookup is a fetch, and a member
    who has left raises NotFound.
    """

    def __init__(self, members: dict[int, str]):
        self._members = members
        self.fetched: list[int] = []

    def get_member(self, user_id: int) -> Any:
        return None

    async def fetch_member(self, user_id: int) -> Any:
        self.fetched.append(user_id)
        name = self._members.get(user_id)
        if name is None:
            raise not_found_error()
        return SimpleNamespace(id=user_id, display_name=name)


class TestEventCommandGroup:
    def test_registers_event_command_group(self) -> None:
        group = EventCommands(make_bot())
        commands = {command.name for command in group.commands}

        assert group.name == "event"
        assert group.guild_only
        assert commands == {"new", "edit", "remind", "cancel", "delete"}

    async def test_new_rejects_users_without_the_create_role(self) -> None:
        group = EventCommands(make_bot())
        interaction = make_interaction()

        await cast(Any, group.new.callback)(group, interaction)

        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args is not None
        kwargs = interaction.response.send_message.await_args.kwargs
        assert kwargs["ephemeral"] is True
        interaction.response.send_modal.assert_not_awaited()

    async def test_new_opens_the_details_modal_for_authorized_users(
        self,
    ) -> None:
        group = EventCommands(make_bot())
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.new.callback)(group, interaction)

        interaction.response.send_modal.assert_awaited_once()
        assert interaction.response.send_modal.await_args is not None
        modal = interaction.response.send_modal.await_args.args[0]
        assert isinstance(modal, EventDetailsModal)

    async def test_new_builds_the_ping_picker_from_the_server(self) -> None:
        group = EventCommands(make_bot())
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            guild=ping_guild(),
        )

        await cast(Any, group.new.callback)(group, interaction)

        modal = interaction.response.send_modal.await_args.args[0]
        assert modal.ping_roles is not None
        assert option_values(modal.ping_roles) == ["10", "20", "30"]


class TestEventDraft:
    def test_incomplete_draft_reports_missing_fields(self) -> None:
        draft = EventDraft(leader_discord_id=42)

        assert not draft.is_complete()
        with pytest.raises(ValueError, match="missing required fields"):
            draft.to_event()

    def test_complete_draft_builds_an_event(self) -> None:
        draft = EventDraft(
            leader_discord_id=42,
            category=EventCategory.RAID,
            title="Kitty Cleanup",
            description="Bring food.",
            channel_id=1234,
            start_time=datetime(2107, 1, 30, 20, 0, tzinfo=UTC),
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.WEEKLY,
            repeat_days=(6,),
        )

        assert draft.is_complete()
        event = draft.to_event(event_id=9)
        assert event.event_id == 9
        assert event.category is EventCategory.RAID
        assert event.repeat_days == (6,)


def make_forum_post(post_id: int = 901, parent_id: int = 4321) -> Any:
    return SimpleNamespace(
        id=post_id,
        type=discord.ChannelType.public_thread,
        parent_id=parent_id,
    )


def make_channel_thread(thread_id: int = 902, parent_id: int = 1234) -> Any:
    return SimpleNamespace(
        id=thread_id,
        type=discord.ChannelType.public_thread,
        parent_id=parent_id,
    )


def make_destination_bot() -> Any:
    # A forum and a text channel, so a picked thread's parent decides whether it
    # is a forum post or a thread under a channel.
    channels = {
        4321: SimpleNamespace(id=4321, type=discord.ChannelType.forum),
        1234: SimpleNamespace(id=1234, type=discord.ChannelType.text),
    }
    return cast(
        Any,
        SimpleNamespace(
            event_timezone=ZoneInfo("UTC"),
            event_store=None,
            get_channel=channels.get,
            fetch_channel=AsyncMock(side_effect=not_found_error()),
        ),
    )


class TestEventChannelChoices:
    """An event goes to a text channel or into a forum post that exists."""

    def test_details_modal_offers_channels_and_forum_posts(self) -> None:
        modal = EventDetailsModal(make_bot(), EventDraft(leader_discord_id=42))

        assert discord.ChannelType.text in modal.channel.channel_types
        # A forum post is a public thread; Discord's picker has no narrower type.
        assert discord.ChannelType.public_thread in modal.channel.channel_types

    def test_change_channel_picker_offers_the_same_choices(self) -> None:
        select = ChannelPickSelect()

        assert set(select.channel_types) == set(EVENT_CHANNEL_TYPES)

    def test_forum_and_private_thread_types_are_never_offered(self) -> None:
        # Posting to a forum channel would open a new post, and a private thread
        # is never a forum post; events go to a channel or an existing post.
        assert discord.ChannelType.forum not in EVENT_CHANNEL_TYPES
        assert discord.ChannelType.media not in EVENT_CHANNEL_TYPES
        assert discord.ChannelType.private_thread not in EVENT_CHANNEL_TYPES

    async def test_details_modal_accepts_a_forum_post(self) -> None:
        draft = EventDraft(leader_discord_id=42)
        modal = EventDetailsModal(make_destination_bot(), draft)
        modal.category._values = ["Raid"]
        modal.title_input._value = "Kitty Cleanup"
        modal.description_input._value = "Bring food."
        cast(Any, modal.channel)._values = [make_forum_post()]
        interaction = make_interaction()

        await modal.on_submit(interaction)

        assert draft.channel_id == 901
        kwargs = interaction.response.send_message.await_args.kwargs
        assert isinstance(kwargs["view"], EventDetailsConfirmView)

    async def test_details_modal_rejects_a_thread_under_a_channel(self) -> None:
        draft = EventDraft(leader_discord_id=42)
        modal = EventDetailsModal(make_destination_bot(), draft)
        modal.category._values = ["Raid"]
        modal.title_input._value = "Kitty Cleanup"
        modal.description_input._value = "Bring food."
        cast(Any, modal.channel)._values = [make_channel_thread()]
        interaction = make_interaction()

        await modal.on_submit(interaction)

        # Only forum posts are supported, and the retry keeps the rest of the
        # details so just the destination has to be picked again.
        assert draft.channel_id is None
        assert draft.title == "Kitty Cleanup"
        text = interaction.response.send_message.await_args.args[0]
        assert "forum post" in text
        assert "Try again" in text

    async def test_change_channel_picker_rejects_a_thread_under_a_channel(
        self,
    ) -> None:
        draft = EventDraft(leader_discord_id=42, channel_id=1234)
        view = ChannelPickView(make_destination_bot(), draft)
        interaction = make_interaction(message=ephemeral_message())

        await view.pick(interaction, make_channel_thread())

        assert draft.channel_id == 1234
        kwargs = interaction.response.edit_message.await_args.kwargs
        assert "forum post" in kwargs["content"]
        assert isinstance(kwargs["view"], ChannelPickView)

    async def test_change_channel_picker_accepts_a_forum_post(self) -> None:
        draft = EventDraft(
            leader_discord_id=42,
            category=EventCategory.RAID,
            title="Kitty Cleanup",
            description="Bring food.",
            channel_id=1234,
            start_time=datetime(2107, 1, 30, 20, 0, tzinfo=UTC),
            duration_minutes=90,
        )
        view = ChannelPickView(make_destination_bot(), draft)
        interaction = make_interaction(message=ephemeral_message())

        await view.pick(interaction, make_forum_post())

        assert draft.channel_id == 901


class TestEventDetailsModal:
    async def test_submit_stores_details_and_previews_them(self) -> None:
        draft = EventDraft(leader_discord_id=42)
        modal = EventDetailsModal(make_bot(), draft)
        modal.category._values = ["Raid"]
        modal.title_input._value = "  Kitty Cleanup  "
        modal.description_input._value = "Bring food."
        cast(Any, modal.channel)._values = [SimpleNamespace(id=1234)]
        interaction = make_interaction()

        await modal.on_submit(interaction)

        assert draft.category is EventCategory.RAID
        assert draft.title == "Kitty Cleanup"
        assert draft.description == "Bring food."
        assert draft.channel_id == 1234
        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args is not None
        kwargs = interaction.response.send_message.await_args.kwargs
        assert kwargs["ephemeral"] is True
        # The details are shown back as an event preview rather than as a
        # "press Continue" prompt, so what was entered can be checked first.
        preview, confirmation = kwargs["embeds"]
        assert "Kitty Cleanup" in cast(str, preview.title)
        assert preview.description == "Bring food."
        assert "<#1234>" in {field.value for field in preview.fields}
        assert "Next" in cast(str, confirmation.description)
        assert isinstance(kwargs["view"], EventDetailsConfirmView)


class FakeRole:
    def __init__(self, role_id: int, name: str):
        self.id = role_id
        self.name = name


class FakeRoleGuild:
    """A guild that answers the role lookups the ping picker makes."""

    def __init__(self, *roles: FakeRole):
        # @everyone is always in a real guild's role list, so it is always in
        # this one: the picker must never offer it.
        self.roles = [FakeRole(1, "@everyone"), *roles]

    def get_role(self, role_id: int) -> Any:
        return next(
            (role for role in self.roles if role.id == role_id),
            None,
        )


def ping_guild() -> Any:
    return cast(
        Any,
        FakeRoleGuild(
            FakeRole(20, "[GW2] Raiders"),
            FakeRole(10, "[GW2] Fractals"),
            FakeRole(30, "[gw2] WvW"),
            FakeRole(40, "Officers"),
            FakeRole(50, "Raiders [GW2]"),
        ),
    )


def values_of(options: list[discord.SelectOption]) -> list[str]:
    return [option.value for option in options]


def option_values(select: Any) -> list[str]:
    return values_of(select.options)


class TestPingRoleOptions:
    def test_only_marked_roles_are_offered_sorted_by_name(self) -> None:
        roles = pingable_roles(ping_guild())

        # A role only counts when the marker starts its name, so "Raiders
        # [GW2]" and the officer role are out, and @everyone can never be
        # reached from here.
        assert [role.id for role in roles] == [10, 20, 30]

    def test_the_marker_is_matched_regardless_of_case(self) -> None:
        assert 30 in {role.id for role in pingable_roles(ping_guild())}

    def test_no_guild_offers_nothing(self) -> None:
        assert pingable_roles(None) == []

    def test_the_drafts_roles_are_preselected(self) -> None:
        options = _ping_role_options(ping_guild(), (20,))

        assert [option.default for option in options] == [False, True, False]

    def test_a_picked_role_that_lost_the_marker_is_still_offered(self) -> None:
        # It was a valid choice when it was picked, so an edit about something
        # else must not quietly stop the event pinging it.
        options = _ping_role_options(ping_guild(), (40,))

        assert values_of(options)[0] == "40"
        assert options[0].default is True

    def test_a_picked_role_that_no_longer_exists_is_dropped(self) -> None:
        # Discord would refuse the mention anyway.
        options = _ping_role_options(ping_guild(), (999,))

        assert "999" not in values_of(options)

    def test_the_option_list_stays_within_what_discord_accepts(self) -> None:
        guild = cast(
            Any,
            FakeRoleGuild(
                *(
                    FakeRole(100 + index, f"[GW2] Squad {index:03d}")
                    for index in range(40)
                )
            ),
        )

        options = _ping_role_options(guild, ())

        assert len(options) == PING_ROLE_OPTION_LIMIT


class TestPingRolesInTheDetailsModal:
    def test_the_picker_is_the_modals_fifth_question(self) -> None:
        modal = EventDetailsModal(
            make_bot(),
            EventDraft(leader_discord_id=42),
            ping_guild(),
        )

        # Discord allows a modal five components, so the roles are asked up
        # front rather than behind "Change something".
        assert len(modal.children) == 5
        assert modal.ping_roles is not None
        assert option_values(modal.ping_roles) == ["10", "20", "30"]

    def test_no_more_than_three_roles_may_be_picked(self) -> None:
        modal = EventDetailsModal(
            make_bot(),
            EventDraft(leader_discord_id=42),
            ping_guild(),
        )

        assert modal.ping_roles is not None
        assert modal.ping_roles.max_values == MAX_PING_ROLES
        # Picking none is a valid answer, so the question stays optional.
        assert modal.ping_roles.min_values == 0
        assert modal.ping_roles.required is False

    def test_a_server_without_marked_roles_gets_no_picker(self) -> None:
        # Discord refuses a select with no options, and there is nothing to
        # choose from anyway.
        guild = cast(Any, FakeRoleGuild(FakeRole(40, "Officers")))

        modal = EventDetailsModal(
            make_bot(),
            EventDraft(leader_discord_id=42),
            guild,
        )

        assert modal.ping_roles is None
        assert len(modal.children) == 4

    def test_max_values_never_exceeds_the_offered_roles(self) -> None:
        guild = cast(Any, FakeRoleGuild(FakeRole(10, "[GW2] Fractals")))

        modal = EventDetailsModal(
            make_bot(),
            EventDraft(leader_discord_id=42),
            guild,
        )

        assert modal.ping_roles is not None
        assert modal.ping_roles.max_values == 1

    async def test_submit_stores_the_picked_roles(self) -> None:
        draft = EventDraft(leader_discord_id=42)
        modal = EventDetailsModal(make_bot(), draft, ping_guild())
        modal.category._values = ["Raid"]
        modal.title_input._value = "Kitty Cleanup"
        modal.description_input._value = "Bring food."
        cast(Any, modal.channel)._values = [SimpleNamespace(id=1234)]
        assert modal.ping_roles is not None
        modal.ping_roles._values = ["20", "10"]

        await modal.on_submit(make_interaction())

        assert draft.ping_role_ids == (20, 10)

    async def test_submit_with_nothing_picked_clears_the_roles(self) -> None:
        draft = EventDraft(leader_discord_id=42, ping_role_ids=(20,))
        modal = EventDetailsModal(make_bot(), draft, ping_guild())
        modal.category._values = ["Raid"]
        modal.title_input._value = "Kitty Cleanup"
        modal.description_input._value = "Bring food."
        cast(Any, modal.channel)._values = [SimpleNamespace(id=1234)]
        assert modal.ping_roles is not None
        modal.ping_roles._values = []

        await modal.on_submit(make_interaction())

        # The picker was shown with the roles pre-selected, so deselecting them
        # is how they are taken off the event.
        assert draft.ping_role_ids == ()

    async def test_submit_without_a_picker_leaves_the_roles_alone(
        self,
    ) -> None:
        # A server with no marked roles gets no picker, which must not be read
        # as "the commander cleared the roles".
        draft = EventDraft(leader_discord_id=42, ping_role_ids=(20,))
        modal = EventDetailsModal(make_bot(), draft)
        modal.category._values = ["Raid"]
        modal.title_input._value = "Kitty Cleanup"
        modal.description_input._value = "Bring food."
        cast(Any, modal.channel)._values = [SimpleNamespace(id=1234)]

        await modal.on_submit(make_interaction())

        assert draft.ping_role_ids == (20,)

    async def test_a_role_that_was_not_offered_is_refused(self) -> None:
        # A modal submission is a client-supplied payload, so the picker's
        # options - not the payload - decide which roles an event may carry.
        draft = EventDraft(leader_discord_id=42)
        modal = EventDetailsModal(make_bot(), draft, ping_guild())
        modal.category._values = ["Raid"]
        modal.title_input._value = "Kitty Cleanup"
        modal.description_input._value = "Bring food."
        cast(Any, modal.channel)._values = [SimpleNamespace(id=1234)]
        assert modal.ping_roles is not None
        # 40 is the officer role, which the picker never offered.
        modal.ping_roles._values = ["40", "20"]

        await modal.on_submit(make_interaction())

        assert draft.ping_role_ids == (20,)

    async def test_a_rejected_destination_keeps_the_picked_roles(self) -> None:
        draft = EventDraft(leader_discord_id=42)
        guild = ping_guild()
        modal = EventDetailsModal(make_destination_bot(), draft, guild)
        modal.category._values = ["Raid"]
        modal.title_input._value = "Kitty Cleanup"
        modal.description_input._value = "Bring food."
        cast(Any, modal.channel)._values = [make_channel_thread()]
        assert modal.ping_roles is not None
        modal.ping_roles._values = ["20"]
        interaction = make_interaction()

        await modal.on_submit(interaction)

        assert draft.ping_role_ids == (20,)
        # The retry modal reopens with the roles still picked, so only the
        # destination has to be answered again.
        retry = interaction.response.send_message.await_args.kwargs["view"]
        retried = retry.build_modal()
        assert retried.ping_roles is not None
        assert [
            option.value
            for option in retried.ping_roles.options
            if option.default
        ] == ["20"]


class TestPingRolesInTheChangeFlow:
    def make_draft(self) -> EventDraft:
        return EventDraft(
            leader_discord_id=42,
            category=EventCategory.RAID,
            title="Kitty Cleanup",
            description="Bring food.",
            channel_id=1234,
            start_time=datetime(2107, 1, 30, 20, 0, tzinfo=UTC),
            duration_minutes=90,
        )

    def test_the_full_change_list_offers_the_roles(self) -> None:
        assert "ping_roles" in [
            option.value for option in ChangeFieldSelect().options
        ]

    async def test_choosing_it_opens_the_picker(self) -> None:
        draft = self.make_draft()
        view = ChangeFieldView(make_bot(), draft)
        interaction = make_interaction(
            message=ephemeral_message(),
            guild=ping_guild(),
        )

        await view.handle_choice(interaction, "ping_roles")

        kwargs = interaction.response.edit_message.await_args.kwargs
        assert isinstance(kwargs["view"], PingRolesPickView)
        select = next(
            item
            for item in kwargs["view"].children
            if isinstance(item, PingRolesSelect)
        )
        assert option_values(select) == ["10", "20", "30"]

    async def test_a_server_without_marked_roles_says_so(self) -> None:
        draft = self.make_draft()
        view = ChangeFieldView(make_bot(), draft)
        interaction = make_interaction(
            message=ephemeral_message(),
            guild=cast(Any, FakeRoleGuild()),
        )

        await view.handle_choice(interaction, "ping_roles")

        kwargs = interaction.response.edit_message.await_args.kwargs
        # A picker with no options is a payload Discord refuses, so the
        # commander is told why and put back on the preview.
        assert "[GW2]" in kwargs["content"]
        assert isinstance(kwargs["view"], EventConfirmView)

    async def test_picking_roles_returns_to_the_preview(self) -> None:
        draft = self.make_draft()
        view = PingRolesPickView(
            make_bot(),
            draft,
            _ping_role_options(ping_guild(), ()),
        )
        interaction = make_interaction(message=ephemeral_message())

        await view.pick(interaction, (10, 20))

        assert draft.ping_role_ids == (10, 20)
        kwargs = interaction.response.edit_message.await_args.kwargs
        assert isinstance(kwargs["view"], EventConfirmView)

    async def test_the_picker_refuses_a_role_it_did_not_offer(self) -> None:
        draft = self.make_draft()
        view = PingRolesPickView(
            make_bot(),
            draft,
            _ping_role_options(ping_guild(), ()),
        )
        select = next(
            item for item in view.children if isinstance(item, PingRolesSelect)
        )
        select._values = ["40", "10"]

        await select.callback(make_interaction(message=ephemeral_message()))

        assert draft.ping_role_ids == (10,)

    async def test_a_retained_role_may_still_be_picked(self) -> None:
        # It lost the marker but the event already pings it, so the picker
        # offers it and picking it is honoured. Whether it is still *notified*
        # is settled against the server when the post goes out.
        draft = replace(self.make_draft(), ping_role_ids=(40,))
        view = PingRolesPickView(
            make_bot(),
            draft,
            _ping_role_options(ping_guild(), (40,)),
        )
        select = next(
            item for item in view.children if isinstance(item, PingRolesSelect)
        )
        select._values = ["40"]

        await select.callback(make_interaction(message=ephemeral_message()))

        assert draft.ping_role_ids == (40,)

    async def test_picking_nothing_clears_the_roles(self) -> None:
        draft = replace(self.make_draft(), ping_role_ids=(10,))
        view = PingRolesPickView(
            make_bot(),
            draft,
            _ping_role_options(ping_guild(), (10,)),
        )
        interaction = make_interaction(message=ephemeral_message())

        await view.pick(interaction, ())

        assert draft.ping_role_ids == ()

    def test_the_preview_says_which_roles_will_be_pinged(self) -> None:
        draft = replace(self.make_draft(), ping_role_ids=(10, 20))

        embeds, _ = build_event_preview(make_bot(), draft)

        # Mentions inside an embed never notify anybody, so the preview can
        # show them as role chips without pinging the server.
        assert "<@&10> <@&20>" in cast(str, embeds[1].description)

    def test_the_preview_says_when_nothing_will_be_pinged(self) -> None:
        embeds, _ = build_event_preview(make_bot(), self.make_draft())

        assert "No roles are pinged" in cast(str, embeds[1].description)

    def test_the_step_one_preview_says_it_too(self) -> None:
        draft = EventDraft(
            leader_discord_id=42,
            category=EventCategory.RAID,
            title="Kitty Cleanup",
            description="Bring food.",
            channel_id=1234,
            ping_role_ids=(10,),
        )

        embeds, _ = build_event_preview(make_bot(), draft)

        assert "<@&10>" in cast(str, embeds[1].description)


class TestEventDetailsConfirmView:
    def make_draft(self) -> EventDraft:
        return EventDraft(
            leader_discord_id=42,
            category=EventCategory.RAID,
            title="Kitty Cleanup",
            description="Bring food.",
            channel_id=1234,
        )

    def buttons(self, view: discord.ui.View) -> list[str]:
        return [
            cast(str, item.label)
            for item in view.children
            if isinstance(item, discord.ui.Button)
        ]

    def test_offers_next_and_change_something(self) -> None:
        view = EventDetailsConfirmView(make_bot(), self.make_draft())

        assert set(self.buttons(view)) == {"Next", "Change something"}

    async def test_next_opens_the_schedule_modal(self) -> None:
        draft = self.make_draft()
        view = EventDetailsConfirmView(make_bot(), draft)
        interaction = make_interaction(message=ephemeral_message())

        await cast(Any, view.next_step.callback)(interaction)

        interaction.response.send_modal.assert_awaited_once()
        modal = interaction.response.send_modal.await_args.args[0]
        assert isinstance(modal, EventScheduleModal)

    async def test_change_something_only_offers_entered_fields(self) -> None:
        draft = self.make_draft()
        view = EventDetailsConfirmView(make_bot(), draft)
        interaction = make_interaction(message=ephemeral_message())

        await cast(Any, view.change_something.callback)(interaction)

        kwargs = interaction.response.send_message.await_args.kwargs
        change_view = kwargs["view"]
        assert isinstance(change_view, ChangeFieldView)
        select = next(
            item
            for item in change_view.children
            if isinstance(item, ChangeFieldSelect)
        )
        # The schedule and repeat questions have not been asked yet, so they
        # cannot be answered out of order from this preview.
        assert [option.value for option in select.options] == [
            "category",
            "title",
            "description",
            "channel",
            "leader",
            "ping_roles",
        ]

    async def test_a_change_returns_to_the_details_preview(self) -> None:
        draft = self.make_draft()
        view = CategoryPickView(make_bot(), draft)
        interaction = make_interaction(message=ephemeral_message())

        await view.pick(interaction, EventCategory.FRACTAL)

        assert draft.category is EventCategory.FRACTAL
        kwargs = interaction.response.edit_message.await_args.kwargs
        assert isinstance(kwargs["view"], EventDetailsConfirmView)
        assert len(kwargs["embeds"]) == 2

    def test_a_complete_draft_gets_the_full_preview(self) -> None:
        draft = replace(
            self.make_draft(),
            start_time=datetime(2107, 1, 30, 20, 0, tzinfo=UTC),
            duration_minutes=90,
        )

        _, view = build_event_preview(make_bot(), draft)

        assert isinstance(view, EventConfirmView)


class TestEventPreviewLogging:
    """The preview embeds carry the event; the log must not."""

    TITLE = "SECRET EVENT TITLE"
    DESCRIPTION = "SECRET EVENT DESCRIPTION"

    def make_draft(self, complete: bool) -> EventDraft:
        draft = EventDraft(
            leader_discord_id=42,
            category=EventCategory.RAID,
            title=self.TITLE,
            description=self.DESCRIPTION,
            channel_id=1234,
        )
        if not complete:
            return draft
        return replace(
            draft,
            start_time=datetime(2107, 1, 30, 20, 0, tzinfo=UTC),
            start_text=FUTURE_START_TEXT,
            duration_minutes=90,
            duration_text="01:30",
        )

    async def test_details_preview_logging_omits_the_event_content(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        draft = EventDraft(leader_discord_id=42)
        modal = EventDetailsModal(make_bot(), draft)
        modal.category._values = ["Raid"]
        modal.title_input._value = self.TITLE
        modal.description_input._value = self.DESCRIPTION
        cast(Any, modal.channel)._values = [SimpleNamespace(id=1234)]
        interaction = make_interaction()

        with caplog.at_level("DEBUG"):
            await modal.on_submit(interaction)

        assert self.TITLE not in caplog.text
        assert self.DESCRIPTION not in caplog.text
        # The step is still traceable end to end, including which preview the
        # commander was shown.
        assert "Event details step submitted" in caplog.text
        assert "Sending event preview" in caplog.text
        assert "complete=False" in caplog.text

    async def test_full_preview_logging_omits_the_event_content(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        draft = self.make_draft(complete=True)
        interaction = make_interaction()

        with caplog.at_level("DEBUG"):
            await send_event_preview(make_bot(), interaction, draft)

        assert self.TITLE not in caplog.text
        assert self.DESCRIPTION not in caplog.text
        assert "Sending event preview" in caplog.text
        assert "complete=True" in caplog.text

    async def test_next_step_logging_omits_the_event_content(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        draft = self.make_draft(complete=False)
        view = EventDetailsConfirmView(make_bot(), draft)
        interaction = make_interaction(message=ephemeral_message())

        with caplog.at_level("DEBUG"):
            await cast(Any, view.next_step.callback)(interaction)

        assert self.TITLE not in caplog.text
        assert self.DESCRIPTION not in caplog.text
        assert "continued to the schedule step" in caplog.text


class TestEventScheduleModal:
    def make_draft(self) -> EventDraft:
        return EventDraft(
            leader_discord_id=42,
            category=EventCategory.FRACTAL,
            title="Kitty Cleanup",
            description="Bring food.",
            channel_id=1234,
        )

    async def test_submit_without_repeat_shows_the_preview(self) -> None:
        draft = self.make_draft()
        modal = EventScheduleModal(make_bot(), draft)
        modal.start_input._value = FUTURE_START_TEXT
        modal.duration_input._value = "01:30"
        modal.repeat._values = ["no"]
        interaction = make_interaction()

        await modal.on_submit(interaction)

        assert draft.start_time == datetime(2107, 1, 30, 20, 0, tzinfo=UTC)
        assert draft.duration_minutes == 90
        assert draft.repeat_frequency is RepeatFrequency.NONE
        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args is not None
        kwargs = interaction.response.send_message.await_args.kwargs
        assert len(kwargs["embeds"]) == 2

    async def test_submit_with_repeat_offers_step_three(self) -> None:
        draft = self.make_draft()
        modal = EventScheduleModal(make_bot(), draft)
        modal.start_input._value = FUTURE_START_TEXT
        modal.duration_input._value = "01:30"
        modal.repeat._values = ["yes"]
        interaction = make_interaction(message=ephemeral_message())

        await modal.on_submit(interaction)

        interaction.response.edit_message.assert_awaited_once()
        assert interaction.response.edit_message.await_args is not None
        kwargs = interaction.response.edit_message.await_args.kwargs
        assert "Step 3" in kwargs["content"]

    async def test_submit_rejects_past_start_times_with_retry(self) -> None:
        draft = self.make_draft()
        modal = EventScheduleModal(make_bot(), draft)
        modal.start_input._value = "01.30.2007 20:00"
        modal.duration_input._value = "01:30"
        modal.repeat._values = ["no"]
        interaction = make_interaction()

        await modal.on_submit(interaction)

        assert draft.start_time is None
        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args is not None
        text = interaction.response.send_message.await_args.args[0]
        assert "in the future" in text
        assert "Try again" in text

    async def test_submit_rejects_bad_duration_with_retry(self) -> None:
        draft = self.make_draft()
        modal = EventScheduleModal(make_bot(), draft)
        modal.start_input._value = FUTURE_START_TEXT
        modal.duration_input._value = "ninety"
        modal.repeat._values = ["no"]
        interaction = make_interaction()

        await modal.on_submit(interaction)

        assert draft.duration_minutes is None
        interaction.response.send_message.assert_awaited_once()


class TestModalComponentLimits:
    """Discord rejects an over-long label with a 400 at send_modal time.

    Nothing in the type system or the library catches it, so every modal in
    the event flow is built here and measured against Discord's limits.
    """

    # https://discord.com/developers/docs/components/reference
    LABEL_MAX_LENGTH = 45
    DESCRIPTION_MAX_LENGTH = 100

    def make_draft(self) -> EventDraft:
        return EventDraft(
            leader_discord_id=42,
            category=EventCategory.FRACTAL,
            title="Kitty Cleanup",
            description="Bring food.",
            channel_id=1234,
            start_time=datetime(2107, 1, 30, 20, 0, tzinfo=UTC),
            start_text=FUTURE_START_TEXT,
            duration_minutes=90,
            duration_text="01:30",
            repeat_frequency=RepeatFrequency.DAILY,
        )

    def modals(self) -> list[discord.ui.Modal]:
        bot = make_bot()
        return [
            EventDetailsModal(bot, self.make_draft()),
            EventScheduleModal(bot, self.make_draft()),
            EventRepeatModal(bot, self.make_draft()),
            *(
                EventFieldEditModal(bot, self.make_draft(), field_name)
                for field_name in ("title", "description", "start", "duration")
            ),
        ]

    def test_labels_are_within_discord_limits(self) -> None:
        for modal in self.modals():
            labels = [
                item
                for item in modal.children
                if isinstance(item, discord.ui.Label)
            ]
            assert labels
            for label in labels:
                assert 1 <= len(label.text) <= self.LABEL_MAX_LENGTH, (
                    f"{type(modal).__name__} label {label.text!r} is "
                    f"{len(label.text)} characters"
                )
                if label.description is not None:
                    assert (
                        1
                        <= len(label.description)
                        <= self.DESCRIPTION_MAX_LENGTH
                    ), (
                        f"{type(modal).__name__} description "
                        f"{label.description!r} is "
                        f"{len(label.description)} characters"
                    )


class TestEventRepeatModal:
    def make_draft(self) -> EventDraft:
        return EventDraft(
            leader_discord_id=42,
            category=EventCategory.FRACTAL,
            title="Kitty Cleanup",
            description="Bring food.",
            channel_id=1234,
            start_time=datetime(2107, 1, 30, 20, 0, tzinfo=UTC),
            start_text=FUTURE_START_TEXT,
            duration_minutes=90,
            duration_text="01:30",
            repeat_frequency=RepeatFrequency.DAILY,
        )

    async def test_submit_weekly_days_shows_the_preview(self) -> None:
        draft = self.make_draft()
        modal = EventRepeatModal(make_bot(), draft)
        modal.frequency._values = ["weekly"]
        modal.days_input._value = "Sunday, Wednesday"
        modal.delete_previous._values = ["yes"]
        interaction = make_interaction()

        await modal.on_submit(interaction)

        assert draft.repeat_frequency is RepeatFrequency.WEEKLY
        assert draft.repeat_days == (2, 6)
        assert draft.delete_previous_on_repeat is True
        interaction.response.send_message.assert_awaited_once()
        kwargs = interaction.response.send_message.await_args.kwargs
        assert "removing the previous post" in kwargs["embeds"][1].description

    async def test_submit_invalid_days_offers_retry(self) -> None:
        draft = self.make_draft()
        modal = EventRepeatModal(make_bot(), draft)
        modal.frequency._values = ["monthly"]
        modal.days_input._value = "first"
        modal.delete_previous._values = ["no"]
        interaction = make_interaction()

        await modal.on_submit(interaction)

        assert draft.repeat_days == ()
        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args is not None
        assert (
            "Try again"
            in interaction.response.send_message.await_args.args[0]
        )

    async def test_invalid_days_leave_the_draft_untouched(self) -> None:
        # A frequency stored without its days would describe an event that
        # repeats on no day at all. next_occurrence_start raises on one of
        # those, and that aborts the whole maintenance pass, so it must never
        # reach a draft a still-open preview could post.
        draft = replace(
            self.make_draft(),
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days_text="",
            delete_previous_on_repeat=False,
        )
        modal = EventRepeatModal(make_bot(), draft)
        modal.frequency._values = ["weekly"]
        modal.days_input._value = "someday"
        modal.delete_previous._values = ["yes"]

        await modal.on_submit(make_interaction())

        assert draft.repeat_frequency is RepeatFrequency.NONE
        assert draft.repeat_days == ()
        assert draft.repeat_days_text == ""
        assert draft.delete_previous_on_repeat is False

    async def test_a_rejected_attempt_refills_the_retry_modal(self) -> None:
        # The draft never took the answers on, so the retry has to carry them.
        draft = replace(
            self.make_draft(),
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days_text="",
        )
        modal = EventRepeatModal(make_bot(), draft)
        modal.frequency._values = ["weekly"]
        modal.days_input._value = "someday"
        modal.delete_previous._values = ["yes"]
        interaction = make_interaction()

        await modal.on_submit(interaction)

        retry = interaction.response.send_message.await_args.kwargs["view"]
        refilled = cast(EventRepeatModal, retry.build_modal())
        assert refilled.days_input.default == "someday"
        assert [
            option.value
            for option in refilled.frequency.options
            if option.default
        ] == ["weekly"]
        assert [
            option.value
            for option in refilled.delete_previous.options
            if option.default
        ] == ["yes"]

    async def test_a_stale_preview_cannot_post_a_dayless_repeat(self) -> None:
        # The rejection message is not the only live message: a preview from an
        # earlier step shares the draft and can still complete it.
        draft = replace(
            self.make_draft(),
            repeat_frequency=RepeatFrequency.NONE,
        )
        modal = EventRepeatModal(make_bot(), draft)
        modal.frequency._values = ["weekly"]
        modal.days_input._value = "someday"
        modal.delete_previous._values = ["no"]
        await modal.on_submit(make_interaction())

        event = draft.to_event()

        assert event.repeat_frequency is RepeatFrequency.NONE
        with pytest.raises(ValueError, match="no next occurrence"):
            next_occurrence_start(
                event.repeat_frequency,
                event.repeat_days,
                event.start_time,
                ZoneInfo("UTC"),
            )

    def test_an_unanswered_frequency_preselects_nothing(self) -> None:
        draft = replace(
            self.make_draft(),
            repeat_frequency=RepeatFrequency.NONE,
        )

        modal = EventRepeatModal(make_bot(), draft)

        # Required with nothing preselected: the commander has to choose a
        # frequency, so none can be inherited from a placeholder.
        assert modal.frequency.required
        assert not any(option.default for option in modal.frequency.options)


class TestUnansweredRepeatSettings:
    """Saying "yes, it repeats" must not itself put a frequency on the draft.

    A preview left open at an earlier step keeps working off the same draft, so
    a frequency written before the repeat modal is answered could be posted
    from there without anyone having chosen it.
    """

    def make_draft(self) -> EventDraft:
        return EventDraft(
            leader_discord_id=42,
            category=EventCategory.RAID,
            title="Kitty Cleanup",
            description="Bring food.",
            channel_id=1234,
        )

    async def test_schedule_step_leaves_the_frequency_unset(self) -> None:
        draft = self.make_draft()
        modal = EventScheduleModal(make_bot(), draft)
        modal.start_input._value = FUTURE_START_TEXT
        modal.duration_input._value = "01:30"
        modal.repeat._values = ["yes"]
        interaction = make_interaction(message=ephemeral_message())

        await modal.on_submit(interaction)

        assert "Step 3" in (
            interaction.response.edit_message.await_args.kwargs["content"]
        )
        assert draft.repeat_frequency is RepeatFrequency.NONE

    async def test_repeat_choice_leaves_the_frequency_unset(self) -> None:
        draft = replace(
            self.make_draft(),
            start_time=datetime(2107, 1, 30, 20, 0, tzinfo=UTC),
            duration_minutes=90,
        )
        view = RepeatChoiceView(make_bot(), draft)
        interaction = make_interaction(message=ephemeral_message())

        await cast(Any, view.repeat_yes.callback)(interaction)

        interaction.response.send_modal.assert_awaited_once()
        assert draft.repeat_frequency is RepeatFrequency.NONE

    async def test_an_abandoned_repeat_step_previews_as_non_repeating(
        self,
    ) -> None:
        # The step-three prompt is not the only live message: a preview from an
        # earlier step can still complete the draft. What it offers to post has
        # to be what it shows.
        draft = self.make_draft()
        schedule = EventScheduleModal(make_bot(), draft)
        schedule.start_input._value = FUTURE_START_TEXT
        schedule.duration_input._value = "01:30"
        schedule.repeat._values = ["yes"]
        await schedule.on_submit(make_interaction(message=ephemeral_message()))

        embeds, view = build_event_preview(make_bot(), draft)

        assert isinstance(view, EventConfirmView)
        assert "Does not repeat" in cast(str, embeds[1].description)
        assert draft.to_event().repeat_frequency is RepeatFrequency.NONE


@pytest.fixture
def store(tmp_path: Path):
    store = EventStore(str(tmp_path / "gw2bot.db"))
    yield store
    store.close()


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def fake_bot(store: EventStore, channel: FakeChannel) -> Any:
    return cast(Any, FakeBot(store, channel))


def make_complete_draft() -> EventDraft:
    return EventDraft(
        leader_discord_id=42,
        category=EventCategory.FRACTAL,
        title="Kitty Cleanup",
        description="Bring food.",
        channel_id=1234,
        start_time=datetime(2107, 1, 30, 20, 0, tzinfo=UTC),
        duration_minutes=90,
    )


class TestPostEventButton:
    async def test_failed_post_cleans_up_and_a_retry_posts_once(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        draft = make_complete_draft()
        view = EventConfirmView(fake_bot, draft)
        channel.send_error = forbidden_error(50001)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.followup.send = AsyncMock()
        interaction.edit_original_response = AsyncMock()

        await view.post_event.callback(interaction)

        assert not draft.posted
        assert channel.sent == []
        assert store.get_unposted_occurrences() == []
        interaction.followup.send.assert_awaited_once()
        assert interaction.followup.send.await_args is not None
        assert (
            "could not be posted"
            in interaction.followup.send.await_args.args[0]
        )
        # The Post event button must be restored so the user can retry
        # from the same preview rather than restarting /event new.
        interaction.edit_original_response.assert_awaited_once_with(view=view)

        retry_interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        retry_interaction.followup.send = AsyncMock()
        retry_interaction.edit_original_response = AsyncMock()

        await view.post_event.callback(retry_interaction)

        assert draft.posted
        assert len(channel.sent) == 1
        posted = store.get_posted_unfinished_occurrences()
        assert len(posted) == 1
        events = {
            store.get_event(occurrence.event_id).event_id  # type: ignore[union-attr]
            for occurrence in posted
        }
        assert len(events) == 1

    async def test_a_cancellation_racing_the_first_post_leaves_nothing_behind(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        draft = replace(
            make_complete_draft(),
            repeat_frequency=RepeatFrequency.DAILY,
        )
        view = EventConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.followup.send = AsyncMock()
        interaction.edit_original_response = AsyncMock()

        async def cancel_while_the_message_is_in_flight(**kwargs: Any) -> Any:
            # Only the first send is interrupted; the cancellation's own post
            # of the successor goes through the plain channel.
            channel.send = plain_send  # type: ignore[method-assign]
            message = await plain_send(**kwargs)
            # Another commander cancels this run inside the send: the row is
            # gone and its successor is seeded and posted in its place.
            pending = store.get_unposted_occurrences()[0]
            stored_event = store.get_event(pending.event_id)
            assert stored_event is not None
            await cancel_occurrence(fake_bot, stored_event, pending)
            return message

        plain_send = channel.send
        # type: ignore[method-assign] - the fake channel stands in for Discord.
        channel.send = cancel_while_the_message_is_in_flight  # type: ignore

        await view.post_event.callback(interaction)

        # The event is torn down, and every message that went out with it -
        # the one this post sent and the successor the cancellation posted -
        # is deleted rather than left in the channel with no rows behind it.
        assert not draft.posted
        assert store.get_event_occurrences(1) == []
        # The post whose row vanished is deleted by the send that made it, and
        # the successor's post - which does have stored ids - through the
        # channel, so both are gone.
        channel.sent[0]["message"].delete.assert_awaited_once()
        channel.partial_message.delete.assert_awaited()
        assert interaction.followup.send.await_args is not None
        assert (
            "could not be posted"
            in interaction.followup.send.await_args.args[0]
        )

    async def test_successful_post_stores_and_sends_once(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        draft = make_complete_draft()
        view = EventConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.followup.send = AsyncMock()

        await view.post_event.callback(interaction)

        assert draft.posted
        assert len(channel.sent) == 1
        assert len(store.get_posted_unfinished_occurrences()) == 1

    async def test_failed_save_restores_post_controls(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        draft = make_complete_draft()
        view = EventConfirmView(fake_bot, draft)
        store.create_event = MagicMock(  # type: ignore[method-assign]
            side_effect=SQLAlchemyError("boom")
        )
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.followup.send = AsyncMock()
        interaction.edit_original_response = AsyncMock()

        await view.post_event.callback(interaction)

        assert not draft.posted
        interaction.followup.send.assert_awaited_once()
        assert interaction.followup.send.await_args is not None
        assert (
            "could not be saved"
            in interaction.followup.send.await_args.args[0]
        )
        interaction.edit_original_response.assert_awaited_once_with(view=view)

    async def test_post_rejected_when_creator_role_revoked(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        draft = make_complete_draft()
        view = EventConfirmView(fake_bot, draft)
        # The preview was opened earlier, but the creator role is gone now.
        interaction = make_interaction(message=ephemeral_message())

        await view.post_event.callback(interaction)

        assert not draft.posted
        assert channel.sent == []
        assert store.get_unposted_occurrences() == []
        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args is not None
        assert (
            "required role"
            in interaction.response.send_message.await_args.args[0]
        )


class TestRolePickSelect:
    def _make_role_flow(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> SignupFlow:
        event = store.create_event(
            category=EventCategory.RAID,
            title="Full quickness",
            description="Bring food.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=datetime(2107, 1, 30, 20, 0, tzinfo=UTC),
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        # Fill both quickness slots so Quickness roles are full while
        # Alacrity and plain DPS remain open.
        for user_id, role in (
            (1, EventRole.QUICKNESS_HEAL),
            (2, EventRole.QUICKNESS_DPS),
        ):
            store.add_signup(
                occurrence_id=occurrence.occurrence_id,
                discord_user_id=user_id,
                role=role,
                assigned_role=role,
                flex_roles=(),
                waitlisted=False,
            )
        return SignupFlow(fake_bot, event, occurrence, 42)

    def test_offers_full_roles_alongside_open_roles(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        flow = self._make_role_flow(fake_bot, store)

        select = RolePickSelect(flow)
        labels = {option.value: option.label for option in select.options}

        # Every role is selectable so a full preferred role can fall back to
        # an open flex role (or waitlist for a specific role).
        assert set(labels) == {role.value for role in EventRole}
        assert labels[EventRole.QUICKNESS_HEAL.value] == "Quickness Heal (full)"
        assert labels[EventRole.QUICKNESS_DPS.value] == "Quickness DPS (full)"
        assert labels[EventRole.ALACRITY_HEAL.value] == "Alacrity Heal"
        assert labels[EventRole.DPS.value] == "Just DPS"

    def test_dungeon_pickers_exclude_healer_roles(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event = store.create_event(
            category=EventCategory.DUNGEON,
            title="Dungeon",
            description="Bring damage.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=datetime(2107, 1, 30, 20, 0, tzinfo=UTC),
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        flow = SignupFlow(fake_bot, event, occurrence, 42)
        dps_roles = {
            EventRole.DPS.value,
            EventRole.QUICKNESS_DPS.value,
            EventRole.ALACRITY_DPS.value,
        }

        role_values = {option.value for option in RolePickSelect(flow).options}
        assert role_values == dps_roles

        flow.role = EventRole.DPS
        flex_values = {
            option.value for option in FlexRolesSelect(flow).options
        }
        assert flex_values == dps_roles - {EventRole.DPS.value}

        add_values = {
            option.value
            for option in AddSignupsRoleSelect(event, []).options
        }
        assert add_values == dps_roles

    def test_boon_seat_held_by_a_flexer_is_not_labelled_full(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event = store.create_event(
            category=EventCategory.RAID,
            title="Flexible quickness",
            description="Bring food.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=datetime(2107, 1, 30, 20, 0, tzinfo=UTC),
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        # Both quickness seats are occupied, but one holder can move to plain
        # DPS, so a rigid quickness signup would still be admitted.
        for user_id, role, flex_roles in (
            (1, EventRole.QUICKNESS_HEAL, ()),
            (2, EventRole.QUICKNESS_DPS, (EventRole.DPS,)),
        ):
            store.add_signup(
                occurrence_id=occurrence.occurrence_id,
                discord_user_id=user_id,
                role=role,
                assigned_role=role,
                flex_roles=flex_roles,
                waitlisted=False,
            )
        flow = SignupFlow(fake_bot, event, occurrence, 42)

        select = RolePickSelect(flow)
        labels = {option.value: option.label for option in select.options}

        assert labels[EventRole.QUICKNESS_DPS.value] == "Quickness DPS"
        assert labels[EventRole.QUICKNESS_HEAL.value] == "Quickness Heal"

    def test_labels_all_roles_as_waitlist_when_roster_full(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event = store.create_event(
            category=EventCategory.FRACTAL,
            title="Packed fractal",
            description="Bring food.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=datetime(2107, 1, 30, 20, 0, tzinfo=UTC),
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        # Fractal capacity is 1 healer and 4 dps; fill every slot.
        assignments = [
            EventRole.QUICKNESS_HEAL,
            EventRole.ALACRITY_DPS,
            EventRole.DPS,
            EventRole.DPS,
            EventRole.DPS,
        ]
        for user_id, role in enumerate(assignments, start=1):
            store.add_signup(
                occurrence_id=occurrence.occurrence_id,
                discord_user_id=user_id,
                role=role,
                assigned_role=role,
                flex_roles=(),
                waitlisted=False,
            )
        flow = SignupFlow(fake_bot, event, occurrence, 99)

        select = RolePickSelect(flow)

        assert {option.value for option in select.options} == {
            role.value for role in EventRole
        }
        assert all(
            option.label.endswith("(waitlist)") for option in select.options
        )


class TestSignOutFlow:
    def _make_ended_occurrence(self, store: EventStore) -> Any:
        past_start = datetime.now(UTC) - timedelta(hours=3)
        event = store.create_event(
            category=EventCategory.WVW,
            title="Border skirmish",
            description="Bring siege.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=past_start,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        return event, occurrence

    async def test_confirm_after_end_keeps_roster(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self._make_ended_occurrence(store)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=42,
            role=None,
            assigned_role=None,
            flex_roles=(),
            waitlisted=False,
        )
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=99,
            role=None,
            assigned_role=None,
            flex_roles=(),
            waitlisted=True,
        )
        view = SignOutConfirmView(fake_bot, event, occurrence)
        interaction = make_interaction(message=ephemeral_message())

        await view.remove_me.callback(interaction)

        # The historical roster must be untouched: no removal of the active
        # participant and no promotion of the waitlisted one.
        assert store.get_signup(occurrence.occurrence_id, 42) is not None
        waitlisted = store.get_signup(occurrence.occurrence_id, 99)
        assert waitlisted is not None
        assert waitlisted.waitlisted
        interaction.response.edit_message.assert_awaited_once()
        assert interaction.response.edit_message.await_args is not None
        assert (
            "already ended"
            in interaction.response.edit_message.await_args.kwargs["content"]
        )

    async def test_button_after_end_does_not_open_confirmation(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self._make_ended_occurrence(store)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=42,
            role=None,
            assigned_role=None,
            flex_roles=(),
            waitlisted=False,
        )
        button = EventSignOutButton(occurrence.occurrence_id)
        interaction = make_interaction(message=ephemeral_message())
        interaction.client = fake_bot

        await button.callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args is not None
        assert (
            "already ended"
            in interaction.response.send_message.await_args.args[0]
        )

    def _make_live_occurrence(
        self,
        store: EventStore,
        repeat_frequency: RepeatFrequency = RepeatFrequency.DAILY,
    ) -> Any:
        event = store.create_event(
            category=EventCategory.WVW,
            title="Border skirmish",
            description="Bring siege.",
            channel_id=1234,
            leader_discord_id=7,
            start_time=FAR_FUTURE,
            duration_minutes=90,
            repeat_frequency=repeat_frequency,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        store.set_occurrence_message(occurrence.occurrence_id, 1234, 555, 777)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=42,
            role=None,
            assigned_role=None,
            flex_roles=(),
            waitlisted=False,
        )
        return event, occurrence

    async def _sign_out(self, fake_bot: Any, event: Any, occurrence: Any) -> Any:
        view = SignOutConfirmView(fake_bot, event, occurrence)
        interaction = make_interaction(message=ephemeral_message())
        interaction.edit_original_response = AsyncMock()
        await view.remove_me.callback(interaction)
        return interaction

    async def test_sign_out_offers_to_disable_auto_signup(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # Signing out only clears this occurrence, so a member with automatic
        # sign-up left on would be seated again on the next one.
        event, occurrence = self._make_live_occurrence(store)
        store.set_auto_signup(
            event.event_id,
            42,
            AutoSignupChoice.YES,
            None,
            (),
        )

        interaction = await self._sign_out(fake_bot, event, occurrence)

        assert store.get_signup(occurrence.occurrence_id, 42) is None
        kwargs = interaction.edit_original_response.await_args.kwargs
        assert isinstance(kwargs["view"], DisableAutoSignupView)
        assert "You were removed from the event." in kwargs["content"]
        assert "Automatic sign-up is still on" in kwargs["content"]

    async def test_sign_out_does_not_prompt_without_auto_signup(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self._make_live_occurrence(store)

        interaction = await self._sign_out(fake_bot, event, occurrence)

        kwargs = interaction.edit_original_response.await_args.kwargs
        assert kwargs["view"] is None
        assert "Automatic sign-up" not in kwargs["content"]

    @pytest.mark.parametrize(
        "choice",
        [AutoSignupChoice.NO, AutoSignupChoice.NEVER_ASK],
    )
    async def test_sign_out_does_not_prompt_when_auto_signup_is_off(
        self,
        fake_bot: Any,
        store: EventStore,
        choice: AutoSignupChoice,
    ) -> None:
        # Both choices already leave automatic sign-up off, and NEVER_ASK is an
        # explicit request to stop being asked about this event.
        event, occurrence = self._make_live_occurrence(store)
        store.set_auto_signup(event.event_id, 42, choice, None, ())

        interaction = await self._sign_out(fake_bot, event, occurrence)

        kwargs = interaction.edit_original_response.await_args.kwargs
        assert kwargs["view"] is None

    async def test_sign_out_does_not_prompt_for_a_one_off_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # A stored choice on a non-repeating event is inert: there is no next
        # occurrence to be signed up for.
        event, occurrence = self._make_live_occurrence(
            store,
            RepeatFrequency.NONE,
        )
        store.set_auto_signup(
            event.event_id,
            42,
            AutoSignupChoice.YES,
            None,
            (),
        )

        interaction = await self._sign_out(fake_bot, event, occurrence)

        kwargs = interaction.edit_original_response.await_args.kwargs
        assert kwargs["view"] is None

    async def test_sign_out_does_not_prompt_a_member_who_was_not_signed_up(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self._make_live_occurrence(store)
        store.remove_signup(occurrence.occurrence_id, 42)
        store.set_auto_signup(
            event.event_id,
            42,
            AutoSignupChoice.YES,
            None,
            (),
        )

        interaction = await self._sign_out(fake_bot, event, occurrence)

        kwargs = interaction.edit_original_response.await_args.kwargs
        assert kwargs["view"] is None
        assert "not signed up" in kwargs["content"]

    async def test_disable_button_turns_auto_signup_off(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self._make_live_occurrence(store)
        store.set_auto_signup(
            event.event_id,
            42,
            AutoSignupChoice.YES,
            None,
            (),
        )
        view = DisableAutoSignupView(fake_bot, event, occurrence, 42)
        interaction = make_interaction(message=ephemeral_message())

        await view.disable_auto.callback(interaction)

        stored = store.get_auto_signup(event.event_id, 42)
        assert stored is not None
        assert stored.choice is AutoSignupChoice.NO
        content = interaction.response.edit_message.await_args.kwargs["content"]
        assert "Automatic sign-up is off" in content

    async def test_disable_button_withdraws_a_seat_seeded_meanwhile(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # This prompt can sit open until the scheduler seeds the next
        # occurrence, and the sign-out that opened it can seed one itself by
        # crossing this occurrence's end. Storing the choice alone would
        # confirm automatic sign-up is off while leaving the member seated.
        event, occurrence = self._make_live_occurrence(store)
        store.set_auto_signup(
            event.event_id,
            42,
            AutoSignupChoice.YES,
            None,
            (),
        )
        following = store.create_occurrence(
            event.event_id,
            occurrence.start_time + timedelta(days=1),
        )
        store.add_signup(
            occurrence_id=following.occurrence_id,
            discord_user_id=42,
            role=None,
            assigned_role=None,
            flex_roles=(),
            waitlisted=False,
        )
        view = DisableAutoSignupView(fake_bot, event, occurrence, 42)
        interaction = make_interaction(message=ephemeral_message())

        await view.disable_auto.callback(interaction)

        assert store.get_signup(following.occurrence_id, 42) is None
        content = interaction.response.edit_message.await_args.kwargs["content"]
        assert "Automatic sign-up is off" in content
        assert "taken off it too" in content

    async def test_disable_button_names_a_seat_it_will_not_withdraw(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self._make_live_occurrence(store)
        store.set_auto_signup(
            event.event_id,
            42,
            AutoSignupChoice.YES,
            None,
            (),
        )
        following = store.create_occurrence(
            event.event_id,
            occurrence.start_time + timedelta(days=1),
        )
        store.set_occurrence_message(following.occurrence_id, 1234, 556, 778)
        store.add_signup(
            occurrence_id=following.occurrence_id,
            discord_user_id=42,
            role=None,
            assigned_role=None,
            flex_roles=(),
            waitlisted=False,
        )
        view = DisableAutoSignupView(fake_bot, event, occurrence, 42)
        interaction = make_interaction(message=ephemeral_message())

        await view.disable_auto.callback(interaction)

        assert store.get_signup(following.occurrence_id, 42) is not None
        content = interaction.response.edit_message.await_args.kwargs["content"]
        assert "still signed up for the next occurrence" in content

    async def test_keep_button_leaves_auto_signup_on(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self._make_live_occurrence(store)
        store.set_auto_signup(
            event.event_id,
            42,
            AutoSignupChoice.YES,
            None,
            (),
        )
        view = DisableAutoSignupView(fake_bot, event, occurrence, 42)
        interaction = make_interaction(message=ephemeral_message())

        await view.keep_auto.callback(interaction)

        stored = store.get_auto_signup(event.event_id, 42)
        assert stored is not None
        assert stored.choice is AutoSignupChoice.YES
        content = interaction.response.edit_message.await_args.kwargs["content"]
        assert "stays on" in content


class TestAutoSignupPrompt:
    async def make_flow(
        self,
        fake_bot: Any,
        store: EventStore,
        user_id: int,
    ) -> SignupFlow:
        event = store.create_event(
            category=EventCategory.FRACTAL,
            title="Kitty Cleanup",
            description="Bring food.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=datetime(2107, 1, 30, 20, 0, tzinfo=UTC),
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.DAILY,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(
            event.event_id,
            event.start_time,
        )
        occurrence = await post_occurrence(
            fake_bot,
            event,
            occurrence,
            datetime(2107, 1, 30, 10, 0, tzinfo=UTC),
        )
        flow = SignupFlow(fake_bot, event, occurrence, user_id)
        flow.role = EventRole.DPS
        return flow

    def make_flow_interaction(self) -> Any:
        interaction = make_interaction(message=ephemeral_message())
        interaction.response.is_done = MagicMock(return_value=False)
        interaction.edit_original_response = AsyncMock()
        return interaction

    async def finalize_and_get_kwargs(
        self,
        flow: SignupFlow,
    ) -> dict[str, Any]:
        interaction = self.make_flow_interaction()
        await flow.finalize(interaction)
        interaction.edit_original_response.assert_awaited_once()
        await_args = interaction.edit_original_response.await_args
        assert await_args is not None
        return await_args.kwargs

    async def test_prompts_when_no_choice_is_stored(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        flow = await self.make_flow(fake_bot, store, 21)

        kwargs = await self.finalize_and_get_kwargs(flow)

        assert "automatically" in kwargs["content"]
        assert isinstance(kwargs["view"], AutoSignupChoiceView)

    async def test_finalize_normalizes_a_role_against_a_changed_category(
        self,
        fake_bot: Any,
        store: EventStore,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        flow = await self.make_flow(fake_bot, store, 21)
        flow.role = EventRole.QUICKNESS_HEAL
        event = flow.event
        store.update_event(
            event_id=event.event_id,
            category=EventCategory.DUNGEON,
            title=event.title,
            description=event.description,
            channel_id=event.channel_id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
        )
        secret = "Normalized signup roles after"
        root_logger = logging.getLogger()
        app_logger = logging.getLogger("gw2bot")
        previous_handlers = list(root_logger.handlers)
        previous_root_level = root_logger.level
        previous_app_level = app_logger.level
        try:
            configure_logging(True, SecretRegistry((secret,)))

            await self.finalize_and_get_kwargs(flow)

            console = capsys.readouterr().err
        finally:
            for handler in list(root_logger.handlers):
                root_logger.removeHandler(handler)
                handler.close()
            for handler in previous_handlers:
                root_logger.addHandler(handler)
            root_logger.setLevel(previous_root_level)
            app_logger.setLevel(previous_app_level)

        signup = store.get_signup(flow.occurrence.occurrence_id, 21)
        assert signup is not None
        assert not signup.waitlisted
        assert signup.role is EventRole.DPS
        assert signup.assigned_role is EventRole.DPS
        assert flow.event.category is EventCategory.DUNGEON
        assert "[REDACTED]" in console
        assert secret not in console

    async def test_prompts_again_after_a_plain_no(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        flow = await self.make_flow(fake_bot, store, 21)
        store.set_auto_signup(
            flow.event.event_id,
            21,
            AutoSignupChoice.NO,
            None,
            (),
        )

        kwargs = await self.finalize_and_get_kwargs(flow)

        assert "automatically" in kwargs["content"]
        assert isinstance(kwargs["view"], AutoSignupChoiceView)

    async def test_never_ask_again_suppresses_the_prompt(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        flow = await self.make_flow(fake_bot, store, 21)
        store.set_auto_signup(
            flow.event.event_id,
            21,
            AutoSignupChoice.NEVER_ASK,
            None,
            (),
        )

        kwargs = await self.finalize_and_get_kwargs(flow)

        assert "automatically" not in kwargs["content"]
        assert kwargs["view"] is None

    async def test_yes_suppresses_the_prompt(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        flow = await self.make_flow(fake_bot, store, 21)
        store.set_auto_signup(
            flow.event.event_id,
            21,
            AutoSignupChoice.YES,
            EventRole.DPS,
            (),
        )

        kwargs = await self.finalize_and_get_kwargs(flow)

        assert "automatically" not in kwargs["content"]
        assert kwargs["view"] is None


class TestRememberedRolesPerEvent:
    # Remembered roles are scoped to one event, so a memory earned on one
    # event neither seats nor silences the member on any other.

    def make_role_event(
        self,
        store: EventStore,
        title: str,
        *,
        repeat_frequency: RepeatFrequency = RepeatFrequency.DAILY,
    ) -> Any:
        event = store.create_event(
            category=EventCategory.FRACTAL,
            title=title,
            description="Bring food.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=FAR_FUTURE,
            duration_minutes=90,
            repeat_frequency=repeat_frequency,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        store.set_occurrence_message(occurrence.occurrence_id, 1234, 555, 777)
        return event, occurrence

    def make_flow_interaction(self) -> Any:
        interaction = make_interaction(message=ephemeral_message())
        interaction.response.is_done = MagicMock(return_value=False)
        interaction.edit_original_response = AsyncMock()
        return interaction

    async def test_an_unfamiliar_event_asks_for_roles(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        remembered, _ = self.make_role_event(store, "Remembered")
        _, fresh = self.make_role_event(store, "Never joined")
        store.set_signup_preference(
            remembered.event_id,
            42,
            EventRole.ALACRITY_HEAL,
            (),
            PreferenceMode.REMEMBER,
        )
        interaction = make_interaction()

        await start_signup_flow(
            fake_bot,
            interaction,
            fresh.occurrence_id,
        )

        await_args = interaction.response.send_message.await_args
        assert await_args is not None
        assert "Pick your role" in await_args.args[0]
        assert isinstance(await_args.kwargs["view"], RolePickView)
        # Nothing was seated: the flow is waiting on the role pickers.
        assert store.get_signup(fresh.occurrence_id, 42) is None

    async def test_remembered_roles_still_seat_their_own_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_role_event(store, "Remembered")
        store.set_signup_preference(
            event.event_id,
            42,
            EventRole.ALACRITY_HEAL,
            (EventRole.DPS,),
            PreferenceMode.REMEMBER,
        )
        interaction = make_interaction()
        interaction.response.is_done = MagicMock(return_value=False)
        interaction.edit_original_response = AsyncMock()

        await start_signup_flow(
            fake_bot,
            interaction,
            occurrence.occurrence_id,
        )

        interaction.response.send_message.assert_not_awaited()
        signup = store.get_signup(occurrence.occurrence_id, 42)
        assert signup is not None
        assert signup.role is EventRole.ALACRITY_HEAL
        assert signup.flex_roles == (EventRole.DPS,)

    async def test_invalid_remembered_role_is_normalized_after_category_change(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_role_event(store, "Changed category")
        event = store.update_event(
            event_id=event.event_id,
            category=EventCategory.DUNGEON,
            title=event.title,
            description=event.description,
            channel_id=event.channel_id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
        )
        store.set_signup_preference(
            event.event_id,
            42,
            EventRole.ALACRITY_HEAL,
            (),
            PreferenceMode.REMEMBER,
        )
        interaction = self.make_flow_interaction()

        await start_signup_flow(fake_bot, interaction, occurrence.occurrence_id)

        signup = store.get_signup(occurrence.occurrence_id, 42)
        assert signup is not None
        assert not signup.waitlisted
        assert signup.role is EventRole.DPS
        assert signup.assigned_role is EventRole.DPS
        preference = store.get_signup_preference(event.event_id, 42)
        assert preference is not None
        assert preference.role is EventRole.DPS
        assert preference.flex_roles == ()

    async def test_remembering_stores_the_choice_against_one_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_role_event(store, "Remembered")
        other, _ = self.make_role_event(store, "Never joined")
        flow = SignupFlow(fake_bot, event, occurrence, 42)
        flow.role = EventRole.QUICKNESS_DPS

        await RememberChoiceView(flow).remember_yes.callback(
            self.make_flow_interaction()
        )

        preference = store.get_signup_preference(event.event_id, 42)
        assert preference is not None
        assert preference.mode is PreferenceMode.REMEMBER
        assert preference.role is EventRole.QUICKNESS_DPS
        assert store.get_signup_preference(other.event_id, 42) is None

    async def test_never_ask_again_covers_only_its_own_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_role_event(store, "Quietened")
        other, fresh = self.make_role_event(store, "Never joined")
        flow = SignupFlow(fake_bot, event, occurrence, 42)
        flow.role = EventRole.DPS
        await RememberChoiceView(flow).remember_never.callback(
            self.make_flow_interaction()
        )

        quietened = store.get_signup_preference(event.event_id, 42)
        assert quietened is not None
        assert quietened.mode is PreferenceMode.NEVER_ASK

        # The other event has never asked, so it asks now.
        interaction = make_interaction()
        await start_signup_flow(
            fake_bot,
            interaction,
            fresh.occurrence_id,
        )
        await_args = interaction.response.send_message.await_args
        assert await_args is not None
        assert isinstance(await_args.kwargs["view"], RolePickView)
        assert store.get_signup_preference(other.event_id, 42) is None

    async def test_a_one_off_event_never_asks_to_remember(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_role_event(
            store,
            "One-off",
            repeat_frequency=RepeatFrequency.NONE,
        )
        flow = SignupFlow(fake_bot, event, occurrence, 42)
        flow.role = EventRole.DPS
        interaction = self.make_flow_interaction()

        await flow.continue_after_roles(interaction)

        # There is no second occurrence for a memory to serve, so the flow
        # signs the member up instead of asking - as automatic sign-up does.
        kwargs = interaction.edit_original_response.await_args.kwargs
        assert kwargs["view"] is None
        assert store.get_signup(occurrence.occurrence_id, 42) is not None

    async def test_a_memory_left_by_a_dropped_repeat_is_inert(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # A leader can turn a repeat off, leaving memories behind that the
        # settings panel no longer offers to reset. Those must stop seating
        # people rather than silently outliving the repeat.
        event, occurrence = self.make_role_event(
            store,
            "No longer repeating",
            repeat_frequency=RepeatFrequency.NONE,
        )
        store.set_signup_preference(
            event.event_id,
            42,
            EventRole.ALACRITY_HEAL,
            (),
            PreferenceMode.REMEMBER,
        )
        interaction = make_interaction()

        await start_signup_flow(
            fake_bot,
            interaction,
            occurrence.occurrence_id,
        )

        await_args = interaction.response.send_message.await_args
        assert await_args is not None
        assert isinstance(await_args.kwargs["view"], RolePickView)
        assert store.get_signup(occurrence.occurrence_id, 42) is None

    def test_a_one_off_event_offers_no_role_memory_settings(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_role_event(
            store,
            "One-off",
            repeat_frequency=RepeatFrequency.NONE,
        )
        view = SignupSettingsView(fake_bot, event, occurrence, 42)
        labels = [
            item.label
            for item in view.children
            if isinstance(item, discord.ui.Button)
        ]

        assert "Reset role memory for this event" not in labels
        description = _describe_signup_settings(fake_bot, event, 42)
        assert "does not repeat" in description
        assert "role memory" in description
        assert "ask every time" not in description

    async def test_editing_a_one_off_signup_skips_the_memory_prompt(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_role_event(
            store,
            "One-off",
            repeat_frequency=RepeatFrequency.NONE,
        )
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=42,
            role=EventRole.QUICKNESS_DPS,
            assigned_role=EventRole.QUICKNESS_DPS,
            flex_roles=(),
            waitlisted=False,
        )
        store.set_signup_preference(
            event.event_id,
            42,
            EventRole.QUICKNESS_DPS,
            (),
            PreferenceMode.REMEMBER,
        )
        flow = EditSignupFlow(fake_bot, event, occurrence, 42)
        flow.role = EventRole.ALACRITY_DPS
        interaction = self.make_flow_interaction()

        await flow.continue_after_roles(interaction)

        kwargs = interaction.edit_original_response.await_args.kwargs
        assert "remembered roles" not in kwargs["content"]
        assert kwargs["view"] is None


class TestEditSignupFlow:
    def make_signed_up_event(
        self,
        store: EventStore,
        *,
        user_id: int = 42,
        role: EventRole = EventRole.QUICKNESS_DPS,
        flex_roles: tuple[EventRole, ...] = (),
        repeat_frequency: RepeatFrequency = RepeatFrequency.NONE,
    ) -> Any:
        event, occurrence = make_posted_edit_event(
            store,
            repeat_frequency=repeat_frequency,
        )
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=user_id,
            role=role,
            assigned_role=role,
            flex_roles=flex_roles,
            waitlisted=False,
        )
        return event, occurrence

    def settings_buttons(
        self,
        view: SignupSettingsView,
    ) -> dict[str, discord.ui.Button[Any]]:
        return {
            item.label: item
            for item in view.children
            if isinstance(item, discord.ui.Button) and item.label is not None
        }

    def make_flow_interaction(self) -> Any:
        interaction = make_interaction(message=ephemeral_message())
        interaction.response.is_done = MagicMock(return_value=False)
        interaction.edit_original_response = AsyncMock()
        return interaction

    def test_settings_offer_edit_only_when_signed_up(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = make_posted_edit_event(store)

        without_signup = SignupSettingsView(fake_bot, event, occurrence, 42)
        assert "Edit my signup" not in self.settings_buttons(without_signup)

        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=42,
            role=EventRole.DPS,
            assigned_role=EventRole.DPS,
            flex_roles=(),
            waitlisted=False,
        )
        with_signup = SignupSettingsView(fake_bot, event, occurrence, 42)
        assert "Edit my signup" in self.settings_buttons(with_signup)

    def test_settings_never_offer_edit_for_role_less_events(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event = store.create_event(
            category=EventCategory.WVW,
            title="Border brawl",
            description="Bring siege.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=FAR_FUTURE,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=42,
            role=None,
            assigned_role=None,
            flex_roles=(),
            waitlisted=False,
        )

        view = SignupSettingsView(fake_bot, event, occurrence, 42)

        # A role-less roster has nothing to edit.
        assert "Edit my signup" not in self.settings_buttons(view)

    async def test_edit_button_opens_the_role_picker(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_signed_up_event(store)
        view = SignupSettingsView(fake_bot, event, occurrence, 42)
        button = self.settings_buttons(view)["Edit my signup"]
        interaction = make_interaction(message=ephemeral_message())

        await button.callback(interaction)

        kwargs = interaction.response.edit_message.await_args.kwargs
        assert "Pick your new role" in kwargs["content"]
        assert isinstance(kwargs["view"], RolePickView)

    async def test_edit_button_reports_a_signed_out_member(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_signed_up_event(store)
        view = SignupSettingsView(fake_bot, event, occurrence, 42)
        # The settings message sat open while the member signed out.
        store.remove_signup(occurrence.occurrence_id, 42)
        button = self.settings_buttons(view)["Edit my signup"]
        interaction = make_interaction(message=ephemeral_message())

        await button.callback(interaction)

        kwargs = interaction.response.edit_message.await_args.kwargs
        assert "no longer signed up" in kwargs["content"]
        assert kwargs["view"] is None

    async def test_edit_flow_applies_without_extra_prompts(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_signed_up_event(store)
        original = store.get_signup(occurrence.occurrence_id, 42)
        assert original is not None
        flow = EditSignupFlow(fake_bot, event, occurrence, 42)
        flow.role = EventRole.ALACRITY_DPS
        flow.flex_roles = (EventRole.DPS,)
        interaction = self.make_flow_interaction()

        await flow.continue_after_roles(interaction)

        # Straight to applying: no remember-my-roles or auto-signup prompts.
        first = interaction.response.edit_message.await_args
        assert first is not None
        assert "Updating your signup" in first.kwargs["content"]
        kwargs = interaction.edit_original_response.await_args.kwargs
        assert "Your signup was updated" in kwargs["content"]
        assert kwargs["view"] is None
        updated = store.get_signup(occurrence.occurrence_id, 42)
        assert updated is not None
        assert updated.role is EventRole.ALACRITY_DPS
        assert updated.flex_roles == (EventRole.DPS,)
        assert updated.assigned_role is EventRole.ALACRITY_DPS
        assert not updated.waitlisted
        assert updated.signed_up_at == original.signed_up_at

    async def test_edit_that_would_waitlist_confirms_then_applies(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = make_posted_edit_event(store)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=1,
            role=EventRole.QUICKNESS_HEAL,
            assigned_role=EventRole.QUICKNESS_HEAL,
            flex_roles=(),
            waitlisted=False,
        )
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=42,
            role=EventRole.DPS,
            assigned_role=EventRole.DPS,
            flex_roles=(),
            waitlisted=False,
        )
        flow = EditSignupFlow(fake_bot, event, occurrence, 42)
        flow.role = EventRole.QUICKNESS_HEAL
        interaction = self.make_flow_interaction()

        await flow.continue_after_roles(interaction)

        kwargs = interaction.edit_original_response.await_args.kwargs
        assert "waitlist" in kwargs["content"]
        confirm = kwargs["view"]
        assert isinstance(confirm, EditWaitlistConfirmView)
        # Nothing is applied until the member consents.
        pending = store.get_signup(occurrence.occurrence_id, 42)
        assert pending is not None
        assert pending.role is EventRole.DPS
        assert not pending.waitlisted

        second = self.make_flow_interaction()
        await confirm.apply_anyway.callback(second)

        moved = store.get_signup(occurrence.occurrence_id, 42)
        assert moved is not None
        assert moved.waitlisted
        assert moved.role is EventRole.QUICKNESS_HEAL
        assert moved.assigned_role is None
        summary = second.edit_original_response.await_args.kwargs
        assert "waitlist" in summary["content"]

    def test_edit_flow_labels_ignore_the_editors_own_seat(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_signed_up_event(
            store,
            role=EventRole.QUICKNESS_HEAL,
        )

        # For anyone else, the seated healer makes both heal roles read full.
        other = SignupFlow(fake_bot, event, occurrence, 99)
        other_labels = {
            option.value: option.label
            for option in RolePickSelect(other).options
        }
        assert (
            other_labels[EventRole.ALACRITY_HEAL.value]
            == "Alacrity Heal (full)"
        )

        # The editor is re-picking their own seat, so it must not count
        # against them: every heal role is freely selectable.
        editing = EditSignupFlow(fake_bot, event, occurrence, 42)
        edit_labels = {
            option.value: option.label
            for option in RolePickSelect(editing).options
        }
        assert edit_labels[EventRole.ALACRITY_HEAL.value] == "Alacrity Heal"
        assert edit_labels[EventRole.QUICKNESS_HEAL.value] == "Quickness Heal"

    async def test_edit_button_blocks_when_out_of_tokens(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_signed_up_event(store)
        store.set_signup_edit_tokens(
            occurrence.occurrence_id,
            42,
            0.0,
            datetime.now(UTC),
        )
        view = SignupSettingsView(fake_bot, event, occurrence, 42)
        button = self.settings_buttons(view)["Edit my signup"]
        interaction = make_interaction(message=ephemeral_message())

        await button.callback(interaction)

        kwargs = interaction.response.edit_message.await_args.kwargs
        assert "used all your signup edits" in kwargs["content"]
        assert kwargs["view"] is None

    async def test_edit_offers_to_update_remembered_roles(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_signed_up_event(
            store,
            repeat_frequency=RepeatFrequency.DAILY,
        )
        store.set_signup_preference(
            event.event_id,
            42,
            EventRole.QUICKNESS_DPS,
            (),
            PreferenceMode.REMEMBER,
        )
        flow = EditSignupFlow(fake_bot, event, occurrence, 42)
        flow.role = EventRole.ALACRITY_DPS
        interaction = self.make_flow_interaction()

        await flow.continue_after_roles(interaction)

        kwargs = interaction.edit_original_response.await_args.kwargs
        assert "remembered roles" in kwargs["content"]
        prompt = kwargs["view"]
        assert isinstance(prompt, UpdateRememberedRolesView)

        second = make_interaction(message=ephemeral_message())
        await prompt.update.callback(second)

        preference = store.get_signup_preference(event.event_id, 42)
        assert preference is not None
        assert preference.mode is PreferenceMode.REMEMBER
        assert preference.role is EventRole.ALACRITY_DPS
        assert (
            "updated"
            in second.response.edit_message.await_args.kwargs["content"]
        )

    async def test_remembered_roles_can_be_kept_as_they_were(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_signed_up_event(
            store,
            repeat_frequency=RepeatFrequency.DAILY,
        )
        store.set_signup_preference(
            event.event_id,
            42,
            EventRole.QUICKNESS_DPS,
            (),
            PreferenceMode.REMEMBER,
        )
        flow = EditSignupFlow(fake_bot, event, occurrence, 42)
        flow.role = EventRole.ALACRITY_DPS
        interaction = self.make_flow_interaction()
        await flow.continue_after_roles(interaction)
        prompt = interaction.edit_original_response.await_args.kwargs["view"]
        assert isinstance(prompt, UpdateRememberedRolesView)

        second = make_interaction(message=ephemeral_message())
        await prompt.keep.callback(second)

        preference = store.get_signup_preference(event.event_id, 42)
        assert preference is not None
        assert preference.role is EventRole.QUICKNESS_DPS
        assert (
            "left unchanged"
            in second.response.edit_message.await_args.kwargs["content"]
        )

    async def test_no_remember_prompt_when_the_selection_matches(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_signed_up_event(
            store,
            repeat_frequency=RepeatFrequency.DAILY,
        )
        store.set_signup_preference(
            event.event_id,
            42,
            EventRole.ALACRITY_DPS,
            (EventRole.DPS,),
            PreferenceMode.REMEMBER,
        )
        flow = EditSignupFlow(fake_bot, event, occurrence, 42)
        flow.role = EventRole.ALACRITY_DPS
        flow.flex_roles = (EventRole.DPS,)
        interaction = self.make_flow_interaction()

        await flow.continue_after_roles(interaction)

        # The memory already matches the new selection; asking would be
        # noise.
        kwargs = interaction.edit_original_response.await_args.kwargs
        assert kwargs["view"] is None

    async def test_keep_button_leaves_the_signup_unchanged(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_signed_up_event(
            store,
            role=EventRole.DPS,
        )
        flow = EditSignupFlow(fake_bot, event, occurrence, 42)
        flow.role = EventRole.QUICKNESS_DPS
        confirm = EditWaitlistConfirmView(flow)
        interaction = make_interaction(message=ephemeral_message())

        await confirm.keep.callback(interaction)

        kwargs = interaction.response.edit_message.await_args.kwargs
        assert "left unchanged" in kwargs["content"]
        untouched = store.get_signup(occurrence.occurrence_id, 42)
        assert untouched is not None
        assert untouched.role is EventRole.DPS
        assert not untouched.waitlisted

    async def test_apply_reloads_event_state_before_applying(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_signed_up_event(store)
        flow = EditSignupFlow(fake_bot, event, occurrence, 42)
        flow.role = EventRole.DPS
        # Simulate a leader changing the category while the flow sat open: the
        # flow still holds the pre-change event, which is now a role-less WvW.
        # If apply used that stale copy it would reject with "no roles to
        # edit"; reloading by id sees the live Fractal event and applies.
        flow.event = replace(event, category=EventCategory.WVW)
        interaction = self.make_flow_interaction()

        await flow.apply(interaction, allow_waitlist=False)

        updated = store.get_signup(occurrence.occurrence_id, 42)
        assert updated is not None
        assert updated.role is EventRole.DPS
        assert updated.assigned_role is EventRole.DPS

    async def test_apply_reports_when_the_occurrence_is_gone(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_signed_up_event(store)
        flow = EditSignupFlow(fake_bot, event, occurrence, 42)
        flow.role = EventRole.DPS
        # The occurrence the flow was opened against no longer exists at apply
        # time (deleted, or the id never resolves after a series change).
        flow.occurrence = replace(occurrence, occurrence_id=999_999)
        interaction = self.make_flow_interaction()

        await flow.apply(interaction, allow_waitlist=False)

        kwargs = interaction.edit_original_response.await_args.kwargs
        assert "no longer exists" in kwargs["content"]
        assert kwargs["view"] is None


class TestSignupViews:
    def test_build_signup_view_is_persistent(self) -> None:
        view = build_signup_view(9)

        assert view.timeout is None
        assert len(view.children) == 3

    def test_signup_button_round_trips_through_custom_id(self) -> None:
        button = EventSignUpButton(9)

        assert button.occurrence_id == 9
        assert button.item.custom_id == "gw2bot:event-signup:9"
        assert button.template.match("gw2bot:event-signup:9") is not None

    def test_signup_summary_describes_flex_fallback(self) -> None:
        summary = _signup_summary(
            SimpleNamespace(
                waitlisted=False,
                assigned_role=EventRole.ALACRITY_DPS,
                role=EventRole.QUICKNESS_DPS,
            )  # type: ignore[arg-type]
        )

        assert "Alacrity DPS" in summary
        assert "flex" in summary

    def test_signup_summary_describes_waitlisting(self) -> None:
        summary = _signup_summary(
            SimpleNamespace(
                waitlisted=True,
                assigned_role=None,
                role=EventRole.DPS,
            )  # type: ignore[arg-type]
        )

        assert "waitlist" in summary


FAR_FUTURE = datetime(2107, 1, 30, 20, 0, tzinfo=UTC)


def make_edit_event(
    store: EventStore,
    channel_id: int = 1234,
    *,
    repeat_frequency: RepeatFrequency = RepeatFrequency.NONE,
    ping_role_ids: tuple[int, ...] = (),
) -> Any:
    return store.create_event(
        category=EventCategory.FRACTAL,
        title="Original Title",
        description="Original description.",
        channel_id=channel_id,
        leader_discord_id=42,
        start_time=FAR_FUTURE,
        duration_minutes=90,
        repeat_frequency=repeat_frequency,
        repeat_days=(),
        ping_role_ids=ping_role_ids,
    )


def make_posted_edit_event(
    store: EventStore,
    channel_id: int = 1234,
    *,
    repeat_frequency: RepeatFrequency = RepeatFrequency.NONE,
    ping_role_ids: tuple[int, ...] = (),
) -> Any:
    event = make_edit_event(
        store,
        channel_id,
        repeat_frequency=repeat_frequency,
        ping_role_ids=ping_role_ids,
    )
    occurrence = store.create_occurrence(event.event_id, event.start_time)
    store.set_occurrence_message(occurrence.occurrence_id, 1234, 555, 777)
    return event, occurrence


def make_ongoing_edit_event(store: EventStore) -> Any:
    # A recurring event that started ten minutes ago and is still running.
    started = datetime.now(UTC) - timedelta(minutes=10)
    event = store.create_event(
        category=EventCategory.FRACTAL,
        title="Ongoing Title",
        description="Original description.",
        channel_id=1234,
        leader_discord_id=42,
        start_time=started,
        duration_minutes=90,
        repeat_frequency=RepeatFrequency.DAILY,
        repeat_days=(),
    )
    occurrence = store.create_occurrence(event.event_id, started)
    store.set_occurrence_message(occurrence.occurrence_id, 1234, 555, 777)
    store.set_occurrence_status(occurrence.occurrence_id, EventStatus.ONGOING)
    return event, occurrence


# A recurring series whose live occurrence (SERIES_WEEK4) has advanced past the
# series origin (SERIES_ORIGIN), reproducing the divergence that caused the
# spurious-reschedule bug.
SERIES_ORIGIN = datetime(2107, 1, 6, 20, 0, tzinfo=UTC)
SERIES_WEEK4 = datetime(2107, 1, 27, 20, 0, tzinfo=UTC)


def make_advanced_recurring_event(
    store: EventStore,
    channel_id: int = 1234,
    *,
    posted: bool = True,
) -> Any:
    event = store.create_event(
        category=EventCategory.FRACTAL,
        title="Weekly clear",
        description="Bring food.",
        channel_id=channel_id,
        leader_discord_id=42,
        start_time=SERIES_ORIGIN,
        duration_minutes=90,
        repeat_frequency=RepeatFrequency.WEEKLY,
        repeat_days=(0,),
    )
    occurrence = store.create_occurrence(event.event_id, SERIES_WEEK4)
    if posted:
        store.set_occurrence_message(occurrence.occurrence_id, 1234, 555, 777)
    return event, occurrence


def make_posted_recurring_event(store: EventStore) -> Any:
    """A recurring series, with the occurrence read back after it was posted.

    The helper above returns the row as it was created, before its message ids
    were stored. A command re-reads the occurrence before handing it to a view,
    so a view test has to start from the stored row to see the same post the
    commander is looking at.
    """
    event, occurrence = make_advanced_recurring_event(store)
    stored = store.get_occurrence(occurrence.occurrence_id)
    assert stored is not None
    return event, stored


class TestEditCommand:
    async def test_edit_rejects_users_without_the_create_role(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        event, _ = make_posted_edit_event(store)
        interaction = make_interaction()

        await cast(Any, group.edit.callback)(group, interaction, event.event_id)

        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args is not None
        kwargs = interaction.response.send_message.await_args.kwargs
        assert kwargs["ephemeral"] is True
        # An error, not a preview.
        assert "embeds" not in kwargs

    async def test_edit_rejects_unknown_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.edit.callback)(group, interaction, 999)

        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args is not None
        assert (
            "does not exist or is over"
            in interaction.response.send_message.await_args.args[0]
        )

    async def test_edit_rejects_completed_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        event, occurrence = make_posted_edit_event(store)
        store.set_occurrence_status(
            occurrence.occurrence_id,
            EventStatus.OVER,
        )
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.edit.callback)(group, interaction, event.event_id)

        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args is not None
        assert (
            "does not exist or is over"
            in interaction.response.send_message.await_args.args[0]
        )

    async def test_edit_opens_preview_for_an_active_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        event, _ = make_posted_edit_event(store)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.edit.callback)(group, interaction, event.event_id)

        interaction.response.send_message.assert_not_awaited()
        kwargs = preview_kwargs(interaction)
        assert len(kwargs["embeds"]) == 2
        assert isinstance(kwargs["view"], EventEditConfirmView)

    async def test_edit_preview_uses_the_live_occurrence_date(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        event, _ = make_advanced_recurring_event(store)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.edit.callback)(group, interaction, event.event_id)

        # The preview must show the upcoming occurrence's date (week 4), not the
        # series origin (week 1) stored on the event.
        kwargs = preview_kwargs(interaction)
        preview = kwargs["embeds"][0]
        date_field = next(
            field for field in preview.fields if field.name == "📅 Date & Time"
        )
        assert date_field.value == f"<t:{int(SERIES_WEEK4.timestamp())}:f>"

    async def test_autocomplete_lists_only_active_events(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        active, _ = make_posted_edit_event(store)
        completed, completed_occurrence = make_posted_edit_event(store)
        store.set_occurrence_status(
            completed_occurrence.occurrence_id,
            EventStatus.OVER,
        )
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        choices = await group.active_event_id_autocomplete(interaction, "")

        values = [choice.value for choice in choices]
        assert active.event_id in values
        assert completed.event_id not in values

    async def test_autocomplete_filters_by_query(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        wing = store.create_event(
            category=EventCategory.RAID,
            title="Wing seven",
            description="Bring food.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=FAR_FUTURE,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        store.create_occurrence(wing.event_id, FAR_FUTURE)
        dailies = store.create_event(
            category=EventCategory.FRACTAL,
            title="Daily fractals",
            description="Bring food.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=FAR_FUTURE,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        store.create_occurrence(dailies.event_id, FAR_FUTURE)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        choices = await group.active_event_id_autocomplete(interaction, "wing")

        assert [choice.value for choice in choices] == [wing.event_id]

    async def test_autocomplete_returns_nothing_for_unauthorized_users(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        make_posted_edit_event(store)
        interaction = make_interaction()

        choices = await group.active_event_id_autocomplete(interaction, "")

        assert choices == []


class TestEditCommandOngoing:
    async def test_edit_opens_the_roster_only_editor(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        event, _ = make_ongoing_edit_event(store)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.edit.callback)(
            group, interaction, event.event_id
        )

        # A running event's details are frozen, so the editor carries the
        # roster controls alone: no "Save changes" and no "Change something",
        # which are the paths into apply_event_edit.
        kwargs = preview_kwargs(interaction)
        view = kwargs["view"]
        assert isinstance(view, EventRosterEditView)
        labels = [
            item.label
            for item in view.children
            if isinstance(item, discord.ui.Button)
        ]
        assert labels == ["Add sign-ups", "Remove sign-ups"]
        assert view._draft.roster_only

    async def test_roster_only_preview_shows_the_live_status(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        event, _ = make_ongoing_edit_event(store)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.edit.callback)(
            group, interaction, event.event_id
        )

        # The preview mirrors a message that is live right now, so it must not
        # render the event as OPEN the way a pending-changes preview does.
        preview, notice = preview_kwargs(interaction)["embeds"]
        assert preview.color is not None
        assert preview.color.value == STATUS_COLORS[EventStatus.ONGOING]
        assert "only its roster" in notice.description

    async def test_edit_rejects_an_occurrence_past_its_end(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # Past its end but still ONGOING in the database, because the scheduler
        # has not run since. Its roster is history, so nothing is editable.
        group = EventCommands(fake_bot)
        event, occurrence = make_ongoing_edit_event(store)
        store.set_occurrence_start_time(
            occurrence.occurrence_id,
            datetime.now(UTC) - timedelta(minutes=event.duration_minutes + 1),
        )
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.edit.callback)(
            group, interaction, event.event_id
        )

        interaction.response.defer.assert_not_awaited()
        assert (
            "does not exist or is over"
            in interaction.response.send_message.await_args.args[0]
        )

    async def test_edit_drops_roster_members_who_left_the_server(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        event, occurrence = make_ongoing_edit_event(store)
        for user_id, role, waitlisted in (
            (7, EventRole.QUICKNESS_HEAL, False),
            (8, EventRole.DPS, False),
        ):
            store.add_signup(
                occurrence_id=occurrence.occurrence_id,
                discord_user_id=user_id,
                role=role,
                assigned_role=None if waitlisted else role,
                flex_roles=(),
                waitlisted=waitlisted,
            )
        # User 8 has left the server; user 7 is still here.
        guild = FakeGuild({7: "Still Here"})
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            guild=guild,
        )

        await cast(Any, group.edit.callback)(
            group, interaction, event.event_id
        )

        seated = [
            signup.discord_user_id
            for signup in store.get_signups(occurrence.occurrence_id)
        ]
        assert seated == [7]
        kwargs = preview_kwargs(interaction)
        assert "left the server" in kwargs["content"]
        # The preview that follows shows the roster as it stands after the
        # prune, not the one the departure was still on.
        rendered = "".join(
            f"{field.name}{field.value}"
            for field in kwargs["embeds"][0].fields
        )
        assert "<@7>" in rendered
        assert "<@8>" not in rendered

    async def test_edit_keeps_the_roster_when_lookups_fail(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # Discord being unreachable proves nothing about membership, so an
        # outage must never quietly empty a roster.
        group = EventCommands(fake_bot)
        event, occurrence = make_ongoing_edit_event(store)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=7,
            role=EventRole.DPS,
            assigned_role=EventRole.DPS,
            flex_roles=(),
            waitlisted=False,
        )
        guild = MagicMock()
        guild.get_member = MagicMock(return_value=None)
        guild.fetch_member = AsyncMock(side_effect=forbidden_error(50001))
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            guild=guild,
        )

        await cast(Any, group.edit.callback)(
            group, interaction, event.event_id
        )

        assert len(store.get_signups(occurrence.occurrence_id)) == 1
        # Nobody was reported as removed, so the preview carries no note.
        assert preview_kwargs(interaction)["content"] is MISSING

    async def test_roster_session_stays_on_the_occurrence_it_opened(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # A running event is minutes from ending, and the scheduler seeds the
        # series' next occurrence the moment it does. Re-resolving "the soonest
        # live occurrence" on each click would hand the picker next week's
        # roster, so a leader tidying up tonight's run would silently be
        # removing people from the one after it.
        group = EventCommands(fake_bot)
        event, occurrence = make_ongoing_edit_event(store)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=7,
            role=EventRole.DPS,
            assigned_role=EventRole.DPS,
            flex_roles=(),
            waitlisted=False,
        )
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            guild=FakeGuild({7: "Still Here"}),
        )
        await cast(Any, group.edit.callback)(
            group, interaction, event.event_id
        )
        view = preview_kwargs(interaction)["view"]
        assert isinstance(view, EventRosterEditView)

        # The run ends and the scheduler retires it, seeding tomorrow's.
        store.set_occurrence_start_time(
            occurrence.occurrence_id,
            datetime.now(UTC) - timedelta(minutes=event.duration_minutes + 1),
        )
        store.set_occurrence_status(
            occurrence.occurrence_id,
            EventStatus.OVER,
        )
        successor = store.create_occurrence(
            event.event_id,
            datetime.now(UTC) + timedelta(days=1),
        )
        store.add_signup(
            occurrence_id=successor.occurrence_id,
            discord_user_id=8,
            role=EventRole.DPS,
            assigned_role=EventRole.DPS,
            flex_roles=(),
            waitlisted=False,
        )
        click = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
            guild=FakeGuild({}),
        )
        click.edit_original_response = AsyncMock()

        await view.remove_signups.callback(click)

        # The pinned run is over, so the session refuses rather than opening a
        # picker over the successor's roster - which must be untouched.
        kwargs = click.response.edit_message.await_args.kwargs
        assert "already ended" in kwargs["content"]
        assert kwargs["view"] is None
        assert [
            signup.discord_user_id
            for signup in store.get_signups(successor.occurrence_id)
        ] == [8]

    async def test_roster_removal_stays_available_while_running(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = make_ongoing_edit_event(store)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=7,
            role=EventRole.DPS,
            assigned_role=EventRole.DPS,
            flex_roles=(),
            waitlisted=False,
        )
        draft = draft_from_event(
            event,
            ZoneInfo("UTC"),
            start_time_override=occurrence.start_time,
            roster_only=True,
        )
        view = EventRosterEditView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.remove_signups.callback(interaction)

        assert interaction.edit_original_response.await_args is not None
        picker = interaction.edit_original_response.await_args.kwargs["view"]
        assert isinstance(picker, RemoveSignupsView)


class TestDepartedSummary:
    def test_names_a_single_departure(self) -> None:
        summary = _departed_summary([7], {7: "Wanderer"})

        assert summary == (
            "Removed Wanderer from the roster: they have left the server."
        )

    def test_names_several_departures(self) -> None:
        summary = _departed_summary([7, 8], {7: "Wanderer", 8: "Drifter"})

        assert summary == (
            "Removed Wanderer, Drifter from the roster: they have left the "
            "server."
        )

    def test_falls_back_to_the_id_when_the_name_is_unknown(self) -> None:
        # Discord could not be reached for them, but they still left.
        summary = _departed_summary([7], {})

        assert summary is not None
        assert "Member 7" in summary

    def test_reports_nothing_when_nobody_left(self) -> None:
        assert _departed_summary([], {}) is None

    def test_caps_a_mass_departure_at_the_content_budget(self) -> None:
        # A whole WvW roster leaving would otherwise build a message Discord
        # refuses - after the removals are already committed, so the commander
        # would be left with no answer at all.
        departed = list(range(1, 61))
        names = {user_id: "N" * 100 for user_id in departed}

        summary = _departed_summary(departed, names)

        assert summary is not None
        assert len(summary) < 2000
        # Whoever did not fit is still accounted for.
        assert "others" in summary
        named = summary.count("N" * 100)
        assert named < len(departed)
        assert f"and {len(departed) - named} others" in summary

    def test_a_single_overlong_name_is_still_reported(self) -> None:
        # The budget never drops the only entry it has, so the message stays
        # meaningful rather than reading as "and 1 other".
        summary = _departed_summary([7], {7: "N" * 100})

        assert summary is not None
        assert "N" * 100 in summary
        assert "other" not in summary


class TestPingRolesEndToEnd:
    async def test_posting_stores_the_roles_and_pings_them(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        draft = replace(make_complete_draft(), ping_role_ids=(10, 20))
        view = EventConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.followup.send = AsyncMock()

        await view.post_event.callback(interaction)

        stored = store.get_event(1)
        assert stored is not None
        assert stored.ping_role_ids == (10, 20)
        # The mentions are the message content, so they land above the embed.
        assert channel.sent[0]["content"] == "<@&10> <@&20>"

    async def test_editing_replaces_the_roles_without_re_pinging(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, _ = make_posted_edit_event(store, ping_role_ids=(10,))
        draft = draft_from_event(event, ZoneInfo("UTC"))
        # The edit session starts from what the event already pings.
        assert draft.ping_role_ids == (10,)
        draft.ping_role_ids = (20, 30)
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.save_changes.callback(interaction)

        updated = store.get_event(event.event_id)
        assert updated is not None
        assert updated.ping_role_ids == (20, 30)
        # An edit refreshes the existing message rather than posting a new one,
        # so nobody is pinged a second time.
        assert channel.sent == []
        edit = channel.partial_message.edit.await_args
        assert edit is not None
        assert "content" not in edit.kwargs


class TestEventEditConfirmView:
    async def test_save_changes_updates_event_and_refreshes_message(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, _ = make_posted_edit_event(store)
        draft = draft_from_event(event, ZoneInfo("UTC"))
        draft.title = "Edited Title"
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.save_changes.callback(interaction)

        updated = store.get_event(event.event_id)
        assert updated is not None
        assert updated.title == "Edited Title"
        channel.partial_message.edit.assert_awaited()
        interaction.edit_original_response.assert_awaited()
        assert interaction.edit_original_response.await_args is not None
        assert (
            "was updated"
            in interaction.edit_original_response.await_args.kwargs["content"]
        )

    async def test_save_changes_reschedules_the_posted_occurrence(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = make_posted_edit_event(store)
        draft = draft_from_event(event, ZoneInfo("UTC"))
        new_start = datetime(2107, 2, 5, 21, 0, tzinfo=UTC)
        draft.start_time = new_start
        draft.start_text = "02.05.2107 21:00"
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.save_changes.callback(interaction)

        rescheduled = store.get_occurrence(occurrence.occurrence_id)
        assert rescheduled is not None
        assert rescheduled.start_time == new_start
        # The reschedule forces the thread name to update.
        channel.thread.edit.assert_awaited()

    async def test_editing_recurring_event_does_not_reschedule_on_no_date_change(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        # Drive the whole flow through the command so the draft is hydrated the
        # way production does; a regression in either the hydration source or
        # the reschedule guard drags the occurrence back to the series origin.
        group = EventCommands(fake_bot)
        event, occurrence = make_advanced_recurring_event(store)
        open_interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))
        await cast(Any, group.edit.callback)(
            group, open_interaction, event.event_id
        )
        view = preview_kwargs(open_interaction)["view"]
        assert isinstance(view, EventEditConfirmView)

        # Change only the title, then save through the real preview view.
        view._draft.title = "Renamed clear"
        save_interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        save_interaction.edit_original_response = AsyncMock()
        await view.save_changes.callback(save_interaction)

        # The upcoming occurrence must NOT be dragged back to the series origin.
        reloaded = store.get_occurrence(occurrence.occurrence_id)
        assert reloaded is not None
        assert reloaded.start_time == SERIES_WEEK4
        assert store.get_event(event.event_id).title == "Renamed clear"  # type: ignore[union-attr]

    async def test_editing_recurring_event_preserves_the_series_origin(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        # The draft is seeded with the *live occurrence's* start, which has long
        # since advanced past the series origin. Writing it back into the event
        # row would drag the origin forward on every edit until it no longer
        # records when the series began.
        event, _ = make_advanced_recurring_event(store)
        draft = draft_from_event(
            event,
            ZoneInfo("UTC"),
            start_time_override=SERIES_WEEK4,
        )
        draft.title = "Renamed clear"
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.save_changes.callback(interaction)

        reloaded = store.get_event(event.event_id)
        assert reloaded is not None
        assert reloaded.title == "Renamed clear"
        assert reloaded.start_time == SERIES_ORIGIN

    async def test_rescheduling_a_series_shifts_the_origin_by_the_same_delta(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        # Moving the occurrence an hour later moves the origin an hour later
        # too, so the origin keeps describing the same series rather than being
        # overwritten with the occurrence's absolute date.
        event, occurrence = make_advanced_recurring_event(store)
        draft = draft_from_event(
            event,
            ZoneInfo("UTC"),
            start_time_override=SERIES_WEEK4,
        )
        new_start = SERIES_WEEK4 + timedelta(hours=1)
        draft.start_time = new_start
        draft.start_text = "01.27.2107 21:00"
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.save_changes.callback(interaction)

        rescheduled = store.get_occurrence(occurrence.occurrence_id)
        assert rescheduled is not None
        assert rescheduled.start_time == new_start
        reloaded = store.get_event(event.event_id)
        assert reloaded is not None
        assert reloaded.start_time == SERIES_ORIGIN + timedelta(hours=1)

    async def test_editing_date_reschedules_an_unposted_occurrence(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = make_advanced_recurring_event(store, posted=False)
        draft = draft_from_event(
            event,
            ZoneInfo("UTC"),
            start_time_override=occurrence.start_time,
        )
        new_start = datetime(2107, 2, 3, 20, 0, tzinfo=UTC)
        draft.start_time = new_start
        draft.start_text = "02.03.2107 20:00"
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.save_changes.callback(interaction)

        # The as-yet-unposted occurrence must be rescheduled so the scheduler
        # posts it at the new time.
        reloaded = store.get_occurrence(occurrence.occurrence_id)
        assert reloaded is not None
        assert reloaded.start_time == new_start

    async def test_save_changes_ignores_a_racing_second_click(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = make_posted_edit_event(store)
        draft = draft_from_event(
            event,
            ZoneInfo("UTC"),
            start_time_override=occurrence.start_time,
        )
        draft.title = "First save"
        view = EventEditConfirmView(fake_bot, draft)
        first = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        first.edit_original_response = AsyncMock()

        await view.save_changes.callback(first)
        assert draft.edit_applied

        # A second click on the same (already-applied) draft must be a no-op.
        draft.title = "Second save"
        second = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        second.edit_original_response = AsyncMock()

        await view.save_changes.callback(second)

        assert store.get_event(event.event_id).title == "First save"  # type: ignore[union-attr]
        second.response.send_message.assert_awaited_once()
        assert (
            "already updated"
            in second.response.send_message.await_args.args[0]
        )

    async def test_channel_move_keeps_the_old_post_when_the_repost_fails(
        self,
        store: EventStore,
    ) -> None:
        old_channel = FakeChannel(channel_id=1234, thread=FakeThread(777))
        new_channel = FakeChannel(channel_id=5678, thread=FakeThread(888))
        bot = cast(Any, FakeBot(store, old_channel))
        bot._channels[new_channel.id] = new_channel
        bot._channels[new_channel.thread.id] = new_channel.thread
        event = make_edit_event(
            store,
            channel_id=old_channel.id,
            ping_role_ids=(10,),
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        posted = await post_occurrence(bot, event, occurrence)
        draft = draft_from_event(
            event,
            ZoneInfo("UTC"),
            start_time_override=occurrence.start_time,
        )
        draft.channel_id = new_channel.id
        new_channel.send_error = forbidden_error(50001)
        view = ChannelMoveConfirmView(bot, draft, old_channel.id)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.move.callback(interaction)

        # The move failed, so the only public post must survive in the old
        # channel and the stored channel must be put back to match it. Leaving
        # channel_id on the new channel would send the next scheduler refresh
        # looking for this message there, get NotFound and retire a live event.
        old_channel.partial_message.delete.assert_not_awaited()
        restored = store.get_event(event.event_id)
        assert restored is not None
        assert restored.channel_id == old_channel.id
        # Putting the channel back rewrites the whole row, so the rest of the
        # event - its ping roles included - must survive that write.
        assert restored.ping_role_ids == (10,)
        stored = store.get_occurrence(occurrence.occurrence_id)
        assert stored is not None
        assert stored.message_id == posted.message_id
        assert stored.status is not EventStatus.OVER
        assert interaction.edit_original_response.await_args is not None
        content = interaction.edit_original_response.await_args.kwargs["content"]
        assert "stays in the current one" in content

    async def test_save_changes_refuses_an_event_that_started_during_preview(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        # The reported failure: shortening a running recurring event's duration
        # so that start + duration is already behind now. The refresh would
        # persist OVER without seeding the next occurrence the way the scheduler
        # does, which silently ends the series. Ongoing events are not editable.
        event, occurrence = make_ongoing_edit_event(store)
        draft = draft_from_event(
            event,
            ZoneInfo("UTC"),
            start_time_override=occurrence.start_time,
        )
        draft.duration_minutes = 1
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.save_changes.callback(interaction)

        # Nothing was written and the occurrence was not retired, so the
        # scheduler still owns the OVER transition and seeds the next occurrence.
        assert store.get_event(event.event_id).duration_minutes == 90  # type: ignore[union-attr]
        stored = store.get_occurrence(occurrence.occurrence_id)
        assert stored is not None
        assert stored.status is not EventStatus.OVER
        assert store.get_event_occurrences(event.event_id) == [stored]
        channel.partial_message.edit.assert_not_awaited()
        assert interaction.edit_original_response.await_args is not None
        content = interaction.edit_original_response.await_args.kwargs["content"]
        assert "already started" in content
        assert "/event delete" in content

    async def test_category_change_reseats_the_roster(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event = store.create_event(
            category=EventCategory.WVW,
            title="Border Push",
            description="Bring siege.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=FAR_FUTURE,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, FAR_FUTURE)
        store.set_occurrence_message(occurrence.occurrence_id, 1234, 555, 777)
        # WvW has no roles, so every signup is stored without one.
        for user_id in range(1, 8):
            store.add_signup(
                occurrence_id=occurrence.occurrence_id,
                discord_user_id=user_id,
                role=None,
                assigned_role=None,
                flex_roles=(),
                waitlisted=False,
            )
        draft = draft_from_event(
            event,
            ZoneInfo("UTC"),
            start_time_override=occurrence.start_time,
        )
        draft.category = EventCategory.FRACTAL
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.save_changes.callback(interaction)

        signups = store.get_signups(occurrence.occurrence_id)
        admitted = [signup for signup in signups if not signup.waitlisted]
        waitlisted = [signup for signup in signups if signup.waitlisted]
        # A Fractal seats 1 healer and 4 DPS. Nobody picked a role in WvW, so
        # they all fall back to DPS: the first four keep seats in sign-up order
        # and the rest are waitlisted, instead of seven role-less signups the
        # capacity check would read as an empty roster and keep admitting onto.
        assert [signup.discord_user_id for signup in admitted] == [1, 2, 3, 4]
        assert len(waitlisted) == 3
        assert all(
            signup.assigned_role is EventRole.DPS for signup in admitted
        )
        # The role is materialised too, because waitlist promotion skips a
        # signup that has no role.
        assert all(signup.role is EventRole.DPS for signup in signups)
        assert all(signup.assigned_role is None for signup in waitlisted)

    async def test_failed_move_leaves_the_old_post_flagged_for_refresh(
        self,
        store: EventStore,
    ) -> None:
        old_channel = FakeChannel(channel_id=1234, thread=FakeThread(777))
        new_channel = FakeChannel(channel_id=5678, thread=FakeThread(888))
        bot = cast(Any, FakeBot(store, old_channel))
        bot._channels[new_channel.id] = new_channel
        bot._channels[new_channel.thread.id] = new_channel.thread
        event = make_edit_event(store, channel_id=old_channel.id)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        await post_occurrence(bot, event, occurrence)
        draft = draft_from_event(
            event,
            ZoneInfo("UTC"),
            start_time_override=occurrence.start_time,
        )
        # A channel move bundled with a title change.
        draft.channel_id = new_channel.id
        draft.title = "Edited Title"
        new_channel.send_error = forbidden_error(50001)
        view = ChannelMoveConfirmView(bot, draft, old_channel.id)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.move.callback(interaction)

        # The move failed, but the title change is committed, so the surviving
        # post in the old channel is now stale. An edit does not change the
        # status, so the scheduler would skip it forever unless it is flagged.
        assert store.get_event(event.event_id).title == "Edited Title"  # type: ignore[union-attr]
        stale = store.get_occurrence(occurrence.occurrence_id)
        assert stale is not None
        assert stale.needs_refresh
        old_channel.partial_message.edit.assert_not_awaited()

        # The next maintenance pass re-renders it in place, in the channel it
        # actually lives in.
        await run_event_maintenance(bot, FAR_FUTURE - timedelta(hours=2))

        old_channel.partial_message.edit.assert_awaited()
        new_channel.partial_message.edit.assert_not_awaited()
        recovered = store.get_occurrence(occurrence.occurrence_id)
        assert recovered is not None
        assert not recovered.needs_refresh

    async def test_save_changes_reports_a_failed_message_refresh(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = make_posted_edit_event(store)
        # refresh_occurrence_message absorbs this failure, marks the occurrence
        # dirty and returns instead of raising, so the edit flow must not report
        # the stale public message as successfully updated.
        channel.partial_message.edit = AsyncMock(
            side_effect=forbidden_error(50001)
        )
        draft = draft_from_event(event, ZoneInfo("UTC"))
        draft.title = "Edited Title"
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.save_changes.callback(interaction)

        # The event row is still saved, and the occurrence is left dirty so the
        # scheduler retries it.
        assert store.get_event(event.event_id).title == "Edited Title"  # type: ignore[union-attr]
        stored = store.get_occurrence(occurrence.occurrence_id)
        assert stored is not None
        assert stored.needs_refresh
        assert interaction.edit_original_response.await_args is not None
        content = interaction.edit_original_response.await_args.kwargs["content"]
        assert "could not be updated" in content
        assert "was updated" not in content

    async def test_save_changes_rejects_users_without_the_role(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, _ = make_posted_edit_event(store)
        draft = draft_from_event(event, ZoneInfo("UTC"))
        draft.title = "Sneaky Edit"
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(message=ephemeral_message())

        await view.save_changes.callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        assert store.get_event(event.event_id).title == "Original Title"  # type: ignore[union-attr]

    async def test_save_changes_prompts_before_moving_a_posted_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, _ = make_posted_edit_event(store)
        draft = draft_from_event(event, ZoneInfo("UTC"))
        draft.channel_id = 5678
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )

        await view.save_changes.callback(interaction)

        interaction.response.edit_message.assert_awaited_once()
        assert interaction.response.edit_message.await_args is not None
        kwargs = interaction.response.edit_message.await_args.kwargs
        assert isinstance(kwargs["view"], ChannelMoveConfirmView)
        assert "delete" in kwargs["content"].lower()
        # Nothing is saved until the move is confirmed.
        assert store.get_event(event.event_id).channel_id == 1234  # type: ignore[union-attr]

    async def test_channel_move_confirm_reposts_to_the_new_channel(
        self,
        store: EventStore,
    ) -> None:
        old_channel = FakeChannel(channel_id=1234, thread=FakeThread(777))
        new_channel = FakeChannel(channel_id=5678, thread=FakeThread(888))
        bot = cast(Any, FakeBot(store, old_channel))
        bot._channels[new_channel.id] = new_channel
        bot._channels[new_channel.thread.id] = new_channel.thread
        event = make_edit_event(store, channel_id=old_channel.id)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        await post_occurrence(bot, event, occurrence)
        draft = draft_from_event(event, ZoneInfo("UTC"))
        draft.channel_id = new_channel.id
        view = ChannelMoveConfirmView(bot, draft, old_channel.id)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.move.callback(interaction)

        old_channel.partial_message.delete.assert_awaited_once()
        assert len(new_channel.sent) == 1
        updated = store.get_event(event.event_id)
        assert updated is not None
        assert updated.channel_id == new_channel.id
        assert interaction.edit_original_response.await_args is not None
        assert (
            "was updated"
            in interaction.edit_original_response.await_args.kwargs["content"]
        )

    async def test_category_and_channel_move_pings_the_new_thread(
        self,
        store: EventStore,
    ) -> None:
        old_channel = FakeChannel(channel_id=1234, thread=FakeThread(777))
        new_channel = FakeChannel(channel_id=5678, thread=FakeThread(888))
        bot = cast(Any, FakeBot(store, old_channel))
        bot._channels[new_channel.id] = new_channel
        bot._channels[new_channel.thread.id] = new_channel.thread
        event = store.create_event(
            category=EventCategory.RAID,
            title="Wing run",
            description="Bring food.",
            channel_id=old_channel.id,
            leader_discord_id=42,
            start_time=FAR_FUTURE,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        await post_occurrence(bot, event, occurrence)
        # Shrinking a raid to a fractal reseats the roster: the seated
        # Quickness DPS loses its boon slot and is flexed to plain DPS, so the
        # reseat produces a role change to announce.
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=1,
            role=EventRole.QUICKNESS_HEAL,
            assigned_role=EventRole.QUICKNESS_HEAL,
            flex_roles=(),
            waitlisted=False,
        )
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=2,
            role=EventRole.QUICKNESS_DPS,
            assigned_role=EventRole.QUICKNESS_DPS,
            flex_roles=(),
            waitlisted=False,
        )
        for user_id in range(3, 6):
            store.add_signup(
                occurrence_id=occurrence.occurrence_id,
                discord_user_id=user_id,
                role=EventRole.DPS,
                assigned_role=EventRole.DPS,
                flex_roles=(),
                waitlisted=False,
            )
        draft = draft_from_event(
            event,
            ZoneInfo("UTC"),
            start_time_override=occurrence.start_time,
        )
        draft.channel_id = new_channel.id
        draft.category = EventCategory.FRACTAL
        view = ChannelMoveConfirmView(bot, draft, old_channel.id)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.move.callback(interaction)

        # The reseat ping must reach members in the new thread; the old thread
        # was deleted by the repost, so a ping sent there would vanish.
        new_channel.thread.send.assert_awaited_once()
        old_channel.thread.send.assert_not_awaited()
        send = new_channel.thread.send.await_args
        assert send is not None
        assert "<@2>" in send.args[0]

    async def test_channel_move_keep_reverts_and_returns_to_preview(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, _ = make_posted_edit_event(store)
        draft = draft_from_event(event, ZoneInfo("UTC"))
        draft.channel_id = 5678
        view = ChannelMoveConfirmView(fake_bot, draft, 1234)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )

        await view.keep.callback(interaction)

        assert draft.channel_id == 1234
        interaction.response.edit_message.assert_awaited_once()
        assert interaction.response.edit_message.await_args is not None
        kwargs = interaction.response.edit_message.await_args.kwargs
        assert isinstance(kwargs["view"], EventEditConfirmView)


class TestDeleteCommand:
    async def test_delete_rejects_users_without_the_create_role(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        event, _ = make_posted_edit_event(store)
        interaction = make_interaction()

        await cast(Any, group.delete.callback)(
            group, interaction, event.event_id
        )

        interaction.response.send_message.assert_awaited_once()
        kwargs = interaction.response.send_message.await_args.kwargs
        assert kwargs["ephemeral"] is True
        assert "view" not in kwargs
        # The event is untouched.
        assert store.get_event(event.event_id) is not None

    async def test_delete_rejects_unknown_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.delete.callback)(group, interaction, 999)

        interaction.response.send_message.assert_awaited_once()
        assert (
            "does not exist"
            in interaction.response.send_message.await_args.args[0]
        )

    async def test_delete_opens_confirmation_for_an_existing_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        event, _ = make_posted_edit_event(store)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.delete.callback)(
            group, interaction, event.event_id
        )

        interaction.response.send_message.assert_awaited_once()
        kwargs = interaction.response.send_message.await_args.kwargs
        assert isinstance(kwargs["view"], EventDeleteConfirmView)
        assert kwargs["ephemeral"] is True
        # Confirmation only; nothing is deleted yet.
        assert store.get_event(event.event_id) is not None


class TestCancelCommand:
    async def test_cancel_rejects_users_without_the_create_role(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        event, occurrence = make_advanced_recurring_event(store)
        interaction = make_interaction()

        await cast(Any, group.cancel.callback)(
            group, interaction, event.event_id
        )

        interaction.response.send_message.assert_awaited_once()
        kwargs = interaction.response.send_message.await_args.kwargs
        assert kwargs["ephemeral"] is True
        assert "view" not in kwargs
        # The occurrence is untouched.
        assert store.get_occurrence(occurrence.occurrence_id) is not None

    async def test_cancel_rejects_unknown_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.cancel.callback)(group, interaction, 999)

        interaction.response.send_message.assert_awaited_once()
        assert (
            "does not exist"
            in interaction.response.send_message.await_args.args[0]
        )

    async def test_cancel_opens_the_confirmation_for_a_repeating_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        event, occurrence = make_advanced_recurring_event(store)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.cancel.callback)(
            group, interaction, event.event_id
        )

        interaction.response.send_message.assert_awaited_once()
        kwargs = interaction.response.send_message.await_args.kwargs
        assert isinstance(kwargs["view"], EventCancelConfirmView)
        assert kwargs["ephemeral"] is True
        # The confirmation names the occurrence being called off, so nobody
        # cancels a run they were not looking at.
        content = interaction.response.send_message.await_args.args[0]
        starts_at = format_event_datetime(
            occurrence.start_time,
            ZoneInfo("UTC"),
        )
        assert starts_at in content
        # Confirmation only; nothing is cancelled yet.
        assert store.get_occurrence(occurrence.occurrence_id) is not None

    async def test_cancel_falls_back_to_deletion_for_a_one_off_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        event, occurrence = make_posted_edit_event(store)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.cancel.callback)(
            group, interaction, event.event_id
        )

        # An event that does not repeat has nothing left after the run being
        # cancelled, so cancelling it is deleting it.
        kwargs = interaction.response.send_message.await_args.kwargs
        assert isinstance(kwargs["view"], EventDeleteConfirmView)
        assert (
            "does not repeat"
            in interaction.response.send_message.await_args.args[0]
        )
        assert store.get_event(event.event_id) is not None
        assert store.get_occurrence(occurrence.occurrence_id) is not None

    async def test_cancel_rejects_a_series_without_an_upcoming_occurrence(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        event, occurrence = make_advanced_recurring_event(store)
        store.set_occurrence_status(
            occurrence.occurrence_id,
            EventStatus.OVER,
        )
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.cancel.callback)(
            group, interaction, event.event_id
        )

        interaction.response.send_message.assert_awaited_once()
        assert (
            "no upcoming occurrence"
            in interaction.response.send_message.await_args.args[0]
        )


    async def test_cancel_refuses_a_one_off_event_that_has_already_run(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        started = datetime.now(UTC) - timedelta(hours=3)
        event = store.create_event(
            category=EventCategory.FRACTAL,
            title="One and done",
            description="Bring food.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=started,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, started)
        store.set_occurrence_message(occurrence.occurrence_id, 1234, 555, 777)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.cancel.callback)(
            group, interaction, event.event_id
        )

        # The run is behind us, so there is nothing to call off. Offering the
        # deletion here would erase a finished run's roster and post through a
        # command that only promised to cancel the next one.
        kwargs = interaction.response.send_message.await_args.kwargs
        assert "view" not in kwargs
        assert (
            "no upcoming occurrence"
            in interaction.response.send_message.await_args.args[0]
        )
        assert store.get_event(event.event_id) is not None


class TestCancelDeleteFallbackView:
    def _one_off_event(self, store: EventStore) -> Any:
        event = make_edit_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        store.set_occurrence_message(occurrence.occurrence_id, 1234, 555, 777)
        return event, occurrence

    async def test_delete_is_refused_once_the_event_repeats(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = self._one_off_event(store)
        view = EventDeleteConfirmView(fake_bot, event, only_while_one_off=True)
        # An edit gives the event a repeat while the confirmation sits open.
        store.update_event(
            event_id=event.event_id,
            category=event.category,
            title=event.title,
            description=event.description,
            channel_id=event.channel_id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=RepeatFrequency.DAILY,
            repeat_days=(),
        )
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.delete.callback(interaction)

        # `/event cancel` never removes more than the next run of a repeating
        # event, so the reason this confirmation offered deletion is gone.
        assert store.get_event(event.event_id) is not None
        assert store.get_occurrence(occurrence.occurrence_id) is not None
        channel.partial_message.delete.assert_not_awaited()
        assert interaction.edit_original_response.await_args is not None
        assert (
            "repeats now"
            in interaction.edit_original_response.await_args.kwargs["content"]
        )

    async def test_delete_still_removes_a_one_off_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self._one_off_event(store)
        view = EventDeleteConfirmView(fake_bot, event, only_while_one_off=True)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.delete.callback(interaction)

        assert store.get_event(event.event_id) is None
        assert store.get_occurrence(occurrence.occurrence_id) is None

    async def test_delete_is_refused_once_the_run_has_ended(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        started = datetime.now(UTC) - timedelta(hours=3)
        event = store.create_event(
            category=EventCategory.FRACTAL,
            title="One and done",
            description="Bring food.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=started,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, started)
        store.set_occurrence_message(occurrence.occurrence_id, 1234, 555, 777)
        view = EventDeleteConfirmView(fake_bot, event, only_while_one_off=True)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.delete.callback(interaction)

        # The run ended while the confirmation sat open, so this is history
        # now - `/event delete` removes it, `/event cancel` does not.
        assert store.get_event(event.event_id) is not None
        channel.partial_message.delete.assert_not_awaited()
        assert interaction.edit_original_response.await_args is not None
        assert (
            "already run"
            in interaction.edit_original_response.await_args.kwargs["content"]
        )

    async def test_a_failed_acknowledgement_releases_the_guard(
        self,
        fake_bot: Any,
        store: EventStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        event, _ = self._one_off_event(store)
        view = EventDeleteConfirmView(fake_bot, event, only_while_one_off=True)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.response.edit_message = AsyncMock(
            side_effect=forbidden_error(50013)
        )

        with caplog.at_level("ERROR", logger="gw2bot.events.views"):
            await view.delete.callback(interaction)

        # Nothing was deleted, and the confirmation still carries its buttons,
        # so a guard left set would refuse every retry until it timed out.
        assert store.get_event(event.event_id) is not None
        assert not view._deleting
        assert "Could not acknowledge an event deletion" in caplog.text
        assert event.title not in caplog.text

    async def test_declining_the_deletion_is_logged(
        self,
        fake_bot: Any,
        store: EventStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        event, _ = self._one_off_event(store)
        view = EventDeleteConfirmView(fake_bot, event, only_while_one_off=True)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )

        with caplog.at_level("DEBUG"):
            await view.keep.callback(interaction)

        # Declining is a decision the workflow's trail has to carry too.
        assert "Event deletion declined" in caplog.text
        assert f"event_id={event.event_id}" in caplog.text
        assert event.title not in caplog.text


class TestEventCancelConfirmView:
    async def test_cancel_removes_the_occurrence_and_posts_the_next_one(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = make_posted_recurring_event(store)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=11,
            role=EventRole.DPS,
            assigned_role=EventRole.DPS,
            flex_roles=(),
            waitlisted=False,
        )
        view = EventCancelConfirmView(fake_bot, event, occurrence)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.cancel_occurrence.callback(interaction)

        assert store.get_occurrence(occurrence.occurrence_id) is None
        assert store.get_signups(occurrence.occurrence_id) == []
        assert store.get_event(event.event_id) is not None
        channel.partial_message.delete.assert_awaited_once()
        next_start = next_occurrence_start(
            event.repeat_frequency,
            event.repeat_days,
            occurrence.start_time,
            ZoneInfo("UTC"),
        )
        successor = store.get_event_occurrences(event.event_id)[0]
        assert successor.start_time == next_start
        assert successor.message_id is not None
        assert interaction.edit_original_response.await_args is not None
        content = interaction.edit_original_response.await_args.kwargs[
            "content"
        ]
        assert "continues on" in content
        assert format_event_datetime(next_start, ZoneInfo("UTC")) in content

    async def test_cancel_reports_a_next_occurrence_that_cannot_be_posted(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = make_posted_recurring_event(store)
        channel.send_error = forbidden_error(50013)
        view = EventCancelConfirmView(fake_bot, event, occurrence)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.cancel_occurrence.callback(interaction)

        assert store.get_occurrence(occurrence.occurrence_id) is None
        assert interaction.edit_original_response.await_args is not None
        content = interaction.edit_original_response.await_args.kwargs[
            "content"
        ]
        assert "could not be posted" in content

    async def test_cancel_rejects_users_without_the_role(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = make_posted_recurring_event(store)
        view = EventCancelConfirmView(fake_bot, event, occurrence)
        interaction = make_interaction(message=ephemeral_message())

        await view.cancel_occurrence.callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        assert store.get_occurrence(occurrence.occurrence_id) is not None

    async def test_cancel_ignores_a_racing_second_click(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = make_posted_recurring_event(store)
        view = EventCancelConfirmView(fake_bot, event, occurrence)
        first = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        first.edit_original_response = AsyncMock()

        await view.cancel_occurrence.callback(first)
        assert view._cancelling

        second = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        await view.cancel_occurrence.callback(second)

        second.response.send_message.assert_awaited_once()
        assert (
            "already being cancelled"
            in second.response.send_message.await_args.args[0]
        )

    async def test_a_store_failure_keeps_the_occurrence(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = make_posted_recurring_event(store)
        store.delete_occurrence = MagicMock(  # type: ignore[method-assign]
            side_effect=SQLAlchemyError("boom")
        )
        view = EventCancelConfirmView(fake_bot, event, occurrence)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.cancel_occurrence.callback(interaction)

        assert store.get_occurrence(occurrence.occurrence_id) is not None
        assert interaction.edit_original_response.await_args is not None
        assert (
            "could not be cancelled"
            in interaction.edit_original_response.await_args.kwargs["content"]
        )
        # The guard is released so the commander can try the same button again.
        assert not view._cancelling

    async def test_keep_leaves_the_occurrence_alone(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = make_posted_recurring_event(store)
        view = EventCancelConfirmView(fake_bot, event, occurrence)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )

        await view.keep.callback(interaction)

        assert store.get_occurrence(occurrence.occurrence_id) is not None
        channel.partial_message.delete.assert_not_awaited()
        assert (
            "not cancelled"
            in interaction.response.edit_message.await_args.kwargs["content"]
        )

    async def test_cancel_reports_a_run_that_is_already_gone(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = make_posted_recurring_event(store)
        view = EventCancelConfirmView(fake_bot, event, occurrence)
        # The event is deleted while the confirmation sits open.
        store.delete_event(event.event_id)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.cancel_occurrence.callback(interaction)

        # Cancelling from the stale copies would seed an occurrence for an
        # event that is not there any more.
        assert store.get_event_occurrences(event.event_id) == []
        assert interaction.edit_original_response.await_args is not None
        assert (
            "no longer there"
            in interaction.edit_original_response.await_args.kwargs["content"]
        )

    async def test_cancel_rejects_an_occurrence_that_has_already_run(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        # A confirmation opened just before the run ends and answered after it.
        started = datetime.now(UTC) - timedelta(hours=3)
        event = store.create_event(
            category=EventCategory.FRACTAL,
            title="Weekly clear",
            description="Bring food.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=started,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.WEEKLY,
            repeat_days=(0,),
        )
        occurrence = store.create_occurrence(event.event_id, started)
        store.set_occurrence_message(occurrence.occurrence_id, 1234, 555, 777)
        stored = store.get_occurrence(occurrence.occurrence_id)
        assert stored is not None
        view = EventCancelConfirmView(fake_bot, event, stored)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.cancel_occurrence.callback(interaction)

        # The run already happened, so its roster and post are history rather
        # than something to call off. A series that keeps its occurrences still
        # has the row, which is why the status alone is not enough to tell.
        assert store.get_occurrence(occurrence.occurrence_id) is not None
        channel.partial_message.delete.assert_not_awaited()
        assert interaction.edit_original_response.await_args is not None
        assert (
            "already run"
            in interaction.edit_original_response.await_args.kwargs["content"]
        )

    async def test_cancel_rechecks_the_run_after_acknowledging_the_click(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = make_posted_recurring_event(store)
        view = EventCancelConfirmView(fake_bot, event, occurrence)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        async def retire_the_run(*args: Any, **kwargs: Any) -> None:
            # The run ends inside the round-trip the acknowledgement costs.
            store.set_occurrence_status(
                occurrence.occurrence_id,
                EventStatus.OVER,
            )

        interaction.response.edit_message = AsyncMock(
            side_effect=retire_the_run
        )

        await view.cancel_occurrence.callback(interaction)

        # The guards run after the acknowledgement, so they see the run as it
        # is when the cancellation would act on it rather than as it was when
        # the click arrived.
        assert store.get_occurrence(occurrence.occurrence_id) is not None
        channel.partial_message.delete.assert_not_awaited()
        assert interaction.edit_original_response.await_args is not None
        assert (
            "already run"
            in interaction.edit_original_response.await_args.kwargs["content"]
        )

    async def test_cancel_rejects_an_event_that_no_longer_repeats(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = make_posted_recurring_event(store)
        view = EventCancelConfirmView(fake_bot, event, occurrence)
        # An edit turns the series into a one-off while the confirmation sits
        # open, so the run being cancelled is now all there is of the event.
        store.update_event(
            event_id=event.event_id,
            category=event.category,
            title=event.title,
            description=event.description,
            channel_id=event.channel_id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.cancel_occurrence.callback(interaction)

        # Cancelling would delete the only occurrence and seed nothing, leaving
        # an event no occurrence-based lookup can reach.
        assert store.get_occurrence(occurrence.occurrence_id) is not None
        channel.partial_message.delete.assert_not_awaited()
        assert interaction.edit_original_response.await_args is not None
        assert (
            "no longer repeats"
            in interaction.edit_original_response.await_args.kwargs["content"]
        )

    async def test_keeping_the_occurrence_is_logged(
        self,
        fake_bot: Any,
        store: EventStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        event, occurrence = make_posted_recurring_event(store)
        view = EventCancelConfirmView(fake_bot, event, occurrence)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )

        with caplog.at_level("DEBUG"):
            await view.keep.callback(interaction)

        # Declining is a decision the workflow's trail has to carry too.
        assert "Event cancel declined" in caplog.text
        assert f"occurrence_id={occurrence.occurrence_id}" in caplog.text
        assert event.title not in caplog.text

    async def test_a_failed_acknowledgement_releases_the_guard(
        self,
        fake_bot: Any,
        store: EventStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        event, occurrence = make_posted_recurring_event(store)
        view = EventCancelConfirmView(fake_bot, event, occurrence)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.response.edit_message = AsyncMock(
            side_effect=forbidden_error(50013)
        )

        with caplog.at_level("ERROR", logger="gw2bot.events.views"):
            await view.cancel_occurrence.callback(interaction)

        # The occurrence is untouched and the buttons are still there, so the
        # commander has to be able to click again.
        assert store.get_occurrence(occurrence.occurrence_id) is not None
        assert not view._cancelling
        assert "Could not acknowledge an event cancellation" in caplog.text
        assert event.title not in caplog.text

    async def test_a_failed_result_message_is_logged_without_its_body(
        self,
        fake_bot: Any,
        store: EventStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        event, occurrence = make_posted_recurring_event(store)
        view = EventCancelConfirmView(fake_bot, event, occurrence)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock(
            side_effect=forbidden_error(50013)
        )

        with caplog.at_level("ERROR", logger="gw2bot.events.views"):
            await view.cancel_occurrence.callback(interaction)

        # The cancellation itself went through; only its wording was lost. It
        # must not escape into discord.py's handler, which logs the exception
        # text - Discord's raw response body - rather than an error type.
        assert store.get_occurrence(occurrence.occurrence_id) is None
        assert "Could not report the event cancellation result" in caplog.text
        assert "error_type=Forbidden" in caplog.text
        assert event.title not in caplog.text

    async def test_a_failed_revalidation_is_reported(
        self,
        fake_bot: Any,
        store: EventStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        event, occurrence = make_posted_recurring_event(store)
        view = EventCancelConfirmView(fake_bot, event, occurrence)
        store.get_event = MagicMock(  # type: ignore[method-assign]
            side_effect=SQLAlchemyError("boom")
        )
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        with caplog.at_level("ERROR", logger="gw2bot.events.views"):
            await view.cancel_occurrence.callback(interaction)

        # The buttons went with the acknowledgement, so a commander left on
        # "Cancelling the occurrence…" has no way to tell it never happened.
        assert not view._cancelling
        assert "Could not re-read an event before cancelling" in caplog.text
        assert interaction.edit_original_response.await_args is not None
        assert (
            "could not be cancelled"
            in interaction.edit_original_response.await_args.kwargs["content"]
        )

    async def test_a_failed_decline_message_is_logged_without_its_body(
        self,
        fake_bot: Any,
        store: EventStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        event, occurrence = make_posted_recurring_event(store)
        view = EventCancelConfirmView(fake_bot, event, occurrence)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.response.edit_message = AsyncMock(
            side_effect=forbidden_error(50013)
        )

        with caplog.at_level("DEBUG", logger="gw2bot.events.views"):
            await view.keep.callback(interaction)

        # Nothing changed, but the failure must not escape into discord.py's
        # handler, which logs the exception text - Discord's response body.
        assert store.get_occurrence(occurrence.occurrence_id) is not None
        assert "Event cancel declined" in caplog.text
        assert "Could not answer a declined event cancellation" in caplog.text
        assert "error_type=Forbidden" in caplog.text
        assert event.title not in caplog.text

    def test_the_result_names_a_successor_nothing_will_post(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = make_posted_recurring_event(store)
        view = EventCancelConfirmView(fake_bot, event, occurrence)
        successor = store.create_occurrence(
            event.event_id,
            occurrence.start_time + timedelta(days=7),
        )

        content = view._result_message(
            OccurrenceCancellation(
                successor=successor,
                successor_posted=False,
                retry_pending=False,
            )
        )

        # Nothing will post that run, so the message must say so - and the
        # alternative it offers has to name its cost, because cancelling again
        # deletes the run rather than restoring it.
        assert "could not be recorded" in content
        assert "nothing will post it on its own" in content
        assert "skip that run" in content
        assert "retried automatically" not in content

    async def test_a_duplicate_cancellation_click_is_logged(
        self,
        fake_bot: Any,
        store: EventStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        event, occurrence = make_posted_recurring_event(store)
        view = EventCancelConfirmView(fake_bot, event, occurrence)
        first = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        first.edit_original_response = AsyncMock()
        await view.cancel_occurrence.callback(first)
        second = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )

        with caplog.at_level("DEBUG"):
            await view.cancel_occurrence.callback(second)

        # A skip is a decision the workflow's trail has to carry.
        assert "Skipped a duplicate event cancellation click" in caplog.text
        assert f"occurrence_id={occurrence.occurrence_id}" in caplog.text

    async def test_cancel_logging_keeps_the_event_out_of_the_log(
        self,
        fake_bot: Any,
        store: EventStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        title = "SECRET EVENT TITLE"
        description = "SECRET EVENT DESCRIPTION"
        event = store.create_event(
            category=EventCategory.FRACTAL,
            title=title,
            description=description,
            channel_id=1234,
            leader_discord_id=42,
            start_time=FAR_FUTURE,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.DAILY,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        store.set_occurrence_message(occurrence.occurrence_id, 1234, 555, 777)
        stored = store.get_occurrence(occurrence.occurrence_id)
        assert stored is not None
        group = EventCommands(fake_bot)
        command_interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
        )
        view = EventCancelConfirmView(fake_bot, event, stored)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        with caplog.at_level("DEBUG"):
            await cast(Any, group.cancel.callback)(
                group, command_interaction, event.event_id
            )
            await view.cancel_occurrence.callback(interaction)

        # Both the confirmation and the result name the event, so neither may
        # reach the log.
        assert title not in caplog.text
        assert description not in caplog.text
        # The workflow still has to be traceable end to end.
        assert "Event cancel command invoked" in caplog.text
        assert "Event cancel confirmation opened" in caplog.text
        assert "Cancelled event occurrence" in caplog.text
        assert "Cancelled event occurrence from confirmation" in caplog.text
        assert "Posted event occurrence" in caplog.text


class TestRemindCommand:
    @staticmethod
    def _seat(store: EventStore, occurrence_id: int, user_id: int) -> None:
        store.add_signup(
            occurrence_id=occurrence_id,
            discord_user_id=user_id,
            role=EventRole.DPS,
            assigned_role=EventRole.DPS,
            flex_roles=(),
            waitlisted=False,
        )

    async def test_remind_rejects_users_without_the_create_role(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        group = EventCommands(fake_bot)
        event, occurrence = make_posted_edit_event(store)
        self._seat(store, occurrence.occurrence_id, 11)
        interaction = make_interaction()

        await cast(Any, group.remind.callback)(
            group, interaction, event.event_id
        )

        interaction.response.send_message.assert_awaited_once()
        kwargs = interaction.response.send_message.await_args.kwargs
        assert kwargs["ephemeral"] is True
        channel.thread.send.assert_not_awaited()

    async def test_remind_rejects_unknown_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.remind.callback)(group, interaction, 999)

        interaction.response.send_message.assert_awaited_once()
        assert (
            "does not exist"
            in interaction.response.send_message.await_args.args[0]
        )

    async def test_remind_rejects_a_finished_event(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        group = EventCommands(fake_bot)
        event, occurrence = make_posted_edit_event(store)
        self._seat(store, occurrence.occurrence_id, 11)
        store.set_occurrence_status(
            occurrence.occurrence_id,
            EventStatus.OVER,
        )
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.remind.callback)(
            group, interaction, event.event_id
        )

        assert (
            "is over"
            in interaction.response.send_message.await_args.args[0]
        )
        channel.thread.send.assert_not_awaited()

    async def test_remind_rejects_an_event_nobody_signed_up_for(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        group = EventCommands(fake_bot)
        event, _ = make_posted_edit_event(store)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.remind.callback)(
            group, interaction, event.event_id
        )

        assert (
            "Nobody is signed up"
            in interaction.response.send_message.await_args.args[0]
        )
        channel.thread.send.assert_not_awaited()

    async def test_remind_rejects_an_event_without_a_thread(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        event = make_edit_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        self._seat(store, occurrence.occurrence_id, 11)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.remind.callback)(
            group, interaction, event.event_id
        )

        assert (
            "no thread or forum post"
            in interaction.response.send_message.await_args.args[0]
        )

    async def test_remind_pings_the_roster_in_the_thread(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        group = EventCommands(fake_bot)
        event, occurrence = make_posted_edit_event(store)
        self._seat(store, occurrence.occurrence_id, 11)
        self._seat(store, occurrence.occurrence_id, 22)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.remind.callback)(
            group, interaction, event.event_id
        )

        channel.thread.send.assert_awaited_once()
        call = channel.thread.send.await_args
        assert call is not None
        assert call.args[0] == (
            "<@11> <@22>: Original Title starts "
            f"<t:{int(FAR_FUTURE.timestamp())}:R>"
        )
        # The title is author-written text in the same message, so the ping
        # names its audience instead of letting Discord parse the title.
        allowed = call.kwargs["allowed_mentions"]
        assert allowed.everyone is False
        assert allowed.roles is False
        assert [mention.id for mention in allowed.users] == [11, 22]
        interaction.response.defer.assert_awaited_once()
        interaction.followup.send.assert_awaited_once()
        assert "2 member(s)" in interaction.followup.send.await_args.args[0]

    async def test_remind_refuses_an_occurrence_that_just_ended(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        # An occurrence that has run its course still reads as ONGOING until a
        # maintenance pass persists OVER. Reminding its roster would announce an
        # event that is already behind them, so the end is judged on the clock.
        group = EventCommands(fake_bot)
        event, occurrence = make_ongoing_edit_event(store)
        store.set_occurrence_start_time(
            occurrence.occurrence_id,
            datetime.now(UTC) - timedelta(minutes=event.duration_minutes + 5),
        )
        self._seat(store, occurrence.occurrence_id, 11)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.remind.callback)(
            group, interaction, event.event_id
        )

        assert (
            "is over"
            in interaction.response.send_message.await_args.args[0]
        )
        channel.thread.send.assert_not_awaited()

    async def test_remind_does_not_consume_the_automatic_reminders(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        event, occurrence = make_posted_edit_event(store)
        self._seat(store, occurrence.occurrence_id, 11)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.remind.callback)(
            group, interaction, event.event_id
        )

        assert (
            store.get_handled_reminder_offsets(occurrence.occurrence_id)
            == set()
        )

    async def test_remind_reports_a_failed_ping(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        group = EventCommands(fake_bot)
        event, occurrence = make_posted_edit_event(store)
        self._seat(store, occurrence.occurrence_id, 11)
        channel.thread.send = AsyncMock(side_effect=forbidden_error(50013))
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.remind.callback)(
            group, interaction, event.event_id
        )

        interaction.followup.send.assert_awaited_once()
        assert (
            "could not be sent"
            in interaction.followup.send.await_args.args[0]
        )


class TestEventDeleteConfirmView:
    async def test_delete_removes_event_rows_and_message(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = make_posted_edit_event(store)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=11,
            role=EventRole.DPS,
            assigned_role=EventRole.DPS,
            flex_roles=(),
            waitlisted=False,
        )
        view = EventDeleteConfirmView(fake_bot, event)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.delete.callback(interaction)

        assert store.get_event(event.event_id) is None
        assert store.get_occurrence(occurrence.occurrence_id) is None
        assert store.get_signups(occurrence.occurrence_id) == []
        channel.partial_message.delete.assert_awaited_once()
        assert interaction.edit_original_response.await_args is not None
        assert (
            "was deleted"
            in interaction.edit_original_response.await_args.kwargs["content"]
        )

    async def test_delete_rejects_users_without_the_role(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, _ = make_posted_edit_event(store)
        view = EventDeleteConfirmView(fake_bot, event)
        interaction = make_interaction(message=ephemeral_message())

        await view.delete.callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        assert store.get_event(event.event_id) is not None

    async def test_delete_ignores_a_racing_second_click(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, _ = make_posted_edit_event(store)
        view = EventDeleteConfirmView(fake_bot, event)
        first = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        first.edit_original_response = AsyncMock()

        await view.delete.callback(first)
        assert view._deleting

        second = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        await view.delete.callback(second)

        second.response.send_message.assert_awaited_once()
        assert (
            "already being deleted"
            in second.response.send_message.await_args.args[0]
        )

    async def test_keep_cancels_without_deleting(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, _ = make_posted_edit_event(store)
        view = EventDeleteConfirmView(fake_bot, event)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )

        await view.keep.callback(interaction)

        assert store.get_event(event.event_id) is not None
        interaction.response.edit_message.assert_awaited_once()
        assert (
            "not deleted"
            in interaction.response.edit_message.await_args.kwargs["content"]
        )


def picked_users(*discord_user_ids: int) -> list[int]:
    # The roster picker hands back the ids it put in its option values.
    return list(discord_user_ids)


def select_option_values(view: RemoveSignupsView) -> list[str]:
    select = next(
        item
        for item in view.children
        if isinstance(item, RemoveSignupsSelect)
    )
    return [option.value for option in select.options]


def nav_button(view: RemoveSignupsView, label: str) -> Any:
    return next(
        item
        for item in view.children
        if isinstance(item, discord.ui.Button) and item.label == label
    )


class TestRemoveSignups:
    def make_repeating_roster(self, store: EventStore) -> Any:
        # Same roster as make_full_roster, on a series that has a next
        # occurrence for automatic sign-up to act on.
        event = store.create_event(
            category=EventCategory.FRACTAL,
            title="Original Title",
            description="Original description.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=FAR_FUTURE,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.DAILY,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        store.set_occurrence_message(occurrence.occurrence_id, 1234, 555, 777)
        self.seat_roster(store, occurrence)
        return event, occurrence

    def seat_roster(self, store: EventStore, occurrence: Any) -> None:
        assignments = [
            (1, EventRole.QUICKNESS_HEAL, False),
            (2, EventRole.DPS, False),
            (3, EventRole.DPS, False),
            (4, EventRole.DPS, False),
            (5, EventRole.DPS, False),
            (6, EventRole.DPS, True),
        ]
        for user_id, role, waitlisted in assignments:
            store.add_signup(
                occurrence_id=occurrence.occurrence_id,
                discord_user_id=user_id,
                role=role,
                assigned_role=None if waitlisted else role,
                flex_roles=(),
                waitlisted=waitlisted,
            )

    def make_full_roster(self, store: EventStore) -> Any:
        # Fractal capacity is 1 healer and 4 DPS, so this roster is full and
        # user 6 lands on the waitlist behind it.
        event, occurrence = make_posted_edit_event(store)
        assignments = [
            (1, EventRole.QUICKNESS_HEAL, False),
            (2, EventRole.DPS, False),
            (3, EventRole.DPS, False),
            (4, EventRole.DPS, False),
            (5, EventRole.DPS, False),
            (6, EventRole.DPS, True),
        ]
        for user_id, role, waitlisted in assignments:
            store.add_signup(
                occurrence_id=occurrence.occurrence_id,
                discord_user_id=user_id,
                role=role,
                assigned_role=None if waitlisted else role,
                flex_roles=(),
                waitlisted=waitlisted,
            )
        return event, occurrence

    def make_remove_view(
        self,
        fake_bot: Any,
        event: Any,
        occurrence: Any,
        names: dict[int, str | None] | None = None,
        page: int = 0,
    ) -> RemoveSignupsView:
        draft = draft_from_event(
            event,
            ZoneInfo("UTC"),
            start_time_override=occurrence.start_time,
        )
        signups = fake_bot.event_store.get_signups(occurrence.occurrence_id)
        if names is None:
            names = {
                signup.discord_user_id: f"User {signup.discord_user_id}"
                for signup in signups
            }
        return RemoveSignupsView(
            fake_bot,
            draft,
            occurrence,
            signups,
            names,
            page,
        )

    def make_remove_interaction(self) -> Any:
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()
        return interaction

    def test_edit_preview_offers_the_remove_button(
        self,
        fake_bot: Any,
    ) -> None:
        draft = draft_from_event(
            make_edit_event(EventStore(":memory:")),
            ZoneInfo("UTC"),
        )
        view = EventEditConfirmView(fake_bot, draft)

        labels = [
            item.label
            for item in view.children
            if isinstance(item, discord.ui.Button)
        ]
        assert "Remove sign-ups" in labels

    async def test_button_reports_an_empty_roster(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, _ = make_posted_edit_event(store)
        draft = draft_from_event(event, ZoneInfo("UTC"))
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )

        await view.remove_signups.callback(interaction)

        interaction.response.edit_message.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()
        assert (
            "Nobody is signed up"
            in interaction.response.send_message.await_args.args[0]
        )

    async def test_button_requires_the_create_role(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, _ = self.make_full_roster(store)
        draft = draft_from_event(event, ZoneInfo("UTC"))
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(message=ephemeral_message())

        await view.remove_signups.callback(interaction)

        interaction.response.edit_message.assert_not_awaited()
        assert (
            "required role"
            in interaction.response.send_message.await_args.args[0]
        )

    async def test_button_opens_the_member_picker(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, _ = self.make_full_roster(store)
        draft = draft_from_event(event, ZoneInfo("UTC"))
        view = EventEditConfirmView(fake_bot, draft)
        interaction = self.make_remove_interaction()

        await view.remove_signups.callback(interaction)

        kwargs = interaction.edit_original_response.await_args.kwargs
        picker = kwargs["view"]
        assert isinstance(picker, RemoveSignupsView)
        select = next(
            item
            for item in picker.children
            if isinstance(item, RemoveSignupsSelect)
        )
        # Only the roster, never the rest of the guild, and one pick per
        # member on it.
        assert [option.value for option in select.options] == [
            "1",
            "2",
            "3",
            "4",
            "5",
            "6",
        ]
        assert select.min_values == 1
        assert select.max_values == 6
        # Names are resolved through Discord because the bot has no member
        # cache, and the seating each member holds rides along as the
        # description.
        assert [option.label for option in select.options] == [
            f"User {user_id}" for user_id in (1, 2, 3, 4, 5, 6)
        ]
        assert select.options[0].description == "Signed up as Quickness Heal"
        assert select.options[5].description == "Waitlisted"
        # The roster embed stays on screen: it shows the seating that the
        # picker's one-line descriptions cannot.
        embeds = kwargs["embeds"]
        assert len(embeds) == 1
        rendered = "\n".join(
            field.value for field in embeds[0].fields if field.value
        )
        for user_id in (1, 2, 3, 4, 5, 6):
            assert f"<@{user_id}>" in rendered

    async def test_picker_drops_members_who_left_the_server(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # The picker's name lookups double as the membership check, so anyone
        # who left is off the roster before it is drawn - a leader is never
        # offered a seat holder who cannot even see the event.
        event, occurrence = self.make_full_roster(store)
        guild = FakeGuild(
            {user_id: f"User {user_id}" for user_id in (1, 2, 3, 4, 6)}
        )
        draft = draft_from_event(event, ZoneInfo("UTC"))
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
            guild=guild,
        )
        interaction.edit_original_response = AsyncMock()

        await view.remove_signups.callback(interaction)

        assert store.get_signup(occurrence.occurrence_id, 5) is None
        assert interaction.edit_original_response.await_args is not None
        kwargs = interaction.edit_original_response.await_args.kwargs
        select = next(
            item
            for item in kwargs["view"].children
            if isinstance(item, RemoveSignupsSelect)
        )
        assert [option.value for option in select.options] == [
            "1",
            "2",
            "3",
            "4",
            "6",
        ]
        assert "left the server" in kwargs["content"]
        # The seat user 5 vacated goes to the waitlisted member behind them.
        promoted = store.get_signup(occurrence.occurrence_id, 6)
        assert promoted is not None
        assert not promoted.waitlisted

    async def test_picker_reports_an_entirely_departed_roster(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_full_roster(store)
        draft = draft_from_event(event, ZoneInfo("UTC"))
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
            guild=FakeGuild({}),
        )
        interaction.edit_original_response = AsyncMock()

        await view.remove_signups.callback(interaction)

        assert store.get_signups(occurrence.occurrence_id) == []
        assert interaction.edit_original_response.await_args is not None
        kwargs = interaction.edit_original_response.await_args.kwargs
        # Nothing left to pick from, so the picker is not drawn at all.
        assert kwargs["view"] is None
        assert "left the server" in kwargs["content"]

    async def test_picker_only_lists_members_who_are_signed_up(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # The whole point of the roster-derived picker: a member of the guild
        # who never signed up must not be selectable.
        event, _ = self.make_full_roster(store)
        draft = draft_from_event(event, ZoneInfo("UTC"))
        view = EventEditConfirmView(fake_bot, draft)
        interaction = self.make_remove_interaction()

        await view.remove_signups.callback(interaction)

        picker = interaction.edit_original_response.await_args.kwargs["view"]
        assert "99" not in select_option_values(picker)

    async def test_picker_falls_back_to_the_id_when_a_name_is_unavailable(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # A failed lookup must not drop the option: an unnamed member is still
        # removable, a missing one is not.
        event, occurrence = self.make_full_roster(store)
        fake_bot.fetch_user_errors[3] = not_found_error()
        draft = draft_from_event(event, ZoneInfo("UTC"))
        view = EventEditConfirmView(fake_bot, draft)
        interaction = self.make_remove_interaction()

        await view.remove_signups.callback(interaction)

        picker = interaction.edit_original_response.await_args.kwargs["view"]
        select = next(
            item
            for item in picker.children
            if isinstance(item, RemoveSignupsSelect)
        )
        labels = {option.value: option.label for option in select.options}
        assert labels["3"] == "Member 3"
        assert labels["2"] == "User 2"

    def test_picker_pages_a_roster_past_the_select_cap(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # A WvW roster seats 50, but a select holds at most 25 options.
        event, occurrence = make_posted_edit_event(store)
        for user_id in range(1, 31):
            store.add_signup(
                occurrence_id=occurrence.occurrence_id,
                discord_user_id=user_id,
                role=None,
                assigned_role=None,
                flex_roles=(),
                waitlisted=False,
            )
        view = self.make_remove_view(fake_bot, event, occurrence)

        assert view.page_count == 2
        assert len(select_option_values(view)) == 25
        assert select_option_values(view) == [
            str(user_id) for user_id in range(1, 26)
        ]
        assert nav_button(view, "Previous").disabled
        assert not nav_button(view, "Next").disabled
        assert "page 1 of 2" in view.prompt()

    def test_picker_hides_navigation_for_a_single_page(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_full_roster(store)
        view = self.make_remove_view(fake_bot, event, occurrence)

        labels = [
            item.label
            for item in view.children
            if isinstance(item, discord.ui.Button)
        ]
        assert labels == ["Back"]
        assert "page 1 of" not in view.prompt()

    async def test_next_page_renders_the_rest_of_the_roster(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = make_posted_edit_event(store)
        for user_id in range(1, 31):
            store.add_signup(
                occurrence_id=occurrence.occurrence_id,
                discord_user_id=user_id,
                role=None,
                assigned_role=None,
                flex_roles=(),
                waitlisted=False,
            )
        view = self.make_remove_view(fake_bot, event, occurrence)
        interaction = self.make_remove_interaction()

        await nav_button(view, "Next").callback(interaction)

        kwargs = interaction.response.edit_message.await_args.kwargs
        following = kwargs["view"]
        assert isinstance(following, RemoveSignupsView)
        assert following.page == 1
        assert select_option_values(following) == [
            str(user_id) for user_id in range(26, 31)
        ]
        assert not nav_button(following, "Previous").disabled
        assert nav_button(following, "Next").disabled
        assert "page 2 of 2" in kwargs["content"]

    async def test_removal_frees_the_slot_and_promotes_the_waitlist(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = self.make_full_roster(store)
        view = self.make_remove_view(fake_bot, event, occurrence)
        interaction = self.make_remove_interaction()

        await view.remove(interaction, picked_users(2))

        assert store.get_signup(occurrence.occurrence_id, 2) is None
        promoted = store.get_signup(occurrence.occurrence_id, 6)
        assert promoted is not None
        assert not promoted.waitlisted
        assert promoted.assigned_role is EventRole.DPS
        # The removed member is dropped from the event thread and the public
        # message is re-rendered against the new roster.
        channel.thread.remove_user.assert_awaited_once()
        channel.partial_message.edit.assert_awaited()
        # The preview comes back with the updated roster and the edit controls.
        kwargs = interaction.edit_original_response.await_args.kwargs
        assert "Removed <@2>" in kwargs["content"]
        assert "<@6> moved up from the waitlist." in kwargs["content"]
        assert isinstance(kwargs["view"], EventEditConfirmView)
        assert len(kwargs["embeds"]) == 2

    async def test_removal_takes_several_members_at_once(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_full_roster(store)
        view = self.make_remove_view(fake_bot, event, occurrence)
        interaction = self.make_remove_interaction()

        await view.remove(interaction, picked_users(2, 3))

        assert store.get_signup(occurrence.occurrence_id, 2) is None
        assert store.get_signup(occurrence.occurrence_id, 3) is None
        # Only one waitlisted member existed, so only one seat is refilled.
        remaining = store.get_signups(occurrence.occurrence_id)
        assert {signup.discord_user_id for signup in remaining} == {1, 4, 5, 6}
        assert not any(signup.waitlisted for signup in remaining)
        content = interaction.edit_original_response.await_args.kwargs[
            "content"
        ]
        assert "Removed <@2>, <@3>" in content

    async def test_promoting_then_removing_the_same_member_is_not_reported(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # Picking a seated member (2) together with the waitlisted member (6):
        # removing 2 promotes 6, and 6 is then removed by the next iteration.
        # 6 ends up off the roster, so the summary must not also claim they
        # moved up from the waitlist.
        event, occurrence = self.make_full_roster(store)
        view = self.make_remove_view(fake_bot, event, occurrence)
        interaction = self.make_remove_interaction()

        await view.remove(interaction, picked_users(2, 6))

        assert store.get_signup(occurrence.occurrence_id, 2) is None
        assert store.get_signup(occurrence.occurrence_id, 6) is None
        remaining = store.get_signups(occurrence.occurrence_id)
        assert {signup.discord_user_id for signup in remaining} == {1, 3, 4, 5}
        content = interaction.edit_original_response.await_args.kwargs[
            "content"
        ]
        assert "Removed <@2>, <@6>" in content
        assert "moved up from the waitlist" not in content

    async def test_multiple_removals_send_one_merged_thread_ping(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = self.make_full_roster(store)
        view = self.make_remove_view(fake_bot, event, occurrence)
        interaction = self.make_remove_interaction()

        await view.remove(interaction, picked_users(2, 3))

        # Each removal resettles the roster separately, but the leader made
        # one edit, so the thread hears one merged announcement.
        channel.thread.send.assert_awaited_once()
        send = channel.thread.send.await_args
        assert send is not None
        content = send.args[0]
        assert "<@6>" in content
        assert "moved up from the waitlist" in content

    async def test_promoted_then_removed_member_is_not_pinged(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = self.make_full_roster(store)
        view = self.make_remove_view(fake_bot, event, occurrence)
        interaction = self.make_remove_interaction()

        await view.remove(interaction, picked_users(2, 6))

        # 6 was promoted by 2's removal and then removed themselves; nothing
        # about the net result is worth announcing.
        channel.thread.send.assert_not_awaited()

    async def test_removal_reports_members_who_were_not_signed_up(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_full_roster(store)
        view = self.make_remove_view(fake_bot, event, occurrence)
        interaction = self.make_remove_interaction()

        await view.remove(interaction, picked_users(2, 99))

        assert store.get_signup(occurrence.occurrence_id, 2) is None
        content = interaction.edit_original_response.await_args.kwargs[
            "content"
        ]
        assert "Removed <@2>" in content
        assert "<@99> was not signed up" in content

    async def test_removal_requires_the_create_role(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_full_roster(store)
        view = self.make_remove_view(fake_bot, event, occurrence)
        interaction = make_interaction(message=ephemeral_message())
        interaction.edit_original_response = AsyncMock()

        await view.remove(interaction, picked_users(2))

        assert store.get_signup(occurrence.occurrence_id, 2) is not None
        interaction.edit_original_response.assert_not_awaited()
        assert (
            "required role"
            in interaction.response.send_message.await_args.args[0]
        )

    async def test_removal_after_the_event_ended_keeps_the_roster(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # The picker can sit open past the end of the event; a finished roster
        # is history, and promoting off its waitlist would rewrite it.
        event, occurrence = self.make_full_roster(store)
        store.set_occurrence_start_time(
            occurrence.occurrence_id,
            datetime.now(UTC) - timedelta(hours=3),
        )
        refetched = store.get_occurrence(occurrence.occurrence_id)
        assert refetched is not None
        occurrence = refetched
        view = self.make_remove_view(fake_bot, event, occurrence)
        interaction = self.make_remove_interaction()

        await view.remove(interaction, picked_users(2))

        assert store.get_signup(occurrence.occurrence_id, 2) is not None
        waitlisted = store.get_signup(occurrence.occurrence_id, 6)
        assert waitlisted is not None
        assert waitlisted.waitlisted
        interaction.edit_original_response.assert_not_awaited()
        assert (
            "already ended"
            in interaction.response.edit_message.await_args.kwargs["content"]
        )

    async def test_removal_stops_when_the_event_ends_mid_loop(
        self,
        fake_bot: Any,
        store: EventStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The end check runs once before the loop, but remove_signup awaits
        # Discord I/O between members, so the event can cross its end partway
        # through. Every member still pending when it does must be left alone.
        event, occurrence = self.make_full_roster(store)
        view = self.make_remove_view(fake_bot, event, occurrence)
        interaction = self.make_remove_interaction()

        # False for the pre-loop check and the first iteration, then True: the
        # event ends right after the first member is removed.
        calls = {"count": 0}

        def fake_ended(_event: Any, _occurrence: Any, _now: Any) -> bool:
            calls["count"] += 1
            return calls["count"] > 2

        monkeypatch.setattr(
            "gw2bot.events.views.occurrence_has_ended",
            fake_ended,
        )

        await view.remove(interaction, picked_users(2, 3, 4))

        # Only the first pick, applied while the event was still live, is gone;
        # the members pending when it ended stay signed up.
        assert store.get_signup(occurrence.occurrence_id, 2) is None
        assert store.get_signup(occurrence.occurrence_id, 3) is not None
        assert store.get_signup(occurrence.occurrence_id, 4) is not None
        kwargs = interaction.edit_original_response.await_args.kwargs
        # The edit session is void once the event ends, so no preview returns.
        assert kwargs["view"] is None
        assert "Removed <@2>" in kwargs["content"]
        assert "ended before" in kwargs["content"]
        assert "<@3>" in kwargs["content"]
        assert "<@4>" in kwargs["content"]

    async def test_removal_reports_a_deleted_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_full_roster(store)
        view = self.make_remove_view(fake_bot, event, occurrence)
        store.delete_event(event.event_id)
        interaction = self.make_remove_interaction()

        await view.remove(interaction, picked_users(2))

        interaction.edit_original_response.assert_not_awaited()
        assert (
            "no longer exists"
            in interaction.response.edit_message.await_args.kwargs["content"]
        )

    async def test_back_returns_to_the_edit_preview(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_full_roster(store)
        view = self.make_remove_view(fake_bot, event, occurrence)
        interaction = self.make_remove_interaction()

        await nav_button(view, "Back").callback(interaction)

        assert store.get_signup(occurrence.occurrence_id, 2) is not None
        kwargs = interaction.response.edit_message.await_args.kwargs
        assert isinstance(kwargs["view"], EventEditConfirmView)

    async def test_removed_members_are_told_by_direct_message(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # The thread announcement never reaches them: the removal already took
        # them out of the event thread.
        event, occurrence = self.make_full_roster(store)
        view = self.make_remove_view(fake_bot, event, occurrence)
        interaction = self.make_remove_interaction()

        await view.remove(interaction, picked_users(2))

        fake_bot.users[2].send.assert_awaited_once()
        content = fake_bot.users[2].send.await_args.args[0]
        assert "Original Title" in content
        assert f"<t:{int(occurrence.start_time.timestamp())}:F>" in content

    async def test_members_who_were_not_signed_up_are_not_messaged(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_full_roster(store)
        view = self.make_remove_view(fake_bot, event, occurrence)
        interaction = self.make_remove_interaction()

        await view.remove(interaction, picked_users(99))

        assert 99 not in fake_bot.users

    async def test_removal_disables_auto_signup_and_says_so(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # Without this the member is seated again as soon as the next
        # occurrence of the series is seeded.
        event, occurrence = self.make_repeating_roster(store)
        store.set_auto_signup(
            event.event_id,
            2,
            AutoSignupChoice.YES,
            EventRole.DPS,
            (),
        )
        view = self.make_remove_view(fake_bot, event, occurrence)
        interaction = self.make_remove_interaction()

        await view.remove(interaction, picked_users(2))

        stored = store.get_auto_signup(event.event_id, 2)
        assert stored is not None
        assert stored.choice is AutoSignupChoice.NO
        content = fake_bot.users[2].send.await_args.args[0]
        assert "Automatic sign-up for this event has been turned off" in content

    async def test_removal_leaves_a_declined_auto_signup_alone(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # NEVER_ASK already means automatic sign-up is off; overwriting it with
        # NO would quietly re-enable the prompt the member switched off.
        event, occurrence = self.make_repeating_roster(store)
        store.set_auto_signup(
            event.event_id,
            2,
            AutoSignupChoice.NEVER_ASK,
            None,
            (),
        )
        view = self.make_remove_view(fake_bot, event, occurrence)
        interaction = self.make_remove_interaction()

        await view.remove(interaction, picked_users(2))

        stored = store.get_auto_signup(event.event_id, 2)
        assert stored is not None
        assert stored.choice is AutoSignupChoice.NEVER_ASK
        content = fake_bot.users[2].send.await_args.args[0]
        assert "Automatic sign-up" not in content

    async def test_removal_dm_omits_auto_signup_for_a_one_off_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_full_roster(store)
        store.set_auto_signup(
            event.event_id,
            2,
            AutoSignupChoice.YES,
            EventRole.DPS,
            (),
        )
        view = self.make_remove_view(fake_bot, event, occurrence)
        interaction = self.make_remove_interaction()

        await view.remove(interaction, picked_users(2))

        content = fake_bot.users[2].send.await_args.args[0]
        assert "Automatic sign-up" not in content

    async def test_a_closed_dm_is_reported_and_the_rest_still_go_out(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # A member can block the bot or close their DMs. That must not abort
        # the removal or the notices behind it.
        event, occurrence = self.make_full_roster(store)
        fake_bot.dm_errors[2] = forbidden_error(50007)
        view = self.make_remove_view(fake_bot, event, occurrence)
        interaction = self.make_remove_interaction()

        await view.remove(interaction, picked_users(2, 3))

        assert store.get_signup(occurrence.occurrence_id, 2) is None
        assert store.get_signup(occurrence.occurrence_id, 3) is None
        fake_bot.users[3].send.assert_awaited_once()
        content = interaction.edit_original_response.await_args.kwargs[
            "content"
        ]
        assert "Removed <@2>, <@3>" in content
        assert "Could not send a direct message to <@2>" in content

    async def test_an_occurrence_seeded_during_the_removal_is_reconciled(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        # remove_signup refreshes the public message, and a refresh that finds
        # the message gone (or that crosses the occurrence's end) seeds the
        # next occurrence from the preference that is still enabled at that
        # point. The DM promises the member will not be signed up again, so
        # that seat has to go with the preference.
        event, occurrence = self.make_repeating_roster(store)
        for user_id in (2, 5):
            store.set_auto_signup(
                event.event_id,
                user_id,
                AutoSignupChoice.YES,
                EventRole.DPS,
                (),
            )
        channel.partial_message.edit = AsyncMock(side_effect=not_found_error())
        view = self.make_remove_view(fake_bot, event, occurrence)
        interaction = self.make_remove_interaction()

        await view.remove(interaction, picked_users(2))

        seeded = [
            following
            for following in store.get_event_occurrences(event.event_id)
            if following.start_time > occurrence.start_time
        ]
        assert len(seeded) == 1
        # User 5 keeps their automatic seat, which is what proves the seeding
        # (and its auto sign-ups) really ran during the removal.
        assert store.get_signup(seeded[0].occurrence_id, 5) is not None
        assert store.get_signup(seeded[0].occurrence_id, 2) is None
        content = fake_bot.users[2].send.await_args.args[0]
        assert "Automatic sign-up for this event has been turned off" in content

    async def test_a_seat_on_a_posted_next_occurrence_is_named_in_the_dm(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # A posted roster may hold a deliberate signup, so the seat stands -
        # but the DM must not claim the member is off every future roster.
        event, occurrence = self.make_repeating_roster(store)
        store.set_auto_signup(
            event.event_id,
            2,
            AutoSignupChoice.YES,
            EventRole.DPS,
            (),
        )
        following = store.create_occurrence(
            event.event_id,
            occurrence.start_time + timedelta(days=1),
        )
        store.set_occurrence_message(following.occurrence_id, 1234, 556, 778)
        store.add_signup(
            occurrence_id=following.occurrence_id,
            discord_user_id=2,
            role=EventRole.DPS,
            assigned_role=EventRole.DPS,
            flex_roles=(),
            waitlisted=False,
        )
        view = self.make_remove_view(fake_bot, event, occurrence)
        interaction = self.make_remove_interaction()

        await view.remove(interaction, picked_users(2))

        assert store.get_signup(following.occurrence_id, 2) is not None
        content = fake_bot.users[2].send.await_args.args[0]
        assert "still signed up for the next occurrence" in content
        assert f"<t:{int(following.start_time.timestamp())}:F>" in content

    async def test_a_closed_dm_still_disables_auto_signup(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_repeating_roster(store)
        store.set_auto_signup(
            event.event_id,
            2,
            AutoSignupChoice.YES,
            EventRole.DPS,
            (),
        )
        fake_bot.dm_errors[2] = forbidden_error(50007)
        view = self.make_remove_view(fake_bot, event, occurrence)
        interaction = self.make_remove_interaction()

        await view.remove(interaction, picked_users(2))

        stored = store.get_auto_signup(event.event_id, 2)
        assert stored is not None
        assert stored.choice is AutoSignupChoice.NO

    async def test_removal_logging_keeps_the_event_out_of_the_log(
        self,
        fake_bot: Any,
        store: EventStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        title = "SECRET EVENT TITLE"
        description = "SECRET EVENT DESCRIPTION"
        event = store.create_event(
            category=EventCategory.FRACTAL,
            title=title,
            description=description,
            channel_id=1234,
            leader_discord_id=42,
            start_time=FAR_FUTURE,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.DAILY,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        store.set_occurrence_message(occurrence.occurrence_id, 1234, 555, 777)
        self.seat_roster(store, occurrence)
        store.set_auto_signup(
            event.event_id,
            2,
            AutoSignupChoice.YES,
            EventRole.DPS,
            (),
        )
        fake_bot.dm_errors[3] = forbidden_error(50007)
        view = self.make_remove_view(fake_bot, event, occurrence)
        interaction = self.make_remove_interaction()

        with caplog.at_level("DEBUG"):
            await view.remove(interaction, picked_users(2, 3))

        # The direct message carries the event title, so neither the event nor
        # the message body may reach the log.
        assert title not in caplog.text
        assert description not in caplog.text
        assert "turned off" not in caplog.text
        # The workflow still has to be traceable end to end.
        assert "Disabled auto signup on removal" in caplog.text
        assert "Sending direct message" in caplog.text
        assert "Delivered direct message" in caplog.text
        assert "Could not deliver a direct message" in caplog.text
        assert "Applied roster removal" in caplog.text


def add_select(view: AddSignupsView) -> AddSignupsSelect:
    return next(
        item for item in view.children if isinstance(item, AddSignupsSelect)
    )


def add_back_button(view: AddSignupsView) -> Any:
    return next(
        item
        for item in view.children
        if isinstance(item, discord.ui.Button) and item.label == "Back"
    )


class TestAddSignups:
    """Manually putting members on an event's roster from /event edit."""

    def make_event(
        self,
        store: EventStore,
        category: EventCategory = EventCategory.FRACTAL,
        *,
        posted: bool = True,
    ) -> Any:
        event = store.create_event(
            category=category,
            title="Original Title",
            description="Original description.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=FAR_FUTURE,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        if posted:
            store.set_occurrence_message(
                occurrence.occurrence_id,
                1234,
                555,
                777,
            )
            refetched = store.get_occurrence(occurrence.occurrence_id)
            assert refetched is not None
            occurrence = refetched
        return event, occurrence

    def make_draft(self, event: Any, occurrence: Any) -> EventDraft:
        return draft_from_event(
            event,
            ZoneInfo("UTC"),
            start_time_override=occurrence.start_time,
        )

    def make_add_view(
        self,
        fake_bot: Any,
        event: Any,
        occurrence: Any,
    ) -> AddSignupsView:
        return AddSignupsView(
            fake_bot,
            self.make_draft(event, occurrence),
            occurrence,
        )

    def make_add_interaction(
        self,
        guild_id: int | None = 9876,
        guild: Any = None,
    ) -> Any:
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
            guild=guild,
        )
        interaction.guild_id = guild_id
        interaction.edit_original_response = AsyncMock()
        return interaction

    def change_event(
        self,
        store: EventStore,
        event: Any,
        **changes: Any,
    ) -> Any:
        # update_event replaces every field, so the unchanged ones are carried
        # over from the stored event.
        fields = {
            "category": event.category,
            "title": event.title,
            "description": event.description,
            "channel_id": event.channel_id,
            "leader_discord_id": event.leader_discord_id,
            "start_time": event.start_time,
            "duration_minutes": event.duration_minutes,
            "repeat_frequency": event.repeat_frequency,
            "repeat_days": event.repeat_days,
        }
        return store.update_event(
            event_id=event.event_id,
            **{**fields, **changes},
        )

    def seat(
        self,
        store: EventStore,
        occurrence: Any,
        discord_user_id: int,
        role: EventRole | None = EventRole.DPS,
        flex_roles: tuple[EventRole, ...] = (),
        waitlisted: bool = False,
    ) -> None:
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=discord_user_id,
            role=role,
            assigned_role=None if waitlisted else role,
            flex_roles=flex_roles,
            waitlisted=waitlisted,
        )

    def test_edit_preview_offers_the_add_button(
        self,
        fake_bot: Any,
    ) -> None:
        draft = draft_from_event(
            make_edit_event(EventStore(":memory:")),
            ZoneInfo("UTC"),
        )

        labels = [
            item.label
            for item in EventEditConfirmView(fake_bot, draft).children
            if isinstance(item, discord.ui.Button)
        ]
        assert "Add sign-ups" in labels

    def test_roster_editor_offers_the_add_button(
        self,
        fake_bot: Any,
    ) -> None:
        draft = replace(
            draft_from_event(
                make_edit_event(EventStore(":memory:")),
                ZoneInfo("UTC"),
            ),
            roster_only=True,
        )

        labels = [
            item.label
            for item in EventRosterEditView(fake_bot, draft).children
            if isinstance(item, discord.ui.Button)
        ]
        assert "Add sign-ups" in labels

    async def test_button_opens_the_member_search(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_event(store)
        self.seat(store, occurrence, 1)
        view = EventEditConfirmView(
            fake_bot,
            self.make_draft(event, occurrence),
        )
        interaction = self.make_add_interaction()

        await cast(Any, view.add_signups.callback)(interaction)

        kwargs = interaction.response.edit_message.await_args.kwargs
        picker = kwargs["view"]
        assert isinstance(picker, AddSignupsView)
        # Discord's own user select: the commander searches the whole server
        # rather than a list the bot builds, so there are no options to page.
        select = add_select(picker)
        assert select.max_values == ADD_SELECT_MAX_MEMBERS
        assert select.min_values == 1
        # The current roster stays on screen above the picker.
        assert len(kwargs["embeds"]) == 1

    async def test_button_requires_the_create_role(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_event(store)
        view = EventEditConfirmView(
            fake_bot,
            self.make_draft(event, occurrence),
        )
        interaction = make_interaction(message=ephemeral_message())

        await cast(Any, view.add_signups.callback)(interaction)

        interaction.response.edit_message.assert_not_awaited()
        assert (
            "required role"
            in interaction.response.send_message.await_args.args[0]
        )

    async def test_button_refuses_an_ended_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_event(store)
        store.set_occurrence_start_time(
            occurrence.occurrence_id,
            datetime.now(UTC) - timedelta(hours=3),
        )
        view = EventEditConfirmView(
            fake_bot,
            self.make_draft(event, occurrence),
        )
        interaction = self.make_add_interaction()

        await cast(Any, view.add_signups.callback)(interaction)

        kwargs = interaction.response.edit_message.await_args.kwargs
        assert "already ended" in kwargs["content"]
        assert kwargs["view"] is None

    async def test_a_role_event_asks_which_role_to_seat_them_as(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_event(store)
        view = self.make_add_view(fake_bot, event, occurrence)
        interaction = self.make_add_interaction()

        await view.pick(interaction, [11, 12])

        # Nothing is written until the role is answered.
        assert store.get_signups(occurrence.occurrence_id) == []
        kwargs = interaction.response.edit_message.await_args.kwargs
        role_view = kwargs["view"]
        assert isinstance(role_view, AddSignupsRoleView)
        assert "<@11>, <@12>" in kwargs["content"]
        select = next(
            item
            for item in role_view.children
            if isinstance(item, AddSignupsRoleSelect)
        )
        assert [option.value for option in select.options] == [
            role.value for role in EventRole
        ]

    async def test_a_headcount_event_adds_without_asking_for_a_role(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = self.make_event(store, EventCategory.WVW)
        view = self.make_add_view(fake_bot, event, occurrence)
        interaction = self.make_add_interaction()

        await view.pick(interaction, [11, 12])

        seated = store.get_signups(occurrence.occurrence_id)
        assert {signup.discord_user_id for signup in seated} == {11, 12}
        assert not any(signup.waitlisted for signup in seated)
        assert all(signup.role is None for signup in seated)
        # They join the event thread and the public message is re-rendered.
        assert channel.thread.add_user.await_count == 2
        channel.partial_message.edit.assert_awaited()
        kwargs = interaction.edit_original_response.await_args.kwargs
        assert "Added <@11>, <@12> to the roster." in kwargs["content"]
        assert isinstance(kwargs["view"], EventEditConfirmView)
        assert len(kwargs["embeds"]) == 2

    async def test_the_picked_role_seats_every_member(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_event(store)
        role_view = AddSignupsRoleView(
            fake_bot,
            self.make_draft(event, occurrence),
            occurrence,
            event,
            [],
            [11, 12],
        )
        interaction = self.make_add_interaction()

        await role_view.pick(interaction, EventRole.DPS)

        seated = store.get_signups(occurrence.occurrence_id)
        assert {signup.discord_user_id for signup in seated} == {11, 12}
        for signup in seated:
            assert signup.role is EventRole.DPS
            assert signup.assigned_role is EventRole.DPS
            # A commander cannot answer the flex question for someone else, so
            # a manual add carries the picked role alone.
            assert signup.flex_roles == ()
        content = interaction.edit_original_response.await_args.kwargs[
            "content"
        ]
        assert "Added <@11>, <@12> to the roster." in content

    async def test_added_members_are_told_who_added_them(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_event(store, EventCategory.WVW)
        view = self.make_add_view(fake_bot, event, occurrence)
        interaction = self.make_add_interaction()

        await view.pick(interaction, [11])

        fake_bot.users[11].send.assert_awaited_once()
        content = fake_bot.users[11].send.await_args.args[0]
        assert content == (
            "<@42> added you to [Original Title]"
            "(https://discord.com/channels/9876/1234/555)."
        )

    async def test_the_link_points_at_the_channel_the_event_was_posted_to(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # An event whose channel was changed keeps occurrences that were not
        # re-posted where they already live, so the link follows the message.
        event, occurrence = self.make_event(store, EventCategory.WVW)
        self.change_event(store, event, channel_id=4321)
        view = self.make_add_view(fake_bot, event, occurrence)
        interaction = self.make_add_interaction()

        await view.pick(interaction, [11])

        content = fake_bot.users[11].send.await_args.args[0]
        assert "/9876/1234/555)" in content

    async def test_an_unposted_occurrence_names_the_event_without_a_link(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_event(
            store,
            EventCategory.WVW,
            posted=False,
        )
        view = self.make_add_view(fake_bot, event, occurrence)
        interaction = self.make_add_interaction()

        await view.pick(interaction, [11])

        content = fake_bot.users[11].send.await_args.args[0]
        assert content == "<@42> added you to **Original Title**."

    async def test_a_bracket_in_the_title_cannot_break_the_link(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_event(store, EventCategory.WVW)
        self.change_event(store, event, title="CM [exp] run")
        view = self.make_add_view(fake_bot, event, occurrence)
        interaction = self.make_add_interaction()

        await view.pick(interaction, [11])

        content = fake_bot.users[11].send.await_args.args[0]
        assert content == (
            "<@42> added you to [CM \\[exp\\] run]"
            "(https://discord.com/channels/9876/1234/555)."
        )

    async def test_a_closed_dm_is_reported_but_keeps_the_signup(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_event(store, EventCategory.WVW)
        fake_bot.dm_errors[11] = forbidden_error(50007)
        view = self.make_add_view(fake_bot, event, occurrence)
        interaction = self.make_add_interaction()

        await view.pick(interaction, [11, 12])

        assert store.get_signup(occurrence.occurrence_id, 11) is not None
        assert store.get_signup(occurrence.occurrence_id, 12) is not None
        content = interaction.edit_original_response.await_args.kwargs[
            "content"
        ]
        assert "Could not send a direct message to <@11>" in content

    async def test_a_member_who_is_already_signed_up_is_left_alone(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # Re-adding them would rewrite their signup row, and with it the
        # sign-up time their seating priority is read from.
        event, occurrence = self.make_event(store, EventCategory.WVW)
        self.seat(store, occurrence, 11, role=None)
        original = store.get_signup(occurrence.occurrence_id, 11)
        assert original is not None
        view = self.make_add_view(fake_bot, event, occurrence)
        interaction = self.make_add_interaction()

        await view.pick(interaction, [11, 12])

        unchanged = store.get_signup(occurrence.occurrence_id, 11)
        assert unchanged is not None
        assert unchanged.signed_up_at == original.signed_up_at
        fake_bot.users[11].send.assert_not_awaited()
        content = interaction.edit_original_response.await_args.kwargs[
            "content"
        ]
        assert "Added <@12> to the roster." in content
        assert "<@11> was already signed up for this event." in content

    async def test_a_full_roster_puts_the_addition_on_the_waitlist(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_event(store)
        self.seat(store, occurrence, 1, EventRole.QUICKNESS_HEAL)
        for user_id in (2, 3, 4, 5):
            self.seat(store, occurrence, user_id, EventRole.DPS)
        role_view = AddSignupsRoleView(
            fake_bot,
            self.make_draft(event, occurrence),
            occurrence,
            event,
            store.get_signups(occurrence.occurrence_id),
            [11],
        )
        interaction = self.make_add_interaction()

        await role_view.pick(interaction, EventRole.DPS)

        added = store.get_signup(occurrence.occurrence_id, 11)
        assert added is not None
        assert added.waitlisted
        content = interaction.edit_original_response.await_args.kwargs[
            "content"
        ]
        assert (
            "The event is full, so <@11> was added to the waitlist." in content
        )
        assert "Added <@11> to the roster." not in content
        # They are still told, because they are on the event either way.
        fake_bot.users[11].send.assert_awaited_once()

    async def test_several_additions_send_one_merged_thread_ping(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        # A fractal seats one quickness in total, so seating a Quickness DPS
        # flexes the quickness healer onto their alacrity flex role. Two
        # additions must still read as the single edit the leader made.
        event, occurrence = self.make_event(store)
        self.seat(
            store,
            occurrence,
            1,
            EventRole.QUICKNESS_HEAL,
            flex_roles=(EventRole.ALACRITY_HEAL,),
        )
        role_view = AddSignupsRoleView(
            fake_bot,
            self.make_draft(event, occurrence),
            occurrence,
            event,
            store.get_signups(occurrence.occurrence_id),
            [11, 12],
        )
        interaction = self.make_add_interaction()

        await role_view.pick(interaction, EventRole.QUICKNESS_DPS)

        flexed = store.get_signup(occurrence.occurrence_id, 1)
        assert flexed is not None
        assert flexed.assigned_role is EventRole.ALACRITY_HEAL
        channel.thread.send.assert_awaited_once()
        send = channel.thread.send.await_args
        assert send is not None
        assert "<@1>" in send.args[0]

    async def test_a_plain_addition_leaves_the_thread_quiet(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = self.make_event(store, EventCategory.WVW)
        view = self.make_add_view(fake_bot, event, occurrence)
        interaction = self.make_add_interaction()

        await view.pick(interaction, [11, 12])

        # Nobody already on the roster moved, so there is nothing to announce.
        channel.thread.send.assert_not_awaited()

    async def test_addition_requires_the_create_role(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_event(store, EventCategory.WVW)
        view = self.make_add_view(fake_bot, event, occurrence)
        interaction = make_interaction(message=ephemeral_message())
        interaction.guild_id = 9876
        interaction.edit_original_response = AsyncMock()

        await view.pick(interaction, [11])

        assert store.get_signups(occurrence.occurrence_id) == []
        interaction.edit_original_response.assert_not_awaited()
        assert (
            "required role"
            in interaction.response.send_message.await_args.args[0]
        )

    async def test_addition_after_the_event_ended_changes_nothing(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_event(store, EventCategory.WVW)
        view = self.make_add_view(fake_bot, event, occurrence)
        store.set_occurrence_start_time(
            occurrence.occurrence_id,
            datetime.now(UTC) - timedelta(hours=3),
        )
        interaction = self.make_add_interaction()

        await view.pick(interaction, [11])

        assert store.get_signups(occurrence.occurrence_id) == []
        interaction.edit_original_response.assert_not_awaited()
        assert (
            "already ended"
            in interaction.response.edit_message.await_args.kwargs["content"]
        )

    async def test_addition_stops_when_the_event_ends_mid_loop(
        self,
        fake_bot: Any,
        store: EventStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The end check runs once before the loop, but seating a member awaits
        # Discord I/O, so the event can cross its end partway through.
        event, occurrence = self.make_event(store, EventCategory.WVW)
        view = self.make_add_view(fake_bot, event, occurrence)
        interaction = self.make_add_interaction()
        calls = {"count": 0}

        # False for the two picker re-checks and the first iteration, then
        # True: the event ends right after the first member is seated.
        def fake_ended(_event: Any, _occurrence: Any, _now: Any) -> bool:
            calls["count"] += 1
            return calls["count"] > 3

        monkeypatch.setattr(
            "gw2bot.events.views.occurrence_has_ended",
            fake_ended,
        )

        await view.pick(interaction, [11, 12, 13])

        seated = store.get_signups(occurrence.occurrence_id)
        assert {signup.discord_user_id for signup in seated} == {11}
        kwargs = interaction.edit_original_response.await_args.kwargs
        # The edit session is void once the event ends, so no preview returns.
        assert kwargs["view"] is None
        assert "Added <@11> to the roster." in kwargs["content"]
        assert "The event ended before the rest could be added" in (
            kwargs["content"]
        )
        assert "<@12>, <@13> were left off." in kwargs["content"]
        assert "post is no longer available" not in kwargs["content"]

    async def test_addition_stops_when_the_occurrence_is_retired(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        # Seating the first member refreshes the public message. A message that
        # has been deleted retires the occurrence as OVER, so the rest of the
        # batch must not be seated onto a roster nobody can see, nor sent a
        # link to a message that is gone.
        event, occurrence = self.make_event(store, EventCategory.WVW)
        view = self.make_add_view(fake_bot, event, occurrence)
        channel.partial_message.edit = AsyncMock(side_effect=not_found_error())
        interaction = self.make_add_interaction()

        await view.pick(interaction, [11, 12, 13])

        retired = store.get_occurrence(occurrence.occurrence_id)
        assert retired is not None
        assert retired.status is EventStatus.OVER
        seated = store.get_signups(occurrence.occurrence_id)
        assert {signup.discord_user_id for signup in seated} == {11}
        fake_bot.users[12].send.assert_not_awaited()
        fake_bot.users[13].send.assert_not_awaited()
        kwargs = interaction.edit_original_response.await_args.kwargs
        assert kwargs["view"] is None
        assert "Added <@11> to the roster." in kwargs["content"]
        assert "<@12>, <@13> were left off." in kwargs["content"]
        # A retired occurrence is not a finished event, and saying so hides the
        # actionable part: the post the commander is looking for is gone.
        assert "This event's post is no longer available" in kwargs["content"]
        assert "ended before" not in kwargs["content"]

    async def test_addition_stops_when_another_leader_edits_the_category(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        # The category picks the capacity every seat is computed against, and
        # the edit that changes it re-seats the whole roster under the new one.
        # Seating the rest of the batch from the stale event would undo that
        # with the old capacity - here, role-less members onto a fractal.
        event, occurrence = self.make_event(store, EventCategory.WVW)
        view = self.make_add_view(fake_bot, event, occurrence)

        async def another_leaders_edit(**_fields: Any) -> None:
            # Lands while the first member is being seated: seat_signup
            # refreshes the public message partway through the batch.
            self.change_event(store, event, category=EventCategory.FRACTAL)

        channel.partial_message.edit = AsyncMock(
            side_effect=another_leaders_edit
        )
        interaction = self.make_add_interaction()

        await view.pick(interaction, [11, 12, 13])

        seated = store.get_signups(occurrence.occurrence_id)
        assert {signup.discord_user_id for signup in seated} == {11}
        fake_bot.users[12].send.assert_not_awaited()
        fake_bot.users[13].send.assert_not_awaited()
        kwargs = interaction.edit_original_response.await_args.kwargs
        assert kwargs["view"] is None
        assert "Another leader changed this event" in kwargs["content"]
        assert "<@12>, <@13> were left off." in kwargs["content"]

    async def test_a_member_who_left_the_server_is_not_seated(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # The picker, and the role step behind it, can sit open for minutes.
        # Seating someone who has left spends a roster slot on a member who
        # cannot see the event, and the thread add behind it fails quietly.
        event, occurrence = self.make_event(store, EventCategory.WVW)
        view = self.make_add_view(fake_bot, event, occurrence)
        interaction = self.make_add_interaction(
            guild=FakeGuild({11: "Still Here"}),
        )

        await view.pick(interaction, [11, 12])

        seated = store.get_signups(occurrence.occurrence_id)
        assert {signup.discord_user_id for signup in seated} == {11}
        fake_bot.users[12].send.assert_not_awaited()
        content = interaction.edit_original_response.await_args.kwargs[
            "content"
        ]
        assert "Added <@11> to the roster." in content
        assert "<@12> has left the server, so they were not added." in content

    async def test_an_unresolvable_member_is_still_added(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # Only a definite "not a member" may cost someone their seat: a lookup
        # that failed proves nothing, and refusing on it would block every
        # addition whenever Discord is unreachable.
        event, occurrence = self.make_event(store, EventCategory.WVW)
        view = self.make_add_view(fake_bot, event, occurrence)
        guild = FakeGuild({})
        guild.fetch_member = AsyncMock(side_effect=forbidden_error(50001))
        interaction = self.make_add_interaction(guild=guild)

        await view.pick(interaction, [11])

        assert store.get_signup(occurrence.occurrence_id, 11) is not None
        content = interaction.edit_original_response.await_args.kwargs[
            "content"
        ]
        assert "Added <@11> to the roster." in content
        assert "left the server" not in content

    async def test_the_last_seat_retiring_the_event_is_reported(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        # Seating the only member refreshes the public message, which retires
        # the occurrence when that message is gone. No iteration is left to
        # notice, so without a final check the member is sent a link to the
        # deleted message and the commander gets an editable preview back.
        event, occurrence = self.make_event(store, EventCategory.WVW)
        view = self.make_add_view(fake_bot, event, occurrence)
        channel.partial_message.edit = AsyncMock(side_effect=not_found_error())
        interaction = self.make_add_interaction()

        await view.pick(interaction, [11])

        assert store.get_signup(occurrence.occurrence_id, 11) is not None
        # Told they were added, but never pointed at a message that is gone.
        dm = fake_bot.users[11].send.await_args.args[0]
        assert dm == "<@42> added you to **Original Title**."
        assert "discord.com/channels" not in dm
        kwargs = interaction.edit_original_response.await_args.kwargs
        assert kwargs["view"] is None
        assert "Added <@11> to the roster." in kwargs["content"]
        assert (
            "This event's post is no longer available" in kwargs["content"]
        )

    async def test_a_role_answered_for_the_old_category_is_refused(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # The role step is answered against the category the picker opened
        # for. A headcount signup is always role-less, so a role chosen for
        # the old category must not ride through onto one.
        event, occurrence = self.make_event(store)
        role_view = AddSignupsRoleView(
            fake_bot,
            self.make_draft(event, occurrence),
            occurrence,
            event,
            [],
            [11],
        )
        self.change_event(store, event, category=EventCategory.WVW)
        interaction = self.make_add_interaction()

        await role_view.pick(interaction, EventRole.DPS)

        assert store.get_signups(occurrence.occurrence_id) == []
        kwargs = interaction.response.edit_message.await_args.kwargs
        assert "changed this event's category" in kwargs["content"]
        assert kwargs["view"] is None

    async def test_an_event_deleted_while_notices_go_out_is_reported(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # The notices are external deliveries, so /event delete can land while
        # they go out. Edit controls must not come back for a roster that has
        # since gone.
        event, occurrence = self.make_event(store, EventCategory.WVW)
        view = self.make_add_view(fake_bot, event, occurrence)
        deleter = FakeUser(11, "User 11")

        async def delete_during_delivery(_content: Any) -> None:
            store.delete_event(event.event_id)

        deleter.send = AsyncMock(side_effect=delete_during_delivery)
        fake_bot.users[11] = deleter
        interaction = self.make_add_interaction()

        await view.pick(interaction, [11, 12])

        # The deletion cascaded 12's seat away mid-delivery, so they must not
        # be told they joined an event that no longer exists.
        fake_bot.users[12].send.assert_not_awaited()
        kwargs = interaction.edit_original_response.await_args.kwargs
        assert kwargs["view"] is None
        assert kwargs["embeds"] == []
        assert "no longer available" in kwargs["content"]
        # The commander has to know 12 went untold.
        assert "<@12>, so they were not notified." in kwargs["content"]

    async def test_an_unsaved_category_change_is_saved_first(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # The preview offers a category change, and the roster is seated
        # against the saved category. Adding under a pending change would ask
        # the wrong question - a headcount category skips the role step - and
        # the save behind it would rebalance those members into roles the
        # commander never picked.
        event, occurrence = self.make_event(store, EventCategory.WVW)
        draft = self.make_draft(event, occurrence)
        draft.category = EventCategory.FRACTAL
        view = EventEditConfirmView(fake_bot, draft)
        interaction = self.make_add_interaction()

        await cast(Any, view.add_signups.callback)(interaction)

        assert store.get_signups(occurrence.occurrence_id) == []
        kwargs = interaction.response.edit_message.await_args.kwargs
        assert "has not been saved" in kwargs["content"]
        # The preview comes back, so Save changes is still one click away and
        # the pending change survives.
        assert isinstance(kwargs["view"], EventEditConfirmView)
        assert draft.category is EventCategory.FRACTAL

    async def test_the_saved_category_still_opens_the_picker(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        # The guard must only catch a *pending* change: an edit draft is
        # seeded from the stored event, so the ordinary case matches.
        event, occurrence = self.make_event(store, EventCategory.WVW)
        view = EventEditConfirmView(
            fake_bot,
            self.make_draft(event, occurrence),
        )
        interaction = self.make_add_interaction()

        await cast(Any, view.add_signups.callback)(interaction)

        kwargs = interaction.response.edit_message.await_args.kwargs
        assert isinstance(kwargs["view"], AddSignupsView)

    async def test_a_seat_rebalanced_mid_write_is_reported_as_it_stands(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        # A category edit landing while seat_signup awaits Discord re-seats the
        # roster under the new capacity, so the row the write returned can
        # describe a capacity that no longer applies. The summary must
        # describe the member as they now stand, not as they were written.
        from gw2bot.events.posting import rebalance_occurrence_roster

        event, occurrence = self.make_event(store)
        self.seat(store, occurrence, 1, EventRole.QUICKNESS_HEAL)
        for user_id in (2, 3, 4, 5):
            self.seat(store, occurrence, user_id, EventRole.DPS)
        role_view = AddSignupsRoleView(
            fake_bot,
            self.make_draft(event, occurrence),
            occurrence,
            event,
            store.get_signups(occurrence.occurrence_id),
            [11],
        )

        async def another_leaders_edit(**_fields: Any) -> None:
            # /event edit saving a category change does exactly this: store the
            # new category, then re-seat the roster against its capacity.
            widened = self.change_event(
                store,
                event,
                category=EventCategory.WVW,
            )
            rebalance_occurrence_roster(fake_bot, widened, occurrence)

        channel.partial_message.edit = AsyncMock(
            side_effect=another_leaders_edit
        )
        interaction = self.make_add_interaction()

        await role_view.pick(interaction, EventRole.DPS)

        # The fractal was full, so the write waitlisted them; the rebalance
        # onto a 50-seat category then seated them.
        seat = store.get_signup(occurrence.occurrence_id, 11)
        assert seat is not None
        assert not seat.waitlisted
        content = interaction.edit_original_response.await_args.kwargs[
            "content"
        ]
        assert "Added <@11> to the roster." in content
        assert "waitlist" not in content

    async def test_a_concurrent_change_is_reported_with_nobody_left_off(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        # The whole batch was applied, so there is nobody to name - but the
        # preview does not come back, so the commander still has to be told
        # why and that the roster may have moved under them.
        event, occurrence = self.make_event(store, EventCategory.WVW)
        view = self.make_add_view(fake_bot, event, occurrence)

        async def another_leaders_edit(**_fields: Any) -> None:
            self.change_event(store, event, category=EventCategory.FRACTAL)

        channel.partial_message.edit = AsyncMock(
            side_effect=another_leaders_edit
        )
        interaction = self.make_add_interaction()

        await view.pick(interaction, [11])

        assert store.get_signup(occurrence.occurrence_id, 11) is not None
        kwargs = interaction.edit_original_response.await_args.kwargs
        assert kwargs["view"] is None
        assert "Added <@11> to the roster." in kwargs["content"]
        assert "Another leader changed this event" in kwargs["content"]
        # The moves this batch computed were against the old capacity, and the
        # edit announces its own rebalance, so nothing stale is posted.
        channel.thread.send.assert_not_awaited()

    async def test_a_picker_that_outlives_its_event_is_logged(
        self,
        fake_bot: Any,
        store: EventStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        event, occurrence = self.make_event(store, EventCategory.WVW)
        view = self.make_add_view(fake_bot, event, occurrence)
        store.delete_event(event.event_id)
        interaction = self.make_add_interaction()

        with caplog.at_level("DEBUG"):
            await view.pick(interaction, [11])

        assert "Roster addition picker outlived its target" in caplog.text

    async def test_addition_reports_a_deleted_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_event(store, EventCategory.WVW)
        view = self.make_add_view(fake_bot, event, occurrence)
        store.delete_event(event.event_id)
        interaction = self.make_add_interaction()

        await view.pick(interaction, [11])

        interaction.edit_original_response.assert_not_awaited()
        assert (
            "no longer exists"
            in interaction.response.edit_message.await_args.kwargs["content"]
        )

    async def test_back_returns_to_the_edit_preview(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self.make_event(store, EventCategory.WVW)
        view = self.make_add_view(fake_bot, event, occurrence)
        interaction = self.make_add_interaction()

        await add_back_button(view).callback(interaction)

        assert store.get_signups(occurrence.occurrence_id) == []
        kwargs = interaction.response.edit_message.await_args.kwargs
        assert isinstance(kwargs["view"], EventEditConfirmView)

    async def test_addition_logging_keeps_the_event_out_of_the_log(
        self,
        fake_bot: Any,
        store: EventStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        title = "SECRET EVENT TITLE"
        description = "SECRET EVENT DESCRIPTION"
        event = store.create_event(
            category=EventCategory.WVW,
            title=title,
            description=description,
            channel_id=1234,
            leader_discord_id=42,
            start_time=FAR_FUTURE,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        store.set_occurrence_message(occurrence.occurrence_id, 1234, 555, 777)
        refetched = store.get_occurrence(occurrence.occurrence_id)
        assert refetched is not None
        fake_bot.dm_errors[12] = forbidden_error(50007)
        view = self.make_add_view(fake_bot, event, refetched)
        interaction = self.make_add_interaction()

        with caplog.at_level("DEBUG"):
            await view.pick(interaction, [11, 12])

        # The direct message carries the event title and a link to its message,
        # so neither the event nor the message body may reach the log.
        assert title not in caplog.text
        assert description not in caplog.text
        assert "discord.com/channels" not in caplog.text
        # The workflow still has to be traceable end to end.
        assert "Picked members to add" in caplog.text
        assert "Sending direct message" in caplog.text
        assert "Delivered direct message" in caplog.text
        assert "Could not deliver a direct message" in caplog.text
        assert "Applied roster addition" in caplog.text
