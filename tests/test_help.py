from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any
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
from gw2bot.help.commands import HELP_DESCRIPTION_LIMIT, handle_help_command

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


class TestHelpCommand:
    async def test_replies_privately_with_the_first_message(
        self,
        bot: Gw2Bot,
    ) -> None:
        interaction = settings_interaction(role_ids=(UNRELATED_ROLE_ID,))

        await handle_help_command(bot, interaction)

        interaction.response.send_message.assert_awaited_once()
        call = interaction.response.send_message.await_args
        assert call.kwargs["ephemeral"] is True
        embed = call.kwargs["embed"]
        assert embed.title == "Commands you can use"
        assert "`/help`" in embed.description
        interaction.followup.send.assert_not_awaited()

    async def test_sends_the_rest_as_private_followups(
        self,
        bot: Gw2Bot,
        config: Config,
    ) -> None:
        interaction = settings_interaction(
            role_ids=(
                config.raffle_officer_role_id,
                config.raffle_addticket_role_id,
                config.raffle_draw_role_id,
                config.event_create_role_id,
            )
        )

        await handle_help_command(bot, interaction)

        interaction.response.send_message.assert_awaited_once()
        assert interaction.followup.send.await_count >= 1
        for call in interaction.followup.send.await_args_list:
            assert call.kwargs["ephemeral"] is True
            assert call.kwargs["embed"].title == "Commands you can use (continued)"

    async def test_a_failed_delivery_stops_without_raising(
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
