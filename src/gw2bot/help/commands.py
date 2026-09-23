"""`/help`: every command the caller may run, and what it does.

Nothing here lists the bot's commands by hand. The registered command tree is
walked on every call, and each command's declared access (see
`gw2bot.core.command_access`) is judged against the caller's roles and the
role settings as they stand right now, so a new command, or a role moved with
`/settings`, is reflected without touching this module.

An option gated more strictly than its command is left out of the usage for a
caller who may not use it, the way `/raffle addticket`'s ``amount`` is shown
to officers only.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands

from gw2bot.core.command_access import (
    Everyone,
    access_extras,
    declared_access,
    declared_option_access,
)
from gw2bot.core.discord_utils import log_discord_failure

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)

HELP_TITLE = "Commands you can use"
# Discord allows 4096 characters in an embed description; the margin keeps a
# joined section from landing exactly on the edge.
HELP_DESCRIPTION_LIMIT = 4000
OTHER_COMMANDS_HEADING = "**Other commands**"

_NUMBERED_OPTION = re.compile(r"^(?P<stem>.*?)(?P<number>\d+)$")

type _TreeCommand = (
    app_commands.Command[Any, ..., Any]
    | app_commands.Group
    | app_commands.ContextMenu
)


def create_help_command(bot: Gw2Bot) -> app_commands.Command[Any, ..., None]:
    @app_commands.command(
        name="help",
        description="Show the commands you can use and what they do",
        extras=access_extras(Everyone()),
    )
    @app_commands.guild_only()
    async def help_command(interaction: discord.Interaction) -> None:
        await handle_help_command(bot, interaction)

    return help_command


async def handle_help_command(
    bot: Gw2Bot,
    interaction: discord.Interaction,
) -> None:
    user_id = getattr(getattr(interaction, "user", None), "id", "unknown")
    LOGGER.debug("Help command invoked by Discord user %s", user_id)
    messages = build_help_messages(
        bot.tree.get_commands(),
        interaction.user,
        interaction.guild,
        bot._config,
    )
    for index, description in enumerate(messages):
        embed = discord.Embed(
            title=HELP_TITLE if index == 0 else f"{HELP_TITLE} (continued)",
            description=description,
        )
        try:
            if index == 0:
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
        except discord.DiscordException as error:
            log_discord_failure(
                "Could not deliver help message %s of %s",
                error,
                index + 1,
                len(messages),
            )
            return
    LOGGER.debug(
        "Help command completed for Discord user %s; messages=%s",
        user_id,
        len(messages),
    )


def build_help_messages(
    commands: Iterable[_TreeCommand],
    user: Any,
    guild: Any,
    settings: object,
) -> list[str]:
    """The embed descriptions `/help` sends ``user``, in order.

    Each command group is one section headed by the group; the top-level
    commands that belong to no group share a closing section. Context menu
    commands are never typed, so they are not listed.
    """
    groups: list[app_commands.Group] = []
    standalone: list[app_commands.Command[Any, ..., Any]] = []
    for command in commands:
        if isinstance(command, app_commands.Group):
            groups.append(command)
        elif isinstance(command, app_commands.Command):
            standalone.append(command)

    sections: list[str] = []
    shown = 0
    hidden = 0
    for group in sorted(groups, key=lambda group: group.name):
        lines: list[str] = []
        for command in group.walk_commands():
            if not isinstance(command, app_commands.Command):
                continue
            line = _command_line(command, user, guild, settings)
            if line is None:
                hidden += 1
                continue
            shown += 1
            lines.append(line)
        if lines:
            heading = f"**/{group.name}** — {group.description}"
            sections.append("\n".join([heading, *lines]))

    other: list[str] = []
    for command in sorted(standalone, key=lambda command: command.name):
        line = _command_line(command, user, guild, settings)
        if line is None:
            hidden += 1
            continue
        shown += 1
        other.append(line)
    if other:
        sections.append("\n".join([OTHER_COMMANDS_HEADING, *other]))

    messages = _pack_sections(sections, HELP_DESCRIPTION_LIMIT)
    LOGGER.debug(
        "Built help; commands_shown=%s commands_hidden=%s sections=%s "
        "messages=%s",
        shown,
        hidden,
        len(sections),
        len(messages),
    )
    return messages


def _command_line(
    command: app_commands.Command[Any, ..., Any],
    user: Any,
    guild: Any,
    settings: object,
) -> str | None:
    """How ``command`` reads in `/help`, or ``None`` if ``user`` may not run it.

    A command that declares no access at all is treated as not the caller's:
    advertising a command whose gate is unknown would be the worse mistake.
    """
    access = declared_access(command)
    if access is None:
        LOGGER.warning(
            "Command declares no access and was left out of help; command=%s",
            command.qualified_name,
        )
        return None
    command_allowed = access.allows(user, guild, settings)
    allowed_options = {
        name
        for name, rule in declared_option_access(command).items()
        if rule.allows(user, guild, settings)
    }
    if not command_allowed and not allowed_options:
        return None

    parameters = [
        parameter
        for parameter in command.parameters
        if parameter.name not in declared_option_access(command)
        or parameter.name in allowed_options
    ]
    usage = " ".join(
        [
            f"/{command.qualified_name}",
            *_option_placeholders(
                parameters,
                # A caller who holds only an option's role can run the
                # command only by supplying that option, so it is not
                # optional for them.
                required=set() if command_allowed else allowed_options,
            ),
        ]
    )
    lines = [f"`{usage}` — {command.description}"]
    lines.extend(
        f"└ `{parameter.display_name}` — {parameter.description}"
        for parameter in parameters
        if parameter.name in allowed_options
    )
    return "\n".join(lines)


def _option_placeholders(
    parameters: Sequence[app_commands.Parameter],
    required: set[str],
) -> list[str]:
    """``<name>`` for each required option and ``[name]`` for the rest.

    A run of numbered options such as ``username1`` .. ``username10`` reads
    as one placeholder, which is what it is to the person typing it.
    """
    placeholders: list[str] = []
    index = 0
    while index < len(parameters):
        parameter = parameters[index]
        is_required = parameter.required or parameter.name in required
        end = index + 1
        match = _NUMBERED_OPTION.match(parameter.display_name)
        if match is not None:
            while end < len(parameters):
                following = parameters[end]
                following_match = _NUMBERED_OPTION.match(following.display_name)
                if (
                    following_match is None
                    or following_match["stem"] != match["stem"]
                    or (following.required or following.name in required)
                    != is_required
                ):
                    break
                end += 1
        if end - index > 1:
            name = f"{parameter.display_name}…{parameters[end - 1].display_name}"
        else:
            name = parameter.display_name
        placeholders.append(f"<{name}>" if is_required else f"[{name}]")
        index = end
    return placeholders


def _pack_sections(sections: Sequence[str], limit: int) -> list[str]:
    """Join ``sections`` into as few descriptions of at most ``limit`` as fit.

    A section never shares a boundary with another unless it fits whole, and
    one too long on its own is split between its lines.
    """
    messages: list[str] = []
    current = ""
    for section in sections:
        pieces = [section] if len(section) <= limit else _split_lines(section, limit)
        for piece in pieces:
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) <= limit:
                current = candidate
                continue
            messages.append(current)
            current = piece
    if current:
        messages.append(current)
    return messages


def _split_lines(text: str, limit: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            pieces.append(current)
        # A single line longer than the limit is cut; no command description
        # comes close, so this only keeps the embed valid.
        current = line[:limit]
    if current:
        pieces.append(current)
    return pieces
