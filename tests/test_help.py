from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands

from factories import default_config, forbidden_error, settings_interaction
from gw2bot.bot import Gw2Bot
from gw2bot.config import Config
from gw2bot.core.command_access import (
    AnyOf,
    Everyone,
    RoleSetting,
    ServerAdministrator,
    access_extras,
    declared_access,
    iter_role_settings,
)
from gw2bot.help import build_help_messages
from gw2bot.help.commands import handle_help_command
from gw2bot.help.pages import (
    HELP_DESCRIPTION_LIMIT,
    NO_COMMANDS_MESSAGE,
    registered_commands,
)
from gw2bot.help.views import HelpPageButton, HelpPagerView

UNRELATED_ROLE_ID = 42


def _caller(*role_ids: int, user_id: int = 1234, administrator: bool = False) -> Any:
    return SimpleNamespace(
        id=user_id,
        roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
        guild_permissions=SimpleNamespace(administrator=administrator),
    )


def _guild(owner_id: int = 999) -> Any:
    return SimpleNamespace(id=5678, owner_id=owner_id)


def _distinct_role_config(tmp_path: Path, **overrides: Any) -> Config:
    """Config with every gating role different, so no role implies another.

    The shipped defaults give raffle_addticket and event_create the same role,
    which would hide a check reading the wrong one.
    """
    values: dict[str, Any] = {
        "raffle_db_path": str(tmp_path / "gw2bot.db"),
        "raffle_draw_role_id": 101,
        "raffle_addticket_role_id": 102,
        "raffle_officer_role_id": 103,
        "event_create_role_id": 104,
    }
    values.update(overrides)
    return default_config(**values)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return _distinct_role_config(tmp_path)


@pytest.fixture
def bot(config: Config) -> Gw2Bot:
    return Gw2Bot(config)


def _help_text(bot: Gw2Bot, user: Any, guild: Any = None) -> str:
    return "\n\n".join(
        build_help_messages(
            bot.tree.get_commands(),
            user,
            guild if guild is not None else _guild(),
            bot._config,
        )
    )


def _leaf_commands(bot: Gw2Bot) -> list[app_commands.Command[Any, ..., Any]]:
    leaves: list[app_commands.Command[Any, ..., Any]] = []
    for command in bot.tree.get_commands():
        if isinstance(command, app_commands.Group):
            leaves.extend(
                child
                for child in command.walk_commands()
                if isinstance(child, app_commands.Command)
            )
        elif isinstance(command, app_commands.Command):
            leaves.append(command)
    return leaves


class TestDeclarations:
    def test_every_registered_command_declares_who_may_run_it(
        self,
        bot: Gw2Bot,
    ) -> None:
        # /help leaves out a command with no declaration, so a new command
        # that forgets one would silently vanish from it.
        undeclared = [
            command.qualified_name
            for command in _leaf_commands(bot)
            if declared_access(command) is None
        ]

        assert undeclared == []

    def test_every_declared_role_names_a_config_field(self, bot: Gw2Bot) -> None:
        config_fields = {field.name for field in fields(Config)}
        named: set[str] = set()
        for command in _leaf_commands(bot):
            access = declared_access(command)
            assert access is not None
            named.update(iter_role_settings(access))
            for rule in command.extras.get("option_access", {}).values():
                named.update(iter_role_settings(rule))

        assert named
        assert named - config_fields == set()

    def test_a_group_declaration_covers_its_nested_subcommands(
        self,
        bot: Gw2Bot,
    ) -> None:
        settings = next(
            command
            for command in _leaf_commands(bot)
            if command.qualified_name == "settings roles raffle_draw"
        )

        assert declared_access(settings) == AnyOf(
            (RoleSetting("raffle_officer_role_id"), ServerAdministrator())
        )

    def test_a_command_declaration_wins_over_its_group(self) -> None:
        group = app_commands.Group(
            name="group",
            description="Group",
            extras=access_extras(Everyone()),
        )

        async def callback(interaction: discord.Interaction) -> None:
            return None

        command = app_commands.Command(
            name="child",
            description="Child",
            callback=callback,
            parent=group,
            extras=access_extras(RoleSetting("raffle_draw_role_id")),
        )

        assert declared_access(command) == RoleSetting("raffle_draw_role_id")


