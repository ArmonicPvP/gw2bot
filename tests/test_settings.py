import logging
import re
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from cryptography.fernet import Fernet
from discord import app_commands

from factories import (
    default_config,
    settings_interaction,
    settings_reply,
    settings_store,
)
from gw2bot.config import BootstrapConfig, ConfigurationError
from gw2bot.logging_setup import RedactingFormatter, SecretRegistry
from gw2bot.settings.commands import SettingsCommands, chunk_lines
from gw2bot.settings.composition import compose_config
from gw2bot.settings.crypto import (
    ENCRYPTION_KEY_VARIABLE,
    SettingsCipher,
    key_file_path,
)
from gw2bot.settings.definitions import (
    CHANNELS_GROUP,
    COMMAND_NAME_LIMIT,
    GROUP_OPTION_LIMIT,
    LEGACY_SETTINGS,
    ROLES_GROUP,
    SECRET_PLACEHOLDER,
    SETTING_DEFINITIONS,
    UNSET_DISPLAY,
    definition_for,
    setting_key,
)
from gw2bot.settings.migration import (
    import_legacy_environment,
    legacy_variables_present,
)
from gw2bot.settings.store import SettingsStore

BOOTSTRAP = BootstrapConfig(
    discord_token="discord-token",
    discord_command_guild_id=5678,
)

OFFICER_ROLE_ID = default_config().raffle_officer_role_id
# Discord's own rules for a slash command name.
COMMAND_NAME_PATTERN = re.compile(r"^[-_a-z0-9]{1,32}$")


@pytest.fixture
def store(tmp_path: Path):
    opened = settings_store(tmp_path)
    yield opened
    opened.close()


class TestSettingsStore:
    def test_round_trips_a_plain_setting(self, store: SettingsStore) -> None:
        definition = definition_for("gw2_guild_id")

        assert not store.is_set(definition)
        assert store.get_raw(definition) is None

        store.set_raw(definition, "guild-id")

        assert store.is_set(definition)
        assert store.get_raw(definition) == "guild-id"

        assert store.unset(definition)
        assert not store.is_set(definition)
        assert store.get_raw(definition) is None

    def test_round_trips_a_secret(self, store: SettingsStore) -> None:
        definition = definition_for("gw2_api_key")

        store.set_raw(definition, "gw2-secret")

        assert store.is_set(definition)
        assert store.get_raw(definition) == "gw2-secret"

    def test_unset_reports_that_there_was_nothing_to_remove(
        self,
        store: SettingsStore,
    ) -> None:
        assert not store.unset(definition_for("gw2_guild_id"))

    def test_overwrites_an_existing_value(self, store: SettingsStore) -> None:
        definition = definition_for("gw2_guild_id")

        store.set_raw(definition, "first")
        store.set_raw(definition, "second")

        assert store.get_raw(definition) == "second"

    def test_raw_values_are_keyed_by_group(self, store: SettingsStore) -> None:
        store.set_raw(definition_for("gw2_guild_id"), "guild-id")
        store.set_raw(definition_for("raffle_draw", ROLES_GROUP), "42")

        assert dict(store.raw_values()) == {
            "gw2_guild_id": "guild-id",
            "roles.raffle_draw": "42",
        }


