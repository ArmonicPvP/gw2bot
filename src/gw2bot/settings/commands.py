from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.config import ConfigurationError
from gw2bot.discord_utils import (
    forum_tags_for_ids,
    safe_int,
    send_interaction_notice,
    user_has_role,
)
from gw2bot.settings.definitions import (
    CHANNELS_GROUP,
    ROLES_GROUP,
    SECRET_PLACEHOLDER,
    SETTING_DEFINITIONS,
    UNSET_DISPLAY,
    SettingDefinition,
    ValidationTarget,
    definitions_in_group,
    setting_key,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)

AUTOCOMPLETE_LIMIT = 25
CHOICE_NAME_LIMIT = 100

# Anything that is only whitespace clears the setting, which is how the
# request describes it: "a space or nothing means unset".
UNSET_INPUT = ""


class SettingsCommands(app_commands.Group):
    """The /settings command tree.

    Every subcommand is generated from a SettingDefinition rather than written
    out, so the registry stays the one place a setting is described. The
    decorator form cannot be parameterised that way, so the commands are built
    and added here.
    """

    def __init__(self, bot: Gw2Bot):
        super().__init__(
            name="settings",
            description="View and change the bot's configuration",
            guild_only=True,
        )
        self._bot = bot
        self._groups: dict[str, app_commands.Group] = {
            ROLES_GROUP: app_commands.Group(
                name=ROLES_GROUP,
                description="Roles that gate the bot's commands and pages",
                parent=self,
            ),
            CHANNELS_GROUP: app_commands.Group(
                name=CHANNELS_GROUP,
                description="Channels and forum tags the bot posts to",
                parent=self,
            ),
        }
        self.add_command(self._build_list_command())
        for definition in SETTING_DEFINITIONS:
            command = self._build_command(definition)
            if definition.group is None:
                self.add_command(command)
            else:
                self._groups[definition.group].add_command(command)

    # ------------------------------------------------------------------
    # Authorization

    async def authorize(self, interaction: discord.Interaction) -> bool:
        """Whether this caller may read and change the configuration.

        The officer role is a setting itself, so gating only on it would let
        one wrong value lock everybody out of the command that could fix it.
        The server owner and anyone holding Administrator are therefore always
        allowed: that arm is the recovery path, not a convenience.
        """
        user = interaction.user
        officer_role_id = self._bot._config.raffle_officer_role_id
        is_officer = user_has_role(user, officer_role_id)
        guild = interaction.guild
        is_owner = guild is not None and getattr(guild, "owner_id", None) == user.id
        permissions = getattr(user, "guild_permissions", None)
        is_admin = bool(getattr(permissions, "administrator", False))
        if is_officer or is_owner or is_admin:
            LOGGER.debug(
                "Authorized settings command; user_id=%s officer=%s owner=%s "
                "administrator=%s",
                user.id,
                is_officer,
                is_owner,
                is_admin,
            )
            return True
        LOGGER.warning(
            "Rejected settings command from Discord user %s; required role %s",
            user.id,
            officer_role_id,
        )
        await send_interaction_notice(
            interaction,
            "You do not have the required role to view or change the bot's "
            "settings.",
        )
        return False

    # ------------------------------------------------------------------
    # Command construction

    def _build_command(
        self,
        definition: SettingDefinition,
    ) -> app_commands.Command[Any, ..., None]:
        async def callback(
            interaction: discord.Interaction,
            value: str | None = None,
        ) -> None:
            await self._handle(interaction, definition, value)

        command = app_commands.Command(
            name=definition.name,
            description=_short_description(definition),
            callback=callback,  # type: ignore[arg-type]
        )
        app_commands.describe(
            value=(
                "New value. A space clears it. Leave it out to see the "
                "current one."
            )
        )(command)
        if definition.validates is not None:
            app_commands.autocomplete(
                value=_autocomplete_for(self, definition)
            )(command)
        return command

    def _build_list_command(self) -> app_commands.Command[Any, ..., None]:
        async def callback(interaction: discord.Interaction) -> None:
            await self._handle_list(interaction)

        return app_commands.Command(
            name="list",
            description="Show every setting, where its value came from",
            callback=callback,  # type: ignore[arg-type]
        )

    # ------------------------------------------------------------------
    # Handlers

    async def _handle(
        self,
        interaction: discord.Interaction,
        definition: SettingDefinition,
        value: str | None,
    ) -> None:
        key = setting_key(definition)
        LOGGER.debug(
            "Settings command invoked; setting=%s user_id=%s value_supplied=%s",
            key,
            getattr(getattr(interaction, "user", None), "id", "unknown"),
            value is not None,
        )
        if not await self.authorize(interaction):
            return
        if value is None:
            await self._report(interaction, definition)
            return
        if value.strip() == UNSET_INPUT:
            await self._unset(interaction, definition)
            return
        await self._store(interaction, definition, value)

    async def _report(
        self,
        interaction: discord.Interaction,
        definition: SettingDefinition,
    ) -> None:
        try:
            display = self._display_value(definition)
        except SQLAlchemyError:
            LOGGER.error(
                "Could not read a setting to report it; setting=%s",
                setting_key(definition),
            )
            await send_interaction_notice(
                interaction,
                "The settings database could not be read. Try again in a "
                "moment.",
            )
            return
        await send_interaction_notice(
            interaction,
            f"**{definition.command_path}**\n"
            f"{definition.description}\n"
            f"\nCurrent value: {display}",
        )
        LOGGER.debug(
            "Reported a setting; setting=%s secret=%s",
            setting_key(definition),
            definition.secret,
        )

    async def _unset(
        self,
        interaction: discord.Interaction,
        definition: SettingDefinition,
    ) -> None:
        key = setting_key(definition)
        try:
            removed = self._bot.settings_store.unset(definition)
        except SQLAlchemyError:
            LOGGER.error("Could not unset a setting; setting=%s", key)
            await send_interaction_notice(
                interaction,
                "The settings database could not be written. Nothing was "
                "changed.",
            )
            return
        applied = await self._apply(interaction, definition)
        if applied is None:
            return
        fallback = self._fallback_note(definition)
        await send_interaction_notice(
            interaction,
            f"**{definition.command_path}** is no longer set."
            + (f"\n{fallback}" if fallback else "")
            + _restart_note(applied),
        )
        LOGGER.debug(
            "Unset a setting from Discord; setting=%s had_value=%s",
            key,
            removed,
        )

    async def _store(
        self,
        interaction: discord.Interaction,
        definition: SettingDefinition,
        value: str,
    ) -> None:
        key = setting_key(definition)
        try:
            parsed = definition.parse(value)
        except ConfigurationError as exc:
            LOGGER.debug(
                "Rejected a setting value; setting=%s reason=parse", key
            )
            await send_interaction_notice(interaction, f"{exc}")
            return
        problem = await self._validate(interaction, definition, parsed)
        if problem is not None:
            LOGGER.debug(
                "Rejected a setting value; setting=%s reason=discord", key
            )
            await send_interaction_notice(interaction, problem)
            return
        stored = str(parsed)
        try:
            self._bot.settings_store.set_raw(definition, stored)
        except SQLAlchemyError:
            LOGGER.error("Could not store a setting; setting=%s", key)
            await send_interaction_notice(
                interaction,
                "The settings database could not be written. Nothing was "
                "changed.",
            )
            return
        applied = await self._apply(interaction, definition)
        if applied is None:
            return
        shown = SECRET_PLACEHOLDER if definition.secret else f"`{stored}`"
        await send_interaction_notice(
            interaction,
            f"**{definition.command_path}** is now {shown}."
            + _restart_note(applied),
        )
        LOGGER.debug(
            "Stored a setting from Discord; setting=%s secret=%s characters=%s",
            key,
            definition.secret,
            len(stored),
        )

    async def _apply(
        self,
        interaction: discord.Interaction,
        definition: SettingDefinition,
    ) -> list[str] | None:
        """Reload the configuration, reporting what had to be restarted.

        Returns None once the caller has been told the change could not be
        applied, so every caller only has to stop.
        """
        try:
            return await self._bot.apply_settings_change({definition.field})
        except Exception as exc:
            LOGGER.error(
                "Could not apply a settings change; setting=%s error_type=%s",
                setting_key(definition),
                type(exc).__name__,
            )
            await send_interaction_notice(
                interaction,
                "The value was saved, but applying it failed: "
                f"{exc}\nRestart the bot to pick it up.",
            )
            return None

    async def _handle_list(self, interaction: discord.Interaction) -> None:
        LOGGER.debug(
            "Settings list command invoked; user_id=%s",
            getattr(getattr(interaction, "user", None), "id", "unknown"),
        )
        if not await self.authorize(interaction):
            return
        try:
            lines = self._list_lines()
        except SQLAlchemyError:
            LOGGER.error("Could not read the settings to list them")
            await send_interaction_notice(
                interaction,
                "The settings database could not be read. Try again in a "
                "moment.",
            )
            return
        await send_interaction_notice(interaction, "\n".join(lines))
        LOGGER.debug("Reported the settings list; settings=%s", len(lines))

    def _list_lines(self) -> list[str]:
        lines = ["**Bot settings**"]
        for group in (None, ROLES_GROUP, CHANNELS_GROUP):
            definitions = definitions_in_group(group)
            if not definitions:
                continue
            if group is not None:
                lines.append(f"\n__/settings {group}__")
            for definition in definitions:
                source = (
                    "set"
                    if self._bot.settings_store.is_set(definition)
                    else "default"
                )
                lines.append(
                    f"`{definition.name}` — {self._display_value(definition)} "
                    f"({source})"
                )
        return lines

    # ------------------------------------------------------------------
    # Values and validation

    def _display_value(self, definition: SettingDefinition) -> str:
        """How a setting's value may be shown.

        A secret is never rendered, set or not: the placeholder is the whole
        answer once one exists, so no reply, log line or screen share can leak
        a credential back out of the bot.
        """
        is_set = self._bot.settings_store.is_set(definition)
        if definition.secret:
            return SECRET_PLACEHOLDER if is_set else UNSET_DISPLAY
        current = getattr(self._bot._config, definition.field, None)
        if current is None:
            return UNSET_DISPLAY
        return f"`{current}`" if is_set else f"`{current}` (default)"

    def _fallback_note(self, definition: SettingDefinition) -> str:
        current = getattr(self._bot._config, definition.field, None)
        if current is None:
            return ""
        if definition.default_from is not None:
            return (
                f"It follows `/settings {definition.group} "
                f"{definition.default_from}` again: `{current}`."
            )
        return f"It falls back to its default: `{current}`."

    async def _validate(
        self,
        interaction: discord.Interaction,
        definition: SettingDefinition,
        parsed: object,
    ) -> str | None:
        """Check a Discord id resolves before it is stored.

        Storing an id nothing answers to is how a guild loses a feature
        silently - the command keeps working and simply never matches anyone -
        so an id that cannot be resolved is refused instead.
        """
        if definition.validates is None or not isinstance(parsed, int):
            return None
        guild = interaction.guild
        if guild is None:
            return "This command has to be run in the server."
        if definition.validates == ValidationTarget.ROLE:
            if guild.get_role(parsed) is None:
                return (
                    f"No role in this server has the id `{parsed}`. Turn on "
                    "Developer Mode and copy the role id, or pick one from "
                    "the suggestions."
                )
            return None
        if definition.validates == ValidationTarget.FORUM_TAG:
            return await self._validate_forum_tag(parsed)
        return await self._validate_channel(guild, definition, parsed)

    async def _validate_channel(
        self,
        guild: discord.Guild,
        definition: SettingDefinition,
        channel_id: int,
    ) -> str | None:
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._bot.fetch_channel(channel_id)
            except discord.DiscordException:
                return (
                    f"No channel with the id `{channel_id}` could be read. "
                    "Check the id and that the bot can see the channel."
                )
            if getattr(getattr(channel, "guild", None), "id", None) != guild.id:
                return "That channel is not in this server."
        wants_forum = definition.validates == ValidationTarget.FORUM_CHANNEL
        is_forum = isinstance(channel, discord.ForumChannel)
        if wants_forum and not is_forum:
            return f"`{channel_id}` is not a forum channel."
        if not wants_forum and is_forum:
            return f"`{channel_id}` is a forum channel, not a text channel."
        return None

    async def _validate_forum_tag(self, tag_id: int) -> str | None:
        forum_id = self._bot._config.trial_forum_channel_id
        try:
            forum = await self._bot.fetch_channel(forum_id)
        except discord.DiscordException:
            return (
                "The Trial application forum could not be read, so the tag "
                f"could not be checked. Set `/settings {CHANNELS_GROUP} "
                "trial_forum` first."
            )
        if not forum_tags_for_ids(forum, {tag_id}):
            return (
                f"The Trial application forum has no tag with the id "
                f"`{tag_id}`. Pick one from the suggestions."
            )
        return None