class TestVisibleCommands:
    def test_a_member_without_roles_sees_only_the_open_commands(
        self,
        bot: Gw2Bot,
    ) -> None:
        text = _help_text(bot, _caller(UNRELATED_ROLE_ID))

        for shown in (
            "`/help`",
            "`/raffle tickets [username]`",
            "`/raffle list`",
            "`/raffle leaderboard [sortby]`",
            "`/raffle audit <run_id>`",
            "`/profit view [days]`",
        ):
            assert shown in text
        for hidden in (
            "/raffle draw",
            "/raffle addticket",
            "/raffle removetickets",
            "/event",
            "/settings",
            "/gold",
            "/roster",
            "/check",
            "/track",
            "/pending",
        ):
            assert hidden not in text

    def test_the_addticket_role_sees_addticket_without_the_officer_option(
        self,
        bot: Gw2Bot,
        config: Config,
    ) -> None:
        text = _help_text(bot, _caller(config.raffle_addticket_role_id))

        assert "`/raffle addticket <username>` — " in text
        assert "amount" not in text
        assert "`/raffle addtickets [username1…username10]`" in text
        assert "`/raffle bulkaddtickets`" in text
        assert "/raffle draw" not in text

    def test_an_officer_with_the_addticket_role_sees_the_amount_option(
        self,
        bot: Gw2Bot,
        config: Config,
    ) -> None:
        text = _help_text(
            bot,
            _caller(
                config.raffle_addticket_role_id,
                config.raffle_officer_role_id,
            ),
        )

        assert "`/raffle addticket <username> [amount]`" in text
        assert "└ `amount` — Purchased tickets to add; Officers only" in text

    def test_an_officer_without_the_addticket_role_must_supply_the_amount(
        self,
        bot: Gw2Bot,
        config: Config,
    ) -> None:
        # Without the addticket role the command only lets them through with
        # an amount, so it is not optional for them.
        text = _help_text(bot, _caller(config.raffle_officer_role_id))

        assert "`/raffle addticket <username> <amount>`" in text
        assert "`/raffle addtickets" not in text
        for shown in ("`/check`", "`/pending`", "`/track <username>`"):
            assert shown in text
        assert "`/gold import`" in text
        assert "`/roster import`" in text
        assert "`/settings list`" in text

    def test_the_draw_role_sees_draw_and_removetickets(
        self,
        bot: Gw2Bot,
        config: Config,
    ) -> None:
        text = _help_text(bot, _caller(config.raffle_draw_role_id))

        assert "`/raffle draw`" in text
        assert "`/raffle removetickets <username> [amount]`" in text
        assert "/raffle addticket" not in text

    def test_the_event_role_sees_every_event_subcommand(
        self,
        bot: Gw2Bot,
        config: Config,
    ) -> None:
        text = _help_text(bot, _caller(config.event_create_role_id))

        for name in ("new", "edit", "remind", "cancel", "delete"):
            assert f"`/event {name}" in text

    @pytest.mark.parametrize(
        ("owner", "administrator"),
        [(True, False), (False, True)],
    )
    def test_the_owner_and_administrators_see_settings_only(
        self,
        bot: Gw2Bot,
        owner: bool,
        administrator: bool,
    ) -> None:
        text = _help_text(
            bot,
            _caller(user_id=7, administrator=administrator),
            _guild(owner_id=7 if owner else 999),
        )

        assert "`/settings list`" in text
        assert "`/check`" not in text
        assert "`/raffle draw`" not in text

    def test_a_role_changed_in_settings_is_what_help_answers_with(
        self,
        tmp_path: Path,
        bot: Gw2Bot,
    ) -> None:
        caller = _caller(555)
        assert "/raffle draw" not in _help_text(bot, caller)

        bot._config = _distinct_role_config(tmp_path, raffle_draw_role_id=555)

        assert "`/raffle draw`" in _help_text(bot, caller)

    def test_a_command_declaring_no_access_is_left_out(
        self,
        config: Config,
    ) -> None:
        async def callback(interaction: discord.Interaction) -> None:
            return None

        undeclared = app_commands.Command(
            name="mystery",
            description="Nobody said who may run this",
            callback=callback,
        )

        text = "\n".join(
            build_help_messages([undeclared], _caller(), _guild(), config)
        )

        assert "mystery" not in text


