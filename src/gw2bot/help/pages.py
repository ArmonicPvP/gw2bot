"""The pages of `/help`: every command the caller may run, and what it does.

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
from typing import Any

import discord
from discord import app_commands

from gw2bot.core.command_access import declared_access, declared_option_access

LOGGER = logging.getLogger(__name__)

# Discord allows 4096 characters in an embed description; the margin keeps a
# joined section from landing exactly on the edge.
HELP_DESCRIPTION_LIMIT = 4000
OTHER_COMMANDS_HEADING = "**Other commands**"
NO_COMMANDS_MESSAGE = "There are no commands you can use here."
# Written the way README.md writes command usage, which is also how Discord
# shows an option once it is picked: its name, a colon, then the value.
USAGE_KEY = (
    "*`option:<value>` is required. `[option:<value>]` is optional and can "
    "be left out.*"
)

# What a value is when the option offers no choices to list instead.
_VALUE_HINTS: dict[discord.AppCommandOptionType, str] = {
    discord.AppCommandOptionType.string: "text",
    discord.AppCommandOptionType.integer: "number",
    discord.AppCommandOptionType.number: "number",
    discord.AppCommandOptionType.boolean: "true|false",
    discord.AppCommandOptionType.user: "member",
    discord.AppCommandOptionType.channel: "channel",
    discord.AppCommandOptionType.role: "role",
    discord.AppCommandOptionType.mentionable: "member or role",
    discord.AppCommandOptionType.attachment: "file",
}

_NUMBERED_OPTION = re.compile(r"^(?P<stem>.*?)(?P<number>\d+)$")

type _TreeCommand = (
    app_commands.Command[Any, ..., Any]
    | app_commands.Group
    | app_commands.ContextMenu
)


def registered_commands(
    tree: app_commands.CommandTree[Any],
    guild: discord.abc.Snowflake | None,
) -> list[_TreeCommand]:
    """The commands Discord offers in ``guild``: its own, then any global ones.

    The bot registers its commands to the command guild: startup copies the
    global commands onto it and then clears the global list, so reading the
    global list alone finds nothing once the bot is running. A guild command
    shadows a global one of the same name, as it does in Discord's picker.
    """
    scoped = list(tree.get_commands(guild=guild)) if guild is not None else []
    names = {command.name for command in scoped}
    return scoped + [
        command for command in tree.get_commands() if command.name not in names
    ]


def build_help_messages(
    commands: Iterable[_TreeCommand],
    user: Any,
    guild: Any,
    settings: object,
) -> list[str]:
    """The pages of `/help` for ``user``, in order, one embed description each.

    Each command group is one section headed by the group, with its
    subcommands in alphabetical order; the top-level commands that belong to
    no group share a closing section. Every page opens with the key to how
    options are written. Context menu commands are never typed, so they are
    not listed.
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
    for group in sorted(groups, key=lambda group: group.name.casefold()):
        lines: list[str] = []
        # By full name, so a nested group's subcommands (`/settings roles
        # ...`) sort among their siblings rather than after them.
        subcommands = sorted(
            (
                command
                for command in group.walk_commands()
                if isinstance(command, app_commands.Command)
            ),
            key=lambda command: command.qualified_name.casefold(),
        )
        for command in subcommands:
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
    for command in sorted(standalone, key=lambda command: command.name.casefold()):
        line = _command_line(command, user, guild, settings)
        if line is None:
            hidden += 1
            continue
        shown += 1
        other.append(line)
    if other:
        sections.append("\n".join([OTHER_COMMANDS_HEADING, *other]))

    # Never an empty list: the reply and the pager both show a page, and
    # /help itself should always be listed, so an empty result means the
    # commands were read from the wrong place and ought to say so.
    # The key opens every page, so the room it takes comes off each one.
    packed = _pack_sections(
        sections,
        HELP_DESCRIPTION_LIMIT - len(USAGE_KEY) - len("\n\n"),
    )
    messages = [f"{USAGE_KEY}\n\n{page}" for page in packed] or [
        NO_COMMANDS_MESSAGE
    ]
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
    """``name:<value>`` for each required option and ``[name:<value>]`` for the rest.

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
            option = (
                f"{_option_usage(parameter)} … "
                f"{_option_usage(parameters[end - 1])}"
            )
        else:
            option = _option_usage(parameter)
        placeholders.append(option if is_required else f"[{option}]")
        index = end
    return placeholders


def _option_usage(parameter: app_commands.Parameter) -> str:
    """``name:<value>``, with the choices as the value when there are any."""
    if parameter.choices:
        value = "|".join(choice.name for choice in parameter.choices)
    else:
        value = _VALUE_HINTS.get(parameter.type, "value")
    return f"{parameter.display_name}:<{value}>"


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