def _short_description(definition: SettingDefinition) -> str:
    """Discord caps a command description at 100 characters."""
    if definition.legacy_variable is not None:
        text = f"Set {definition.legacy_variable.lower()} (was {definition.legacy_variable})"
    else:
        text = definition.description
    if len(text) <= CHOICE_NAME_LIMIT:
        return text
    return text[: CHOICE_NAME_LIMIT - 1].rstrip() + "…"


def _restart_note(restarted: list[str]) -> str:
    if not restarted:
        return ""
    return "\nRestarted: " + ", ".join(restarted) + "."


def _autocomplete_for(
    commands: SettingsCommands,
    definition: SettingDefinition,
) -> Any:
    async def autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        # Autocomplete must never raise: an unauthorized caller simply sees no
        # suggestions, and the command itself still enforces the gate.
        if not user_has_role(
            interaction.user,
            commands._bot._config.raffle_officer_role_id,
        ) and not _is_owner_or_admin(interaction):
            return []
        try:
            choices = await _suggestions(commands, definition, interaction)
        except discord.DiscordException:
            LOGGER.debug(
                "Settings autocomplete could not reach Discord; setting=%s",
                setting_key(definition),
            )
            return []
        text = current.strip().casefold()
        matched = [
            choice
            for choice in choices
            if not text or text in choice.name.casefold() or text in choice.value
        ]
        LOGGER.debug(
            "Returning settings autocomplete choices; setting=%s choices=%s",
            setting_key(definition),
            len(matched[:AUTOCOMPLETE_LIMIT]),
        )
        return matched[:AUTOCOMPLETE_LIMIT]

    return autocomplete