class TestPacking:
    def test_long_help_is_split_across_messages_within_the_limit(
        self,
        config: Config,
    ) -> None:
        async def callback(interaction: discord.Interaction) -> None:
            return None

        # Discord caps a group at 25 subcommands, so the length comes from
        # several groups.
        groups: list[app_commands.Group] = []
        for group_index in range(4):
            group = app_commands.Group(
                name=f"big{group_index}",
                description="A group",
                extras=access_extras(Everyone()),
            )
            for index in range(25):
                group.add_command(
                    app_commands.Command(
                        name=f"command{index}",
                        description="x" * 90,
                        callback=callback,
                    )
                )
            groups.append(group)

        messages = build_help_messages(groups, _caller(), _guild(), config)

        assert len(messages) > 1
        assert all(len(message) <= HELP_DESCRIPTION_LIMIT for message in messages)
        joined = "\n".join(messages)
        assert all(
            f"`/big{group_index} command{index}`" in joined
            for group_index in range(4)
            for index in range(25)
        )


def _officer_interaction(config: Config) -> Any:
    """A caller whose help runs past one page: /settings alone nearly fills one."""
    interaction = settings_interaction(
        role_ids=(
            config.raffle_officer_role_id,
            config.raffle_addticket_role_id,
            config.raffle_draw_role_id,
            config.event_create_role_id,
        )
    )
    interaction.response.edit_message = AsyncMock()
    return interaction


def _page_buttons(view: HelpPagerView) -> list[HelpPageButton]:
    return [item for item in view.children if isinstance(item, HelpPageButton)]


def _synced(bot: Gw2Bot) -> Gw2Bot:
    """``bot`` with its commands where startup leaves them.

    ``_sync_commands`` copies the global commands onto the command guild and
    then clears the global list, so a running bot has no global commands.
    """
    guild = discord.Object(id=bot._config.discord_command_guild_id)
    bot.tree.copy_global_to(guild=guild)
    bot.tree.clear_commands(guild=None)
    return bot


class TestRegisteredCommands:
    def test_reads_the_command_guild_once_the_globals_are_cleared(
        self,
        bot: Gw2Bot,
        config: Config,
    ) -> None:
        _synced(bot)
        guild = discord.Object(id=config.discord_command_guild_id)

        names = {command.name for command in registered_commands(bot.tree, guild)}

        assert {"help", "raffle", "settings"} <= names

    def test_a_guild_command_shadows_a_global_one_of_the_same_name(
        self,
        bot: Gw2Bot,
        config: Config,
    ) -> None:
        # Before any sync every command is global only; copying them to the
        # guild must not list each one twice.
        guild = discord.Object(id=config.discord_command_guild_id)
        bot.tree.copy_global_to(guild=guild)

        names = [command.name for command in registered_commands(bot.tree, guild)]

        assert len(names) == len(set(names))
        assert "help" in names

    def test_without_a_guild_only_the_globals_are_read(self, bot: Gw2Bot) -> None:
        names = {command.name for command in registered_commands(bot.tree, None)}

        assert "help" in names

    def test_nothing_to_list_is_still_one_page(self, config: Config) -> None:
        assert build_help_messages([], _caller(), _guild(), config) == [
            NO_COMMANDS_MESSAGE
        ]


