"""Which roles events may ping, and the `/event pingroles` commands behind it.

Officers keep the list by hand. A role is offered to a commander, and notified
by a post, only because it is on the list - never because of what it is called
- so renaming or repurposing a role changes nothing about whether an event can
reach it.

`views/` reads the list to build the role picker and `posting/` reads it again
before every ping, both through `pingable_role_ids`, so this module must never
import either of them back.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from typing import Any, TYPE_CHECKING

import discord
from discord import app_commands
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.core.command_access import RoleSetting, access_extras
from gw2bot.core.discord_utils import (
    log_discord_failure,
    safe_int,
    send_interaction_notice,
    user_has_role,
)
from gw2bot.events.models import MAX_PINGABLE_ROLES

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)

# The name prefix that made a role pingable before officers chose the roles.
# Read once, by the seed below, and never again.
LEGACY_PING_ROLE_PREFIX = "[GW2]"

AUTOCOMPLETE_LIMIT = 25
CHOICE_NAME_LIMIT = 100
# Between a role's name and its id in a suggestion: "{name} - {id}".
CHOICE_SEPARATOR = " - "
# What a listed role that no longer exists in the server is called.
DELETED_ROLE_NAME = "Deleted role"

# A suggestion typed out in full instead of picked, and a pasted mention. A
# picked suggestion arrives as the bare id, but either of these reaches the
# command as text and still names exactly one role.
_TYPED_CHOICE = re.compile(r".* - (?P<id>\d+)", re.DOTALL)
_ROLE_MENTION = re.compile(r"<@&(?P<id>\d+)>")

_OFFICER_ACCESS = RoleSetting("raffle_officer_role_id")
_COMMANDER_ACCESS = RoleSetting("event_create_role_id")


def pingable_role_ids(bot: Gw2Bot) -> tuple[int, ...]:
    """The roles events may ping, or none when the list cannot be read.

    Every caller is deciding whether to offer or notify a role, and a list
    that cannot be read has to answer that with "no" rather than with an
    error that takes an event post or a picker down with it.
    """
    try:
        return bot.event_store.get_ping_role_ids()
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not read the pingable event roles; treating none as "
            "pingable; error_type=%s",
            type(exc).__name__,
        )
        return ()


def seed_legacy_ping_roles(bot: Gw2Bot) -> None:
    """Start the pingable list from the roles that were pingable by name.

    Until officers chose them, any role whose name started with the legacy
    prefix could be picked and pinged. Starting the list from those roles is
    what keeps the events already pinging them doing so across the upgrade,
    with nothing for an officer to do first. It happens once per database: the
    store records that it ran, so a role an officer removes stays removed.

    A server that is not in the cache yet is retried on the next connection
    rather than seeded empty, which would mark the list seeded and lose every
    role it should have carried over.
    """
    guild = bot.get_guild(bot._config.discord_command_guild_id)
    if guild is None:
        LOGGER.debug(
            "Cannot seed the pingable event roles without the command "
            "server; retrying on the next connection"
        )
        return
    prefix = LEGACY_PING_ROLE_PREFIX.casefold()
    role_ids = [
        role.id
        for role in guild.roles
        if not _is_everyone(guild, role)
        and role.name.casefold().startswith(prefix)
    ]
    try:
        seeded = bot.event_store.seed_ping_roles(role_ids)
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not seed the pingable event roles; retrying on the next "
            "connection; error_type=%s",
            type(exc).__name__,
        )
        return
    LOGGER.debug(
        "Pingable event role seed finished; seeded=%s candidates=%s",
        seeded,
        len(role_ids),
    )


def role_choice_name(name: str, role_id: int) -> str:
    """How a role is suggested: its name, then its id.

    The id is always kept whole, because two roles can share a name and it is
    what the officer is really picking; a long name is shortened instead.
    """
    suffix = f"{CHOICE_SEPARATOR}{role_id}"
    budget = CHOICE_NAME_LIMIT - len(suffix)
    if len(name) > budget:
        name = name[: budget - 1].rstrip() + "…"
    return f"{name}{suffix}"


def role_choices(
    roles: Iterable[tuple[int, str | None]],
    current: str,
) -> list[app_commands.Choice[str]]:
    """Suggestions for ``(role id, name)`` pairs matching what was typed.

    Matched against the name and the id alike, so an officer holding a copied
    id finds the role as readily as one typing its name. A name of None is a
    role the server no longer has, which can still be on the pingable list.
    """
    query = current.strip().casefold()
    named = [
        (role_id, name if name is not None else DELETED_ROLE_NAME)
        for role_id, name in roles
    ]
    named.sort(key=lambda pair: (pair[1].casefold(), pair[0]))
    choices: list[app_commands.Choice[str]] = []
    for role_id, name in named:
        if query and query not in name.casefold() and query not in str(role_id):
            continue
        choices.append(
            app_commands.Choice(
                name=role_choice_name(name, role_id),
                value=str(role_id),
            )
        )
        if len(choices) >= AUTOCOMPLETE_LIMIT:
            break
    return choices


def matching_role_ids(
    candidates: Sequence[tuple[int, str | None]],
    text: str,
) -> list[int]:
    """Which of ``candidates`` the officer's ``text`` names.

    An id - bare, as a mention, or at the end of a suggestion typed out in
    full - wins when it is one of the candidates. Otherwise the text is read
    as a name, compared without regard to case, which can match several roles
    that share it; the caller decides what to say about that.
    """
    cleaned = text.strip()
    if not cleaned:
        return []
    referenced = _referenced_role_id(cleaned)
    if referenced is not None and any(
        role_id == referenced for role_id, _ in candidates
    ):
        return [referenced]
    # Not an id the candidates hold, so it may be a name that happens to look
    # like one - a role called "2024" is still found by typing 2024.
    folded = cleaned.casefold()
    return [
        role_id
        for role_id, name in candidates
        if name is not None and name.casefold() == folded
    ]


def _referenced_role_id(text: str) -> int | None:
    if text.isdigit():
        return safe_int(text)
    match = _ROLE_MENTION.fullmatch(text) or _TYPED_CHOICE.fullmatch(text)
    if match is None:
        return None
    return safe_int(match["id"])


def _is_everyone(guild: Any, role: Any) -> bool:
    # Discord gives @everyone the server's own id.
    return role.id == getattr(guild, "id", None)


def _role_name(guild: Any, role_id: int) -> str | None:
    role = guild.get_role(role_id) if guild is not None else None
    return None if role is None else role.name


def _describe_role(name: str | None, role_id: int) -> str:
    if name is None:
        return f"The deleted role with ID `{role_id}`"
    return f"**{name}**"


class EventPingRoleCommands(app_commands.Group):
    """`/event pingroles`: the roles events may ping.

    Officers add and remove them; commanders, who pick from them, may see
    them. Each subcommand declares and checks its own role, because the two
    halves are for different people and neither is the `/event` group's.
    """

    def __init__(self, bot: Gw2Bot, parent: app_commands.Group):
        super().__init__(
            name="pingroles",
            description="Choose which roles events may ping",
            parent=parent,
        )
        self._bot = bot

    async def _authorized(
        self,
        interaction: discord.Interaction,
        role_id: int,
        action: str,
    ) -> bool:
        if user_has_role(interaction.user, role_id):
            return True
        LOGGER.warning(
            "Rejected event ping role command from Discord user %s; "
            "required role %s",
            interaction.user.id,
            role_id,
        )
        await send_interaction_notice(
            interaction,
            f"You do not have the required role to {action}.",
        )
        return False

    async def server_role_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        # Autocomplete must never raise: a caller without the role simply sees
        # no suggestions, and the command still enforces the gate.
        if not user_has_role(
            interaction.user,
            self._bot._config.raffle_officer_role_id,
        ):
            return []
        guild = interaction.guild
        if guild is None:
            return []
        choices = role_choices(
            (
                (role.id, role.name)
                for role in guild.roles
                if not _is_everyone(guild, role)
            ),
            current,
        )
        LOGGER.debug(
            "Returning server role autocomplete choices; choices=%s",
            len(choices),
        )
        return choices

    async def pingable_role_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        # Only the roles already on the list: removing is all this is for, and
        # a role deleted from the server is still offered by its id so it can
        # be taken off.
        if not user_has_role(
            interaction.user,
            self._bot._config.raffle_officer_role_id,
        ):
            return []
        guild = interaction.guild
        choices = role_choices(
            (
                (role_id, _role_name(guild, role_id))
                for role_id in pingable_role_ids(self._bot)
            ),
            current,
        )
        LOGGER.debug(
            "Returning pingable role autocomplete choices; choices=%s",
            len(choices),
        )
        return choices

    @app_commands.command(
        name="add",
        description="Let events ping a role",
        extras=access_extras(_OFFICER_ACCESS),
    )
    @app_commands.describe(role="The role to make pingable, by name or ID")
    @app_commands.autocomplete(role=server_role_autocomplete)
    async def add(self, interaction: discord.Interaction, role: str) -> None:
        LOGGER.debug(
            "Event ping role add invoked by Discord user %s; characters=%s",
            interaction.user.id,
            len(role),
        )
        if not await self._authorized(
            interaction,
            self._bot._config.raffle_officer_role_id,
            "change which roles events may ping",
        ):
            return
        guild = interaction.guild
        if guild is None:
            await send_interaction_notice(
                interaction,
                "This command can only be used in the server.",
            )
            return
        server_roles = {
            server_role.id: server_role for server_role in guild.roles
        }
        matched = matching_role_ids(
            [
                (role_id, server_role.name)
                for role_id, server_role in server_roles.items()
            ],
            role,
        )
        if len(matched) != 1:
            await self._refuse_match(interaction, matched, "add")
            return
        role_id = matched[0]
        target = server_roles[role_id]
        if _is_everyone(guild, target):
            # @everyone is in the server's role list, but a post that pinged it
            # would notify the whole server rather than the people who opted in.
            LOGGER.debug(
                "Refused to make @everyone pingable; user_id=%s",
                interaction.user.id,
            )
            await send_interaction_notice(
                interaction,
                "@everyone cannot be pinged by events. Pick a role members "
                "opt into instead.",
            )
            return
        name = _describe_role(target.name, role_id)
        try:
            current = self._bot.event_store.get_ping_role_ids()
            if role_id in current:
                added = False
            elif len(current) >= MAX_PINGABLE_ROLES:
                LOGGER.debug(
                    "Refused a pingable event role over the limit; "
                    "user_id=%s roles=%s",
                    interaction.user.id,
                    len(current),
                )
                await send_interaction_notice(
                    interaction,
                    f"Events can already ping {MAX_PINGABLE_ROLES} roles, "
                    "which is as many as the role picker can show. Remove "
                    "one with `/event pingroles remove` first.",
                )
                return
            else:
                added = self._bot.event_store.add_ping_role(role_id)
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not add a pingable event role; user_id=%s "
                "error_type=%s",
                interaction.user.id,
                type(exc).__name__,
            )
            await send_interaction_notice(
                interaction,
                "The event database could not be written. Nothing was "
                "changed.",
            )
            return
        LOGGER.info(
            "Event ping role add handled; user_id=%s role_id=%s added=%s",
            interaction.user.id,
            role_id,
            added,
        )
        if not added:
            await send_interaction_notice(
                interaction,
                f"{name} can already be pinged by events.",
            )
            return
        await send_interaction_notice(
            interaction,
            f"{name} can now be pinged by events. Commanders can pick it "
            "when they create or edit an event.",
        )

    @app_commands.command(
        name="remove",
        description="Stop events pinging a role",
        extras=access_extras(_OFFICER_ACCESS),
    )
    @app_commands.describe(role="The pingable role to remove, by name or ID")
    @app_commands.autocomplete(role=pingable_role_autocomplete)
    async def remove(self, interaction: discord.Interaction, role: str) -> None:
        LOGGER.debug(
            "Event ping role remove invoked by Discord user %s; "
            "characters=%s",
            interaction.user.id,
            len(role),
        )
        if not await self._authorized(
            interaction,
            self._bot._config.raffle_officer_role_id,
            "change which roles events may ping",
        ):
            return
        guild = interaction.guild
        try:
            current = self._bot.event_store.get_ping_role_ids()
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not read the pingable event roles to remove one; "
                "user_id=%s error_type=%s",
                interaction.user.id,
                type(exc).__name__,
            )
            await send_interaction_notice(
                interaction,
                "The event database could not be read. Nothing was changed.",
            )
            return
        # Only the listed roles are candidates, so a role that is not on the
        # list can never be matched - by name or by id - and one deleted from
        # the server is still matched by its id.
        candidates = [
            (role_id, _role_name(guild, role_id)) for role_id in current
        ]
        matched = matching_role_ids(candidates, role)
        if len(matched) != 1:
            await self._refuse_match(interaction, matched, "remove")
            return
        role_id = matched[0]
        name = _describe_role(_role_name(guild, role_id), role_id)
        try:
            removed = self._bot.event_store.remove_ping_role(role_id)
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not remove a pingable event role; user_id=%s "
                "error_type=%s",
                interaction.user.id,
                type(exc).__name__,
            )
            await send_interaction_notice(
                interaction,
                "The event database could not be written. Nothing was "
                "changed.",
            )
            return
        LOGGER.info(
            "Event ping role remove handled; user_id=%s role_id=%s "
            "removed=%s",
            interaction.user.id,
            role_id,
            removed,
        )
        await send_interaction_notice(
            interaction,
            f"{name} can no longer be pinged by events. Events that already "
            "ping it stop doing so from their next post, and start again if "
            "it is added back.",
        )

    @app_commands.command(
        name="list",
        description="Show the roles events may ping",
        extras=access_extras(_COMMANDER_ACCESS),
    )
    async def list_roles(self, interaction: discord.Interaction) -> None:
        LOGGER.debug(
            "Event ping role list invoked by Discord user %s",
            interaction.user.id,
        )
        if not await self._authorized(
            interaction,
            self._bot._config.event_create_role_id,
            "see which roles events may ping",
        ):
            return
        try:
            role_ids = self._bot.event_store.get_ping_role_ids()
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not read the pingable event roles to list them; "
                "user_id=%s error_type=%s",
                interaction.user.id,
                type(exc).__name__,
            )
            await send_interaction_notice(
                interaction,
                "The event database could not be read. Try again in a "
                "moment.",
            )
            return
        if not role_ids:
            await send_interaction_notice(
                interaction,
                "No roles can be pinged by events yet. An officer can add "
                "one with `/event pingroles add`.",
            )
            return
        guild = interaction.guild
        named = sorted(
            (
                (name, role_id)
                for role_id in role_ids
                if (name := _role_name(guild, role_id)) is not None
            ),
            key=lambda pair: (pair[0].casefold(), pair[1]),
        )
        lines = [f"• {name}" for name, _ in named]
        # A listed role the server no longer has is still on the list, and
        # shown so an officer can see there is something to remove.
        lines.extend(
            f"• {DELETED_ROLE_NAME} (ID `{role_id}`)"
            for role_id in role_ids
            if _role_name(guild, role_id) is None
        )
        # An embed rather than a message: 25 role names of up to 100
        # characters each overrun what one message may hold.
        embed = discord.Embed(
            title="Roles events can ping",
            description="\n".join(lines),
        )
        try:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.DiscordException as exc:
            log_discord_failure(
                "Could not send the pingable event role list; user_id=%s",
                exc,
                interaction.user.id,
            )
            return
        LOGGER.debug(
            "Listed the pingable event roles; user_id=%s roles=%s deleted=%s",
            interaction.user.id,
            len(role_ids),
            len(role_ids) - len(named),
        )

    async def _refuse_match(
        self,
        interaction: discord.Interaction,
        matched: Sequence[int],
        action: str,
    ) -> None:
        """Answer an add or remove whose text named no role, or several."""
        LOGGER.debug(
            "Event ping role %s did not name one role; user_id=%s matches=%s",
            action,
            interaction.user.id,
            len(matched),
        )
        if matched:
            message = (
                f"{len(matched)} roles have that name. Pick the one you mean "
                "from the suggestions, or give its role ID."
            )
        elif action == "add":
            message = (
                "No role in this server has that name or ID. Pick one from "
                "the suggestions."
            )
        else:
            message = (
                "That is not one of the roles events can ping. Pick one from "
                "the suggestions."
            )
        await send_interaction_notice(interaction, message)