def _is_owner_or_admin(interaction: discord.Interaction) -> bool:
    guild = interaction.guild
    user = interaction.user
    if guild is not None and getattr(guild, "owner_id", None) == user.id:
        return True
    permissions = getattr(user, "guild_permissions", None)
    return bool(getattr(permissions, "administrator", False))


async def _suggestions(
    commands: SettingsCommands,
    definition: SettingDefinition,
    interaction: discord.Interaction,
) -> list[app_commands.Choice[str]]:
    guild = interaction.guild
    if guild is None:
        return []
    if definition.validates == ValidationTarget.ROLE:
        return [
            app_commands.Choice(name=_choice_name(role.name), value=str(role.id))
            for role in guild.roles
            if not role.is_default()
        ]
    if definition.validates == ValidationTarget.FORUM_TAG:
        forum = await commands._bot.fetch_channel(
            commands._bot._config.trial_forum_channel_id
        )
        return [
            app_commands.Choice(
                name=_choice_name(str(getattr(tag, "name", tag_id))),
                value=str(tag_id),
            )
            for tag in getattr(forum, "available_tags", ())
            if (tag_id := safe_int(getattr(tag, "id", None))) is not None
        ]
    wants_forum = definition.validates == ValidationTarget.FORUM_CHANNEL
    return [
        app_commands.Choice(
            name=_choice_name(f"#{channel.name}"),
            value=str(channel.id),
        )
        for channel in guild.channels
        if isinstance(channel, discord.ForumChannel) is wants_forum
        and isinstance(channel, discord.ForumChannel | discord.TextChannel)
    ]


def _choice_name(name: str) -> str:
    if len(name) <= CHOICE_NAME_LIMIT:
        return name
    return name[: CHOICE_NAME_LIMIT - 1].rstrip() + "…"