class TestSettingsEncryption:
    def test_a_secret_is_not_stored_in_the_clear(self, tmp_path: Path) -> None:
        database = tmp_path / "gw2bot.db"
        store = SettingsStore(str(database), SettingsCipher(Fernet.generate_key()))
        store.set_raw(definition_for("gw2_api_key"), "gw2-secret")
        store.close()

        assert b"gw2-secret" not in database.read_bytes()

    def test_a_plain_setting_is_stored_as_written(self, tmp_path: Path) -> None:
        # Only the credentials are encrypted; encrypting a guild id would cost
        # a key rotation without protecting anything.
        database = tmp_path / "gw2bot.db"
        store = SettingsStore(str(database), SettingsCipher(Fernet.generate_key()))
        store.set_raw(definition_for("gw2_guild_id"), "guild-id")
        store.close()

        assert b"guild-id" in database.read_bytes()

    def test_a_secret_the_key_cannot_read_is_treated_as_unset(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        database = tmp_path / "gw2bot.db"
        definition = definition_for("gw2_api_key")
        original = SettingsStore(str(database), SettingsCipher(Fernet.generate_key()))
        original.set_raw(definition, "gw2-secret")
        original.close()

        replaced = SettingsStore(str(database), SettingsCipher(Fernet.generate_key()))
        with caplog.at_level(logging.ERROR, logger="gw2bot"):
            value = replaced.get_raw(definition)
        # The row is still there, so /settings can say the value exists and
        # cannot be read rather than claiming nobody ever set it.
        still_set = replaced.is_set(definition)
        replaced.close()

        assert value is None
        assert still_set
        assert "Could not decrypt a stored secret" in caplog.text
        assert "gw2_api_key" in caplog.text

    def test_a_decrypt_failure_never_logs_the_ciphertext(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        database = tmp_path / "gw2bot.db"
        definition = definition_for("gw2_api_key")
        original = SettingsStore(str(database), SettingsCipher(Fernet.generate_key()))
        original.set_raw(definition, "gw2-secret")
        stored = original.raw_values()
        original.close()
        assert stored

        replaced = SettingsStore(str(database), SettingsCipher(Fernet.generate_key()))
        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            replaced.get_raw(definition)
        replaced.close()

        assert "gw2-secret" not in caplog.text

    def test_generates_a_private_key_file_beside_the_database(
        self,
        tmp_path: Path,
    ) -> None:
        database = tmp_path / "data" / "gw2bot.db"
        database.parent.mkdir()

        cipher = SettingsCipher.for_database(str(database), {})
        key_file = key_file_path(str(database))

        assert key_file.exists()
        # World- or group-readable would put every stored credential one
        # container escape away.
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
        assert cipher.decrypt(cipher.encrypt("value"), "gw2_api_key") == "value"

    def test_reuses_the_key_file_across_restarts(self, tmp_path: Path) -> None:
        database = tmp_path / "gw2bot.db"

        first = SettingsCipher.for_database(str(database), {})
        token = first.encrypt("gw2-secret")
        second = SettingsCipher.for_database(str(database), {})

        assert second.decrypt(token, "gw2_api_key") == "gw2-secret"

    def test_the_environment_key_wins_over_the_file(self, tmp_path: Path) -> None:
        database = tmp_path / "gw2bot.db"
        key = Fernet.generate_key().decode("ascii")
        # A key file is left behind by an earlier run that had no variable.
        SettingsCipher.for_database(str(database), {})

        cipher = SettingsCipher.for_database(
            str(database),
            {ENCRYPTION_KEY_VARIABLE: key},
        )
        token = cipher.encrypt("gw2-secret")

        assert Fernet(key.encode()).decrypt(token.encode()).decode() == "gw2-secret"

    def test_a_runtime_secret_is_redacted_by_the_installed_formatter(self) -> None:
        # configure_logging freezes its handler at startup, long before
        # /settings can hand the bot a new key, so the registry it holds has
        # to be the same object the bot keeps registering into.
        secrets = SecretRegistry(("discord-token",))
        formatter = RedactingFormatter("%(message)s", secrets)
        record = logging.LogRecord(
            "gw2bot",
            logging.INFO,
            __file__,
            1,
            "key is gw2-secret",
            None,
            None,
        )

        assert "gw2-secret" in formatter.format(record)

        secrets.add("gw2-secret")

        assert "gw2-secret" not in formatter.format(record)
        assert "[REDACTED]" in formatter.format(record)


class TestSettingDefinitions:
    def test_every_command_name_is_one_discord_accepts(self) -> None:
        # An over-long or malformed name is rejected at sync time, and Discord
        # rejects the whole tree rather than the one command, so this would
        # take every other command down with it.
        for definition in SETTING_DEFINITIONS:
            assert len(definition.name) <= COMMAND_NAME_LIMIT
            assert COMMAND_NAME_PATTERN.match(definition.name)

    def test_every_description_fits_discord_s_limit(self) -> None:
        commands = SettingsCommands(cast(Any, _fake_bot()))
        for command in _walk(commands):
            assert len(command.description) <= 100

    def test_no_group_exceeds_discord_s_option_limit(self) -> None:
        commands = SettingsCommands(cast(Any, _fake_bot()))
        groups = [commands, *(_subgroups(commands))]

        for group in groups:
            assert len(group.commands) <= GROUP_OPTION_LIMIT

    def test_setting_keys_are_unique(self) -> None:
        keys = [setting_key(definition) for definition in SETTING_DEFINITIONS]

        assert len(keys) == len(set(keys))

    def test_every_definition_names_a_config_field(self) -> None:
        config = default_config()
        for definition in SETTING_DEFINITIONS:
            assert hasattr(config, definition.field), definition.name

    def test_only_credentials_are_marked_secret(self) -> None:
        secrets = {
            definition.name
            for definition in SETTING_DEFINITIONS
            if definition.secret
        }

        assert secrets == {
            "gw2_api_key",
            "discord_oauth_client_secret",
            "web_session_secret",
        }


class TestSettingParsers:
    @pytest.mark.parametrize(
        ("name", "value", "message"),
        (
            ("gw2_poll_interval_seconds", "10", "must be at least 30"),
            ("guild_log_poll_interval_seconds", "10", "must be at least 30"),
            ("gw2_guild_member_cache_seconds", "0", "greater than zero"),
            ("gw2_guild_member_cache_seconds", "soon", "must be an integer"),
            ("timezone", "Mars/Olympus_Mons", "valid IANA timezone name"),
            ("web_base_url", "calendar.test", "must start with http"),
            ("web_session_secret", "short", "at least 32 characters"),
            ("discord_notification_channel_id", "0", "greater than zero"),
        ),
    )
    def test_rejects_an_unusable_value(
        self,
        name: str,
        value: str,
        message: str,
    ) -> None:
        with pytest.raises(ConfigurationError, match=message):
            definition_for(name).parse(value)

    @pytest.mark.parametrize(
        ("written", "expected"),
        (
            ("9012", 9012),
            ("<#9012>", 9012),
            ("  9012  ", 9012),
        ),
    )
    def test_accepts_a_pasted_channel_mention(
        self,
        written: str,
        expected: int,
    ) -> None:
        # Discord's copy-link and mention syntax both end up pasted into a
        # text option, and refusing them would look like the id was wrong.
        assert definition_for("discord_notification_channel_id").parse(written) == expected

    def test_accepts_a_pasted_role_mention(self) -> None:
        assert definition_for("raffle_draw", ROLES_GROUP).parse("<@&42>") == 42


class TestComposition:
    def test_unset_settings_fall_back_to_their_defaults(self) -> None:
        config = compose_config(BOOTSTRAP, {})

        assert config.poll_interval_seconds == 300
        assert config.event_timezone == "UTC"
        assert config.gw2_api_key is None
        assert config.raffle_officer_role_id == OFFICER_ROLE_ID

    def test_stored_settings_win_over_the_defaults(self) -> None:
        config = compose_config(
            BOOTSTRAP,
            {"gw2_poll_interval_seconds": "600", "roles.raffle_officer": "42"},
        )

        assert config.poll_interval_seconds == 600
        assert config.raffle_officer_role_id == 42

    def test_the_food_page_role_follows_the_raffle_draw_role(self) -> None:
        config = compose_config(BOOTSTRAP, {"roles.raffle_draw": "42"})

        assert config.food_page_role_id == 42

    def test_the_food_page_role_stops_following_once_it_is_set(self) -> None:
        config = compose_config(
            BOOTSTRAP,
            {"roles.raffle_draw": "42", "roles.food_page": "99"},
        )

        assert config.raffle_draw_role_id == 42
        assert config.food_page_role_id == 99

    def test_an_unusable_stored_value_reads_as_unset(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The bot is already running by the time a settings change recomposes,
        # so one bad row must not take the whole configuration down.
        with caplog.at_level(logging.ERROR, logger="gw2bot"):
            config = compose_config(BOOTSTRAP, {"gw2_poll_interval_seconds": "1"})

        assert config.poll_interval_seconds == 300
        assert "set it again with /settings gw2_poll_interval_seconds" in caplog.text

    def test_bootstrap_values_are_not_overridable_by_settings(self) -> None:
        bootstrap = BootstrapConfig(
            discord_token="discord-token",
            discord_command_guild_id=5678,
            debug=True,
            raffle_db_path="/app/data/gw2bot.db",
            web_port=9090,
        )

        config = compose_config(bootstrap, {})

        assert config.debug
        assert config.raffle_db_path == "/app/data/gw2bot.db"
        assert config.web_port == 9090


class TestLegacyEnvironmentImport:
    def test_imports_the_variables_that_are_set(self, store: SettingsStore) -> None:
        imported = import_legacy_environment(
            store,
            {"GW2_API_KEY": "gw2-secret", "TZ": "America/New_York"},
        )

        assert set(imported) == {"GW2_API_KEY", "TZ"}
        assert store.get_raw(definition_for("gw2_api_key")) == "gw2-secret"
        assert store.get_raw(definition_for("timezone")) == "America/New_York"

    def test_runs_only_once(self, store: SettingsStore) -> None:
        environment = {"GW2_API_KEY": "gw2-secret"}
        import_legacy_environment(store, environment)
        store.unset(definition_for("gw2_api_key"))

        imported = import_legacy_environment(store, environment)

        # Re-importing would silently undo an unset every time the container
        # restarted, with nothing on screen to explain it.
        assert imported == ()
        assert not store.is_set(definition_for("gw2_api_key"))

    def test_a_value_already_set_is_left_alone(self, store: SettingsStore) -> None:
        store.set_raw(definition_for("gw2_api_key"), "chosen-in-discord")

        imported = import_legacy_environment(store, {"GW2_API_KEY": "from-env"})

        assert imported == ()
        assert store.get_raw(definition_for("gw2_api_key")) == "chosen-in-discord"

    def test_an_unusable_value_is_rejected_and_named(
        self,
        store: SettingsStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="gw2bot"):
            imported = import_legacy_environment(
                store,
                {"GW2_POLL_INTERVAL_SECONDS": "10"},
            )

        assert imported == ()
        assert not store.is_set(definition_for("gw2_poll_interval_seconds"))
        assert "GW2_POLL_INTERVAL_SECONDS" in caplog.text

    def test_blank_variables_are_not_imported(self, store: SettingsStore) -> None:
        assert import_legacy_environment(store, {"GW2_API_KEY": "   "}) == ()
        assert not store.is_set(definition_for("gw2_api_key"))

    def test_the_discord_ids_have_nothing_to_import(self) -> None:
        # They were source literals, never variables, so there is nothing to
        # copy and nothing to warn about.
        assert all(
            definition.legacy_variable is None
            for definition in SETTING_DEFINITIONS
            if definition.group is not None
        )
        assert all(
            definition.group is None for definition in LEGACY_SETTINGS
        )

    def test_reports_the_variables_still_in_the_environment(self) -> None:
        present = legacy_variables_present(
            {"GW2_API_KEY": "gw2-secret", "DISCORD_TOKEN": "token", "TZ": " "}
        )

        # DISCORD_TOKEN is a bootstrap variable and belongs there; TZ is blank.
        assert present == ("GW2_API_KEY",)


class TestSettingsCommandGate:
    async def test_an_officer_may_change_a_setting(self, tmp_path: Path) -> None:
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        allowed = await commands.authorize(cast(discord.Interaction, interaction))
        bot.settings_store.close()

        assert allowed

    async def test_a_plain_member_may_not(self, tmp_path: Path) -> None:
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction()

        allowed = await commands.authorize(cast(discord.Interaction, interaction))
        bot.settings_store.close()

        assert not allowed
        assert "do not have the required role" in settings_reply(interaction)

    @pytest.mark.parametrize(
        ("owner", "administrator"),
        ((True, False), (False, True)),
    )
    async def test_the_owner_and_an_administrator_survive_a_bad_officer_role(
        self,
        tmp_path: Path,
        owner: bool,
        administrator: bool,
    ) -> None:
        # The officer role is a setting now. If it were the only way in, one
        # wrong value would lock everybody out of the command that fixes it,
        # with no environment variable left to override it.
        commands, bot = _commands(tmp_path)
        bot._config = default_config(raffle_officer_role_id=999_999)
        interaction = settings_interaction(
            owner=owner,
            administrator=administrator,
        )

        allowed = await commands.authorize(cast(discord.Interaction, interaction))
        bot.settings_store.close()

        assert allowed

    async def test_a_plain_member_is_still_refused_with_a_bad_officer_role(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        bot._config = default_config(raffle_officer_role_id=999_999)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        allowed = await commands.authorize(cast(discord.Interaction, interaction))
        bot.settings_store.close()

        assert not allowed


class TestSettingsCommandBehaviour:
    async def test_reports_the_description_and_value_without_an_argument(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "timezone", interaction, None)
        bot.settings_store.close()

        reply = settings_reply(interaction)
        assert "/settings timezone" in reply
        assert "IANA timezone name" in reply
        assert "`UTC` (default)" in reply

    async def test_never_reports_a_secret_s_value(self, tmp_path: Path) -> None:
        commands, bot = _commands(tmp_path)
        bot.settings_store.set_raw(definition_for("gw2_api_key"), "gw2-secret")
        bot._config = default_config(gw2_api_key="gw2-secret")
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "gw2_api_key", interaction, None)
        bot.settings_store.close()

        reply = settings_reply(interaction)
        assert SECRET_PLACEHOLDER in reply
        assert "gw2-secret" not in reply

    async def test_reports_an_unset_secret_as_unset(self, tmp_path: Path) -> None:
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "gw2_api_key", interaction, None)
        bot.settings_store.close()

        assert UNSET_DISPLAY in settings_reply(interaction)

    async def test_stores_a_value(self, tmp_path: Path) -> None:
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "timezone", interaction, "America/New_York")
        stored = bot.settings_store.get_raw(definition_for("timezone"))
        bot.settings_store.close()

        assert stored == "America/New_York"
        assert "America/New_York" in settings_reply(interaction)
        bot.apply_settings_change.assert_awaited_once_with({"event_timezone"})

    async def test_confirming_a_secret_does_not_echo_it(self, tmp_path: Path) -> None:
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "gw2_api_key", interaction, "gw2-secret")
        bot.settings_store.close()

        reply = settings_reply(interaction)
        assert SECRET_PLACEHOLDER in reply
        assert "gw2-secret" not in reply

    async def test_a_secret_never_reaches_the_debug_log(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            await _run(commands, "gw2_api_key", interaction, "gw2-secret")
        bot.settings_store.close()

        assert "gw2-secret" not in caplog.text
        assert "setting=gw2_api_key" in caplog.text

    @pytest.mark.parametrize("written", (" ", "", "   "))
    async def test_whitespace_unsets_the_value(
        self,
        tmp_path: Path,
        written: str,
    ) -> None:
        commands, bot = _commands(tmp_path)
        bot.settings_store.set_raw(definition_for("timezone"), "America/New_York")
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "timezone", interaction, written)
        still_set = bot.settings_store.is_set(definition_for("timezone"))
        bot.settings_store.close()

        assert not still_set
        assert "no longer set" in settings_reply(interaction)

    async def test_unsetting_names_the_default_it_falls_back_to(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "timezone", interaction, " ")
        bot.settings_store.close()

        assert "falls back to its default: `UTC`" in settings_reply(interaction)

    async def test_a_rejected_value_is_not_stored(self, tmp_path: Path) -> None:
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "timezone", interaction, "Mars/Olympus_Mons")
        stored = bot.settings_store.is_set(definition_for("timezone"))
        bot.settings_store.close()

        assert not stored
        assert "valid IANA timezone name" in settings_reply(interaction)
        bot.apply_settings_change.assert_not_awaited()

    async def test_a_plain_member_cannot_read_a_value(self, tmp_path: Path) -> None:
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction()

        await _run(commands, "timezone", interaction, None)
        bot.settings_store.close()

        assert "do not have the required role" in settings_reply(interaction)

    async def test_lists_every_setting_and_where_its_value_came_from(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        bot.settings_store.set_raw(definition_for("timezone"), "America/New_York")
        bot._config = default_config(event_timezone="America/New_York")
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await cast(Any, commands.get_command("list")).callback(interaction)
        bot.settings_store.close()

        reply = settings_reply(interaction)
        assert "`timezone` — `America/New_York` (set)" in reply
        assert "`gw2_poll_interval_seconds` — `300` (default)" in reply
        assert f"/settings {ROLES_GROUP}" in reply
        assert f"/settings {CHANNELS_GROUP}" in reply

    async def test_the_list_never_shows_a_secret(self, tmp_path: Path) -> None:
        commands, bot = _commands(tmp_path)
        bot.settings_store.set_raw(definition_for("gw2_api_key"), "gw2-secret")
        bot._config = default_config(gw2_api_key="gw2-secret")
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await cast(Any, commands.get_command("list")).callback(interaction)
        bot.settings_store.close()

        reply = settings_reply(interaction)
        assert "gw2-secret" not in reply
        assert SECRET_PLACEHOLDER in reply


class TestDiscordIdValidation:
    async def test_stores_a_role_that_exists(self, tmp_path: Path) -> None:
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction(
            role_ids=(OFFICER_ROLE_ID,),
            guild=_guild(roles={42: "Officers"}),
        )

        await _run(commands, "raffle_draw", interaction, "42", ROLES_GROUP)
        stored = bot.settings_store.get_raw(
            definition_for("raffle_draw", ROLES_GROUP)
        )
        bot.settings_store.close()

        assert stored == "42"

    async def test_refuses_a_role_that_does_not_exist(self, tmp_path: Path) -> None:
        # Storing an id nothing answers to is how a guild loses a feature
        # silently: the gate keeps working and simply never matches anyone.
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction(
            role_ids=(OFFICER_ROLE_ID,),
            guild=_guild(roles={}),
        )

        await _run(commands, "raffle_draw", interaction, "42", ROLES_GROUP)
        stored = bot.settings_store.is_set(
            definition_for("raffle_draw", ROLES_GROUP)
        )
        bot.settings_store.close()

        assert not stored
        assert "No role in this server has the id `42`" in settings_reply(interaction)

    async def test_refuses_a_forum_where_a_text_channel_is_wanted(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        forum = _forum_channel(7)
        interaction = settings_interaction(
            role_ids=(OFFICER_ROLE_ID,),
            guild=_guild(channels={7: forum}),
        )

        await _run(
            commands,
            "raffle_contribution",
            interaction,
            "7",
            CHANNELS_GROUP,
        )
        stored = bot.settings_store.is_set(
            definition_for("raffle_contribution", CHANNELS_GROUP)
        )
        bot.settings_store.close()

        assert not stored
        assert "is a forum channel, not a text channel" in settings_reply(interaction)

    async def test_refuses_a_text_channel_where_a_forum_is_wanted(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        channel = _text_channel(7)
        interaction = settings_interaction(
            role_ids=(OFFICER_ROLE_ID,),
            guild=_guild(channels={7: channel}),
        )

        await _run(commands, "trial_forum", interaction, "7", CHANNELS_GROUP)
        stored = bot.settings_store.is_set(
            definition_for("trial_forum", CHANNELS_GROUP)
        )
        bot.settings_store.close()

        assert not stored
        assert "is not a forum channel" in settings_reply(interaction)

    async def test_refuses_a_tag_the_configured_forum_does_not_have(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        forum = _forum_channel(7, tags={11: "Accepted"})
        bot.fetch_channel = AsyncMock(return_value=forum)
        interaction = settings_interaction(
            role_ids=(OFFICER_ROLE_ID,),
            guild=_guild(channels={7: forum}),
        )

        await _run(
            commands,
            "trial_accepted_tag",
            interaction,
            "99",
            CHANNELS_GROUP,
        )
        stored = bot.settings_store.is_set(
            definition_for("trial_accepted_tag", CHANNELS_GROUP)
        )
        bot.settings_store.close()

        assert not stored
        assert "no tag with the id `99`" in settings_reply(interaction)

    async def test_accepts_a_tag_the_configured_forum_has(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        forum = _forum_channel(7, tags={11: "Accepted"})
        bot.fetch_channel = AsyncMock(return_value=forum)
        interaction = settings_interaction(
            role_ids=(OFFICER_ROLE_ID,),
            guild=_guild(channels={7: forum}),
        )

        await _run(
            commands,
            "trial_accepted_tag",
            interaction,
            "11",
            CHANNELS_GROUP,
        )
        stored = bot.settings_store.get_raw(
            definition_for("trial_accepted_tag", CHANNELS_GROUP)
        )
        bot.settings_store.close()

        assert stored == "11"

    async def test_unsetting_a_role_restores_the_shipped_default(
        self,
        tmp_path: Path,
    ) -> None:
        # Unset means "back to the default", never "off": a role gate that
        # could be switched off entirely would be a privilege escalation.
        commands, bot = _commands(tmp_path)
        definition = definition_for("raffle_draw", ROLES_GROUP)
        bot.settings_store.set_raw(definition, "42")
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "raffle_draw", interaction, " ", ROLES_GROUP)
        still_set = bot.settings_store.is_set(definition)
        bot.settings_store.close()

        assert not still_set
        assert "falls back to its default" in settings_reply(interaction)


def _fake_bot(tmp_path: Path | None = None) -> Any:
    store = (
        settings_store(tmp_path)
        if tmp_path is not None
        else cast(Any, MagicMock())
    )
    return SimpleNamespace(
        _config=default_config(),
        settings_store=store,
        apply_settings_change=AsyncMock(return_value=[]),
        fetch_channel=AsyncMock(side_effect=AssertionError("unexpected fetch")),
    )


def _commands(tmp_path: Path) -> tuple[SettingsCommands, Any]:
    bot = _fake_bot(tmp_path)
    return SettingsCommands(cast(Any, bot)), bot


async def _run(
    commands: SettingsCommands,
    name: str,
    interaction: Any,
    value: str | None,
    group: str | None = None,
) -> None:
    container: Any = commands if group is None else commands.get_command(group)
    command = cast(Any, container.get_command(name))
    if value is None:
        await command.callback(interaction)
    else:
        await command.callback(interaction, value)


def _walk(group: app_commands.Group) -> list[Any]:
    found: list[Any] = []
    for command in group.commands:
        found.append(command)
        if isinstance(command, app_commands.Group):
            found.extend(_walk(command))
    return found


def _subgroups(group: app_commands.Group) -> list[app_commands.Group]:
    return [
        command
        for command in group.commands
        if isinstance(command, app_commands.Group)
    ]


def _guild(
    *,
    roles: dict[int, str] | None = None,
    channels: dict[int, Any] | None = None,
) -> Any:
    role_map = {
        role_id: SimpleNamespace(id=role_id, name=name, is_default=lambda: False)
        for role_id, name in (roles or {}).items()
    }
    channel_map = channels or {}
    return SimpleNamespace(
        id=5678,
        owner_id=999,
        roles=list(role_map.values()),
        channels=list(channel_map.values()),
        get_role=role_map.get,
        get_channel=channel_map.get,
    )


def _text_channel(channel_id: int) -> Any:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.name = "channel"
    channel.guild = SimpleNamespace(id=5678)
    return channel


def _forum_channel(channel_id: int, tags: dict[int, str] | None = None) -> Any:
    channel = MagicMock(spec=discord.ForumChannel)
    channel.id = channel_id
    channel.name = "forum"
    channel.guild = SimpleNamespace(id=5678)
    channel.available_tags = [
        SimpleNamespace(id=tag_id, name=name)
        for tag_id, name in (tags or {}).items()
    ]
    channel.get_tag = lambda tag_id: next(
        (tag for tag in channel.available_tags if tag.id == tag_id),
        None,
    )
    return channel


class TestCommandText:
    def test_a_renamed_subcommand_names_the_variable_it_replaced(self) -> None:
        # Discord's 32-character name limit forced three renames, and the
        # command list is where an operator would otherwise have to guess.
        commands = SettingsCommands(cast(Any, _fake_bot()))
        renamed = cast(Any, commands.get_command("timezone"))

        assert renamed.description == "Was the TZ environment variable"

    def test_an_unrenamed_subcommand_describes_what_it_does(self) -> None:
        commands = SettingsCommands(cast(Any, _fake_bot()))
        command = cast(Any, commands.get_command("gw2_guild_id"))

        assert command.description.startswith("Guild id listed in")

    def test_the_list_is_split_into_messages_discord_accepts(self) -> None:
        # It grows with every setting added, and one character over the limit
        # fails the whole reply rather than printing a little less.
        lines = [f"line {index} " + "x" * 80 for index in range(60)]

        messages = chunk_lines(lines)

        assert len(messages) > 1
        assert all(len(message) <= 1900 for message in messages)
        assert "\n".join(messages) == "\n".join(lines)

    def test_a_short_list_stays_one_message(self) -> None:
        assert chunk_lines(["one", "two"]) == ["one\ntwo"]

    async def test_the_real_list_fits_in_one_message_today(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await cast(Any, commands.get_command("list")).callback(interaction)
        bot.settings_store.close()

        assert interaction.response.send_message.await_count == 1
