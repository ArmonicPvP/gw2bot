from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from discord import app_commands
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.config import ConfigurationError, same_guild_id
from gw2bot.gw2_api import Gw2ApiClient
from gw2bot.discord_utils import (
    forum_tags_for_ids,
    log_discord_failure,
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
DESCRIPTION_LIMIT = 100
# Discord rejects a message over 2000 characters outright, which would make
# /settings list fail as a whole rather than print a little less.
MESSAGE_LIMIT = 1900

# Anything that is only whitespace clears the setting, which is how the
# request describes it: "a space or nothing means unset".
UNSET_INPUT = ""

# Shown instead of "(set)" when a row exists but its text no longer parses.
# compose_config has already fallen back to the default, so the value on
# screen is real - it is the claim that somebody chose it that would be false.
UNREADABLE_NOTE = "default — the stored value could not be read; set it again"

# _apply's "no previous value was captured, so do not roll back" marker. None
# is a real previous value - it means the setting was unset - so it cannot
# double as the sentinel.
_KEEP: Any = object()


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
            # A read answers from SQLite alone and comfortably beats Discord's
            # three-second window, so it replies directly.
            await self._report(interaction, definition)
            return
        # Changing one can validate against Discord, rebuild the API client
        # and restart the calendar first. Past three seconds the interaction
        # token expires and the confirmation is lost - with the value already
        # stored - leaving the officer unsure whether it took.
        await self._defer(interaction)
        if value.strip() == UNSET_INPUT:
            await self._unset(interaction, definition)
            return
        await self._store(interaction, definition, value)

    async def _defer(self, interaction: discord.Interaction) -> None:
        """Buy time for a change, privately.

        send_interaction_notice already picks the followup once the response
        is done, so deferring here is all the rest of the flow needs to know
        about it. A refused defer is logged rather than raised: the change is
        still worth attempting, and the notice will report what happened.
        """
        if interaction.response.is_done():
            return
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.DiscordException as error:
            log_discord_failure(
                "Could not defer a settings command; the reply may not arrive",
                error,
            )

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
            "Reported a setting; setting=%s encrypted=%s",
            setting_key(definition),
            definition.encrypted,
        )

    async def _unset(
        self,
        interaction: discord.Interaction,
        definition: SettingDefinition,
    ) -> None:
        key = setting_key(definition)
        try:
            # Inside the guard with the write it protects: a database that
            # cannot be read cannot be rolled back to either, and the caller
            # still has to be told rather than meeting an unhandled error.
            previous = self._bot.settings_store.get_raw(definition)
            removed = self._bot.settings_store.unset(definition)
        except SQLAlchemyError:
            LOGGER.error("Could not unset a setting; setting=%s", key)
            await send_interaction_notice(
                interaction,
                "The settings database could not be written. Nothing was "
                "changed.",
            )
            return
        applied = await self._apply(interaction, definition, previous)
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
            # The snapshot is kept so a change that cannot be applied can be
            # put back; leaving a value the running bot rejected would apply
            # it on the next restart, with no command there to report why.
            # It is read inside the guard with the write it protects.
            previous = self._bot.settings_store.get_raw(definition)
            self._bot.settings_store.set_raw(definition, stored)
        except SQLAlchemyError:
            LOGGER.error("Could not store a setting; setting=%s", key)
            await send_interaction_notice(
                interaction,
                "The settings database could not be written. Nothing was "
                "changed.",
            )
            return
        applied = await self._apply(interaction, definition, previous)
        if applied is None:
            return
        shown = SECRET_PLACEHOLDER if definition.encrypted else f"`{stored}`"
        await send_interaction_notice(
            interaction,
            f"**{definition.command_path}** is now {shown}."
            + _restart_note(applied)
            + self._unverified_guild_note(definition),
        )
        LOGGER.debug(
            "Stored a setting from Discord; setting=%s encrypted=%s "
            "characters=%s",
            key,
            definition.encrypted,
            len(stored),
        )

    async def _apply(
        self,
        interaction: discord.Interaction,
        definition: SettingDefinition,
        previous: str | None = _KEEP,
    ) -> list[str] | None:
        """Reload the configuration, reporting what had to be restarted.

        Returns None once the caller has been told the change could not be
        applied, so every caller only has to stop.

        When ``previous`` is supplied and applying fails, the stored value is
        put back and the configuration recomposed from it. A value the running
        bot has already rejected must not survive in the database, because the
        next startup would apply it with no command there to report why the
        bot will not come up.
        """
        try:
            return await self._bot.apply_settings_change({definition.field})
        except Exception as exc:
            LOGGER.error(
                "Could not apply a settings change; setting=%s error_type=%s",
                setting_key(definition),
                type(exc).__name__,
            )
            restored = (
                False
                if previous is _KEEP
                else await self._roll_back(definition, previous)
            )
            await send_interaction_notice(
                interaction,
                f"That change could not be applied: {exc}"
                + (
                    "\nThe previous value is back in place."
                    if restored
                    else "\nRestart the bot to pick it up."
                ),
            )
            return None

    async def _roll_back(
        self,
        definition: SettingDefinition,
        previous: str | None,
    ) -> bool:
        """Put a rejected value back, reporting whether it worked.

        The change is applied a second time after the restore so the running
        bot matches what the database now holds, rather than staying on the
        half-applied state the failure left behind.
        """
        key = setting_key(definition)
        try:
            if previous is None:
                self._bot.settings_store.unset(definition)
            else:
                self._bot.settings_store.set_raw(definition, previous)
            await self._bot.apply_settings_change({definition.field})
        except Exception as exc:
            # Nothing further can be done from here; the operator needs to
            # know the database still holds a value the bot would not take.
            LOGGER.error(
                "Could not roll a rejected setting back; setting=%s "
                "error_type=%s",
                key,
                type(exc).__name__,
            )
            return False
        LOGGER.debug("Rolled a rejected setting back; setting=%s", key)
        return True

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
        messages = chunk_lines(lines)
        for message in messages:
            await send_interaction_notice(interaction, message)
        LOGGER.debug(
            "Reported the settings list; settings=%s messages=%s",
            len(lines),
            len(messages),
        )

    def _list_lines(self) -> list[str]:
        lines = ["**Bot settings**"]
        for group in (None, ROLES_GROUP, CHANNELS_GROUP):
            definitions = definitions_in_group(group)
            if not definitions:
                continue
            if group is not None:
                lines.append(f"\n__/settings {group}__")
            for definition in definitions:
                if not self._bot.settings_store.is_set(definition):
                    source = "default"
                elif self._stored_value_parses(definition):
                    source = "set"
                else:
                    source = "unreadable"
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
        if definition.encrypted:
            return SECRET_PLACEHOLDER if is_set else UNSET_DISPLAY
        current = getattr(self._bot._config, definition.field, None)
        if current is None:
            return UNSET_DISPLAY
        if is_set and not self._stored_value_parses(definition):
            return f"`{current}` ({UNREADABLE_NOTE})"
        return f"`{current}`" if is_set else f"`{current}` (default)"

    def _stored_value_parses(self, definition: SettingDefinition) -> bool:
        """Whether the stored text is the value the bot is actually running.

        compose_config drops a stored value its parser rejects and falls back
        to the default, logging it to the console. Without this the row would
        still read as set here and the reply would show the default as though
        somebody had chosen it - which is the one question /settings list
        exists to answer.
        """
        raw = self._bot.settings_store.get_raw(definition)
        if raw is None:
            # Either nothing is stored, or it is an encrypted row this key
            # cannot read; the caller answered for both before reaching here.
            return True
        try:
            definition.parse(raw)
        except Exception:
            # Deliberately as broad as compose_config's own catch: a parser
            # may raise anything, and the two have to agree on what counts as
            # unusable or the display would contradict the running config.
            return False
        return True

    def _fallback_note(self, definition: SettingDefinition) -> str:
        # No secret's value is printed here either. None of them has a
        # fallback today, so this guards the day one gains a default rather
        # than the code as it stands.
        if definition.encrypted:
            return ""
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
        if definition.field == "gw2_guild_id" and isinstance(parsed, str):
            return await self._validate_guild_id(parsed)
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

    async def _validate_guild_id(self, guild_id: str) -> str | None:
        """Refuse a guild id the ledger or the API has already ruled out.

        The ledger records the guild it belongs to and rejects a different
        one. Finding that out only while applying the change would leave the
        new id persisted and the bot unable to start next time, so the check
        happens before anything is written - and because that record is
        written once and never cleared, a wrong id has to be caught here or
        not at all.
        """
        try:
            bound = self._bot.raffle_store.bound_guild()
        except SQLAlchemyError:
            LOGGER.error(
                "Could not read the raffle ledger's guild binding; setting="
                "gw2_guild_id"
            )
            return (
                "The raffle database could not be read, so this could not be "
                "checked against it. Nothing was changed; try again in a "
                "moment."
            )
        if bound is not None and not same_guild_id(bound, guild_id):
            return (
                "The raffle ledger already belongs to a different guild, so "
                "its tickets and history could not be read as this one's. "
                "Point RAFFLE_DB_PATH at a different database to run a "
                "second guild's ledger."
            )
        return await self._reject_guild_the_key_does_not_lead(guild_id)

    async def _reject_guild_the_key_does_not_lead(
        self,
        guild_id: str,
    ) -> str | None:
        """Check the id against /v2/account.guild_leader while a key exists.

        docs/gw2-api.md already names this as the check to make before polling
        a guild, and here it is the one thing standing between a well-formed
        but wrong id and a ledger permanently claimed by it. Without a key
        there is nothing to ask, so the id's shape is all that guards it -
        and _store says so in the reply rather than letting it pass silently.
        """
        api = self._guild_api_client()
        if api is None:
            LOGGER.debug("Skipped the guild id API check; no API key is set")
            return None
        try:
            account = await api.get_account()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            # An outage must not stop an officer correcting a setting, and the
            # shape check has already run, so this reports and allows.
            LOGGER.warning(
                "Could not check gw2_guild_id against the Guild Wars 2 API; "
                "storing it unchecked. error_type=%s",
                type(exc).__name__,
            )
            return None
        led = account.get("guild_leader") or ()
        # The API answers in upper case; the stored value is canonical lower
        # case. Comparing the raw text would refuse a correct id.
        if any(same_guild_id(candidate, guild_id) for candidate in led):
            LOGGER.debug("Guild id confirmed against guild_leader")
            return None
        LOGGER.debug(
            "Refused a guild id the API key does not lead; led_count=%s",
            len(led),
        )
        return (
            "The configured API key does not lead a guild with that id, so "
            "every Guild Wars 2 request for it would fail. Check the id in "
            "/v2/account.guild_leader. Nothing was changed - the raffle "
            "ledger records the first guild id it is given and refuses a "
            "different one afterwards, so a wrong one is worth catching here."
        )

    def _guild_api_client(self) -> Gw2ApiClient | None:
        """The client to ask about a guild id, building one if need be.

        Gw2Bot builds self._api only once the key and the guild id are both
        set, so on a clean install there is none - which is exactly when the
        first guild id is about to claim the ledger for good. Borrowing the
        running client when there is one and building a throwaway from the
        stored key otherwise makes the check run in the order the README
        recommends, key first.
        """
        if self._bot._api is not None:
            return self._bot._api
        config = self._bot._config
        session = self._bot._session
        if session is None or config.gw2_api_key is None:
            return None
        LOGGER.debug("Built a temporary API client to check a guild id")
        return Gw2ApiClient(
            session,
            config.gw2_api_base_url,
            config.gw2_api_key,
        )

    def _unverified_guild_note(self, definition: SettingDefinition) -> str:
        """Say when a guild id was stored without being checked.

        The ledger is claimed by it and cannot be re-pointed from Discord, so
        an officer who mistyped has to hear about it now rather than when the
        first poll fails.
        """
        if definition.field != "gw2_guild_id":
            return ""
        if self._guild_api_client() is not None:
            return ""
        return (
            "\nIt could not be checked against the Guild Wars 2 API, because "
            "no `/settings gw2_api_key` is set. The raffle ledger now belongs "
            "to this guild id and cannot be pointed at another one from "
            "Discord, so check it is right."
        )

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
        # Check for the type the setting actually needs rather than merely
        # ruling the other one out: a category or a voice channel would pass a
        # "not a forum" test and then fail at delivery, where channel.send
        # does not exist and the AttributeError falls outside every caller's
        # discord.DiscordException handling.
        if definition.validates == ValidationTarget.FORUM_CHANNEL:
            if not isinstance(channel, discord.ForumChannel):
                return f"`{channel_id}` is not a forum channel."
            return None
        if not isinstance(channel, discord.TextChannel):
            return (
                f"`{channel_id}` is not a text channel the bot can post in."
            )
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
    """The first sentence of the description, within Discord's 100 characters.

    A setting whose subcommand had to be renamed to fit Discord's 32-character
    name limit names the variable it replaced instead, because that rename is
    the one thing an operator reading the command list cannot guess.
    """
    variable = definition.legacy_variable
    if variable is not None and variable.lower() != definition.name:
        return _truncate(f"Was the {variable} environment variable")
    sentence = definition.description.split(". ")[0].rstrip(".")
    return _truncate(sentence)


def _truncate(text: str, limit: int = DESCRIPTION_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


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
        except (
            discord.DiscordException,
            aiohttp.ClientError,
            asyncio.TimeoutError,
        ) as exc:
            # The forum-tag branch fetches a channel, so a transport failure
            # arrives as an aiohttp or timeout error rather than a Discord
            # one, and letting either escape breaks the promise above.
            LOGGER.debug(
                "Settings autocomplete could not reach Discord; setting=%s "
                "error_type=%s",
                setting_key(definition),
                type(exc).__name__,
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
    return _truncate(name, CHOICE_NAME_LIMIT)


def chunk_lines(lines: list[str], limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split a report into messages Discord will accept.

    The settings list grows with every setting added, so it is split by length
    rather than trusting it to stay under the limit; going over would fail the
    whole reply.
    """
    messages: list[str] = []
    current: list[str] = []
    length = 0
    for line in lines:
        addition = len(line) + 1
        if current and length + addition > limit:
            messages.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += addition
    if current:
        messages.append("\n".join(current))
    return messages