class TestHelpCommand:
    async def test_lists_commands_on_a_bot_whose_commands_are_synced(
        self,
        bot: Gw2Bot,
        config: Config,
    ) -> None:
        # The regression: after startup the global list is empty, and /help
        # read only that, found no pages and raised IndexError.
        _synced(bot)
        interaction = settings_interaction(role_ids=(config.raffle_draw_role_id,))
        assert interaction.guild.id == config.discord_command_guild_id

        await handle_help_command(bot, interaction)

        interaction.response.send_message.assert_awaited_once()
        call = interaction.response.send_message.await_args
        description = call.kwargs["embed"].description
        assert "`/help`" in description
        assert "`/raffle draw`" in description

    async def test_a_single_page_is_one_private_embed_without_arrows(
        self,
        bot: Gw2Bot,
    ) -> None:
        interaction = settings_interaction(role_ids=(UNRELATED_ROLE_ID,))

        await handle_help_command(bot, interaction)

        interaction.response.send_message.assert_awaited_once()
        call = interaction.response.send_message.await_args
        assert call.kwargs["ephemeral"] is True
        assert "view" not in call.kwargs
        embed = call.kwargs["embed"]
        assert embed.title == "Commands you can use"
        assert "`/help`" in embed.description
        assert embed.footer.text is None
        interaction.followup.send.assert_not_awaited()

    async def test_several_pages_are_one_private_embed_with_arrows(
        self,
        bot: Gw2Bot,
        config: Config,
    ) -> None:
        interaction = _officer_interaction(config)

        await handle_help_command(bot, interaction)

        interaction.response.send_message.assert_awaited_once()
        interaction.followup.send.assert_not_awaited()
        call = interaction.response.send_message.await_args
        assert call.kwargs["ephemeral"] is True
        page_count = len(
            build_help_messages(
                bot.tree.get_commands(),
                interaction.user,
                interaction.guild,
                config,
            )
        )
        assert page_count > 1
        assert call.kwargs["embed"].footer.text == f"Page 1 of {page_count}"
        previous, following = _page_buttons(call.kwargs["view"])
        assert previous.item.disabled is True
        assert following.item.disabled is False

    async def test_a_failed_delivery_is_logged_without_raising(
        self,
        bot: Gw2Bot,
        config: Config,
    ) -> None:
        interaction = settings_interaction(
            role_ids=(config.raffle_officer_role_id,)
        )
        interaction.response.send_message = AsyncMock(
            side_effect=forbidden_error(50013)
        )

        await handle_help_command(bot, interaction)

        interaction.followup.send.assert_not_awaited()


class TestHelpPager:
    async def test_turns_pages_on_a_bot_whose_commands_are_synced(
        self,
        bot: Gw2Bot,
        config: Config,
    ) -> None:
        _synced(bot)
        interaction = _officer_interaction(config)
        interaction.client = bot

        await HelpPageButton(0, 1).callback(interaction)

        call = interaction.response.edit_message.await_args
        assert call is not None
        assert call.kwargs["embed"].footer.text.startswith("Page 2 of ")

    async def test_the_next_arrow_edits_the_reply_to_the_next_page(
        self,
        bot: Gw2Bot,
        config: Config,
    ) -> None:
        interaction = _officer_interaction(config)
        interaction.client = bot
        pages = build_help_messages(
            bot.tree.get_commands(),
            interaction.user,
            interaction.guild,
            config,
        )

        await HelpPageButton(0, 1).callback(interaction)

        interaction.response.edit_message.assert_awaited_once()
        call = interaction.response.edit_message.await_args
        embed = call.kwargs["embed"]
        assert embed.description == pages[1]
        assert embed.footer.text == f"Page 2 of {len(pages)}"
        previous, following = _page_buttons(call.kwargs["view"])
        assert previous.item.disabled is False
        assert following.item.disabled is (len(pages) == 2)
        interaction.response.send_message.assert_not_awaited()

    async def test_a_page_that_no_longer_exists_clamps_and_drops_the_arrows(
        self,
        bot: Gw2Bot,
        config: Config,
    ) -> None:
        # The officer role was taken away after /help was run: their pages
        # now fit on one, so the arrow that pointed past it lands on it and
        # the arrows go away.
        interaction = settings_interaction(role_ids=(UNRELATED_ROLE_ID,))
        interaction.response.edit_message = AsyncMock()
        interaction.client = bot

        await HelpPageButton(1, 1).callback(interaction)

        interaction.response.edit_message.assert_awaited_once()
        call = interaction.response.edit_message.await_args
        assert call is not None
        assert call.kwargs["embed"].footer.text is None
        assert "`/help`" in call.kwargs["embed"].description
        assert call.kwargs["view"] is None

    async def test_the_page_rides_in_the_custom_id(self) -> None:
        button = HelpPageButton(3, -1)

        assert button.item.custom_id == "gw2bot:help:3:-1"
        match = HelpPageButton.__discord_ui_compiled_template__.fullmatch(
            "gw2bot:help:3:-1"
        )
        assert match is not None
        rebuilt = await HelpPageButton.from_custom_id(
            cast(Any, None),
            button.item,
            match,
        )
        assert (rebuilt.page, rebuilt.direction) == (3, -1)

    async def test_a_failed_page_turn_is_logged_without_raising(
        self,
        bot: Gw2Bot,
        config: Config,
    ) -> None:
        interaction = _officer_interaction(config)
        interaction.client = bot
        interaction.response.edit_message = AsyncMock(
            side_effect=forbidden_error(50013)
        )

        await HelpPageButton(0, 1).callback(interaction)

        interaction.response.edit_message.assert_awaited_once()
