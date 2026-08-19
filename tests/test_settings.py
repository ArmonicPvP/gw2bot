import asyncio
import logging
import re
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import discord
import pytest
from cryptography.fernet import Fernet
from discord import app_commands
from sqlalchemy.exc import SQLAlchemyError

from factories import (
    default_config,
    forbidden_error,
    settings_interaction,
    settings_reply,
    settings_store,
)
from gw2bot.config import (
    BootstrapConfig,
    ConfigurationError,
    bootstrap_from_env,
)
from gw2bot.logging_setup import RedactingFormatter, SecretRegistry
from gw2bot.raffle import RaffleStore
from gw2bot.settings.commands import (
    SettingsCommands,
    _autocomplete_for,
    chunk_lines,
)
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
# Guild Wars 2 guild ids are GUIDs, and /settings now checks the shape,
# so the tests use ids the parser accepts rather than readable labels.
FIRST_GUILD = "116e0c0e-0035-44a9-bb22-4ae3e23127e5"
SECOND_GUILD = "22c7b3a1-9f4e-4d18-8a05-6b1c0f2d7e93"
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

    def test_only_credentials_are_marked_encrypted(self) -> None:
        encrypted = {
            definition.name
            for definition in SETTING_DEFINITIONS
            if definition.encrypted
        }

        assert encrypted == {
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

    def test_tz_is_imported_even_though_it_is_never_warned_about(
        self,
        store: SettingsStore,
    ) -> None:
        # The two behaviours are separate on purpose: an upgrade still picks
        # up the operator's timezone, but the warning must not tell them to
        # delete the variable their container uses for its own clock.
        imported = import_legacy_environment(store, {"TZ": "America/New_York"})

        assert imported == ("TZ",)
        assert store.get_raw(definition_for("timezone")) == "America/New_York"
        assert legacy_variables_present({"TZ": "America/New_York"}) == ()

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
        assert "not a text channel" in settings_reply(interaction)

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
        # No API client and no key by default: the guild id check skips its
        # API arm rather than reaching for a network nothing has configured.
        _api=None,
        _session=object(),
        # An unclaimed ledger, which is the state a fresh install is in and
        # the one the guild id check has to get right.
        raffle_store=SimpleNamespace(bound_guild=MagicMock(return_value=None)),
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


class TestSecretHandlingGuards:
    def test_a_malformed_environment_key_is_reported_without_echoing_it(
        self,
        tmp_path: Path,
    ) -> None:
        with pytest.raises(ConfigurationError) as failure:
            SettingsCipher.for_database(
                str(tmp_path / "gw2bot.db"),
                {ENCRYPTION_KEY_VARIABLE: "not-a-fernet-key"},
            )

        message = str(failure.value)
        assert ENCRYPTION_KEY_VARIABLE in message
        assert "url-safe base64-encoded" in message
        # It says what shape the key must have, never the one that was given.
        assert "not-a-fernet-key" not in message

    async def test_unsetting_a_secret_never_names_a_fallback_value(
        self,
        tmp_path: Path,
    ) -> None:
        # No secret has a default today, so this guards the day one gains
        # one rather than the code as it stands.
        commands, bot = _commands(tmp_path)
        definition = definition_for("gw2_api_key")
        bot.settings_store.set_raw(definition, "gw2-secret")
        bot._config = default_config(gw2_api_key="gw2-secret")

        note = commands._fallback_note(definition)
        bot.settings_store.close()

        assert note == ""


class TestEncryptionKeyFile:
    def test_narrows_a_key_file_that_arrives_world_readable(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # One restored from a backup or dropped in by hand keeps whatever mode
        # it had, and a readable key hands over every stored credential.
        database = tmp_path / "gw2bot.db"
        key_file = key_file_path(str(database))
        key_file.write_bytes(Fernet.generate_key())
        key_file.chmod(0o644)

        with caplog.at_level(logging.WARNING, logger="gw2bot"):
            SettingsCipher.for_database(str(database), {})

        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
        assert "readable beyond its owner" in caplog.text

    def test_leaves_an_already_private_key_file_alone(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        database = tmp_path / "gw2bot.db"
        SettingsCipher.for_database(str(database), {})

        with caplog.at_level(logging.WARNING, logger="gw2bot"):
            SettingsCipher.for_database(str(database), {})

        assert caplog.records == []

    def test_refuses_a_key_file_whose_mode_cannot_be_narrowed(
        self,
        tmp_path: Path,
    ) -> None:
        # Reading it anyway would leave every stored credential available to
        # any other local user, which is the whole reason for the mode.
        database = tmp_path / "gw2bot.db"
        key_file = key_file_path(str(database))
        key_file.write_bytes(Fernet.generate_key())
        key_file.chmod(0o644)

        with patch.object(Path, "chmod", side_effect=PermissionError("read-only")):
            with pytest.raises(ConfigurationError) as failure:
                SettingsCipher.for_database(str(database), {})

        message = str(failure.value)
        assert "readable beyond its owner" in message
        assert ENCRYPTION_KEY_VARIABLE in message

    def test_refuses_a_key_file_whose_mode_cannot_be_read(
        self,
        tmp_path: Path,
    ) -> None:
        # A mode that cannot be inspected cannot be trusted; assuming it is
        # private is how the invariant quietly stops holding.
        database = tmp_path / "gw2bot.db"
        key_file_path(str(database)).write_bytes(Fernet.generate_key())

        with patch.object(Path, "stat", side_effect=PermissionError("denied")):
            with pytest.raises(ConfigurationError) as failure:
                SettingsCipher.for_database(str(database), {})

        assert "could not be read and checked" in str(failure.value)

    def test_a_corrupt_key_file_is_reported_rather_than_crashing(
        self,
        tmp_path: Path,
    ) -> None:
        # A truncated file from a half-finished restore used to raise a
        # ValueError several frames deep that main() did not catch, so the bot
        # would not start and nothing said why.
        database = tmp_path / "gw2bot.db"
        key_file = key_file_path(str(database))
        key_file.write_bytes(b"not-a-key")

        with pytest.raises(ConfigurationError) as failure:
            SettingsCipher.for_database(str(database), {})

        message = str(failure.value)
        assert "not a usable encryption key" in message
        # Non-destructive: the remedy is restore-or-delete, never a silent
        # overwrite of the only thing that can read the stored secrets.
        assert "Restore it from a backup" in message
        assert key_file.read_bytes() == b"not-a-key"

    def test_an_empty_key_file_is_reported_the_same_way(
        self,
        tmp_path: Path,
    ) -> None:
        database = tmp_path / "gw2bot.db"
        key_file_path(str(database)).write_bytes(b"")

        with pytest.raises(ConfigurationError):
            SettingsCipher.for_database(str(database), {})


class TestReviewFindings:
    """Cases for the four problems the PR review turned up."""

    async def test_a_category_channel_is_refused_for_a_text_setting(
        self,
        tmp_path: Path,
    ) -> None:
        # Ruling out forums is not enough: a category passes that test and
        # then has no send(), so delivery would raise AttributeError outside
        # every caller's discord.DiscordException handling.
        commands, bot = _commands(tmp_path)
        category = MagicMock(spec=discord.CategoryChannel)
        category.id = 7
        category.name = "Guild"
        category.guild = SimpleNamespace(id=5678)
        interaction = settings_interaction(
            role_ids=(OFFICER_ROLE_ID,),
            guild=_guild(channels={7: category}),
        )

        await _run(
            commands,
            "discord_notification_channel_id",
            interaction,
            "7",
        )
        stored = bot.settings_store.is_set(
            definition_for("discord_notification_channel_id")
        )
        bot.settings_store.close()

        assert not stored
        assert "not a text channel" in settings_reply(interaction)

    async def test_a_guild_id_the_ledger_would_reject_is_refused(
        self,
        tmp_path: Path,
    ) -> None:
        # Finding out only while applying would leave the new id persisted and
        # the bot unable to start next time, with no command there to say why.
        commands, bot = _commands(tmp_path)
        bot.raffle_store = SimpleNamespace(bound_guild=lambda: FIRST_GUILD)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "gw2_guild_id", interaction, SECOND_GUILD)
        stored = bot.settings_store.is_set(definition_for("gw2_guild_id"))
        bot.settings_store.close()

        assert not stored
        assert "already belongs to a different guild" in settings_reply(interaction)
        bot.apply_settings_change.assert_not_awaited()

    async def test_the_same_guild_id_the_ledger_holds_is_accepted(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        bot.raffle_store = SimpleNamespace(bound_guild=lambda: FIRST_GUILD)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "gw2_guild_id", interaction, FIRST_GUILD)
        stored = bot.settings_store.get_raw(definition_for("gw2_guild_id"))
        bot.settings_store.close()

        assert stored == FIRST_GUILD

    async def test_a_value_that_cannot_be_applied_is_rolled_back(
        self,
        tmp_path: Path,
    ) -> None:
        # Leaving it stored would apply it on the next restart, where nothing
        # can report the problem.
        commands, bot = _commands(tmp_path)
        definition = definition_for("timezone")
        bot.settings_store.set_raw(definition, "America/New_York")
        bot.apply_settings_change = AsyncMock(
            side_effect=[RuntimeError("poller would not restart"), []]
        )
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "timezone", interaction, "Europe/Berlin")
        stored = bot.settings_store.get_raw(definition)
        bot.settings_store.close()

        assert stored == "America/New_York"
        assert "previous value is back in place" in settings_reply(interaction)

    async def test_a_rollback_restores_being_unset(self, tmp_path: Path) -> None:
        # None is a real previous value - it means nobody had set it - so the
        # restore has to clear the row rather than skip.
        commands, bot = _commands(tmp_path)
        definition = definition_for("timezone")
        bot.apply_settings_change = AsyncMock(
            side_effect=[RuntimeError("nope"), []]
        )
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "timezone", interaction, "Europe/Berlin")
        still_set = bot.settings_store.is_set(definition)
        bot.settings_store.close()

        assert not still_set

    async def test_a_successful_change_is_not_rolled_back(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "timezone", interaction, "Europe/Berlin")
        stored = bot.settings_store.get_raw(definition_for("timezone"))
        bot.settings_store.close()

        assert stored == "Europe/Berlin"
        assert bot.apply_settings_change.await_count == 1


class TestGuildIdImportGuard:
    """The one-time import must not store a guild id that stops startup.

    RaffleStore refuses to open against a ledger bound to another guild, and
    that happens before any command exists to report it. With the import
    marker already written, a corrected variable would never be read again, so
    storing the conflicting value would leave a bot that cannot start and no
    way to fix it from Discord.
    """

    def _bound_store(self, tmp_path: Path, guild_id: str) -> SettingsStore:
        # Claim the ledger the way RaffleStore does on first use.
        RaffleStore(str(tmp_path / "gw2bot.db"), guild_id).close()
        return settings_store(tmp_path)

    def test_refuses_a_guild_id_the_ledger_contradicts(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        store = self._bound_store(tmp_path, FIRST_GUILD)

        with caplog.at_level(logging.WARNING, logger="gw2bot"):
            imported = import_legacy_environment(
                store,
                {"GW2_GUILD_ID": SECOND_GUILD, "GW2_API_KEY": "gw2-secret"},
            )
        guild_id = store.get_raw(definition_for("gw2_guild_id"))
        api_key = store.get_raw(definition_for("gw2_api_key"))
        store.close()

        assert guild_id is None
        assert imported == ("GW2_API_KEY",)
        # One bad variable must not cost the others their import.
        assert api_key == "gw2-secret"
        assert "already belongs to a different guild" in caplog.text

    def test_imports_a_guild_id_the_ledger_agrees_with(
        self,
        tmp_path: Path,
    ) -> None:
        store = self._bound_store(tmp_path, FIRST_GUILD)

        imported = import_legacy_environment(store, {"GW2_GUILD_ID": FIRST_GUILD})
        guild_id = store.get_raw(definition_for("gw2_guild_id"))
        store.close()

        assert imported == ("GW2_GUILD_ID",)
        assert guild_id == FIRST_GUILD

    def test_imports_a_guild_id_when_the_ledger_is_unclaimed(
        self,
        store: SettingsStore,
    ) -> None:
        assert import_legacy_environment(store, {"GW2_GUILD_ID": FIRST_GUILD}) == (
            "GW2_GUILD_ID",
        )
        assert store.get_raw(definition_for("gw2_guild_id")) == FIRST_GUILD


class TestDatabaseFailuresDuringAChange:
    """A database that cannot be read must still reach the caller privately."""

    def _broken(self, tmp_path: Path) -> Any:
        bot = _fake_bot(tmp_path)
        store = bot.settings_store
        store.get_raw = MagicMock(side_effect=SQLAlchemyError("read failed"))
        return bot

    async def test_a_failed_snapshot_read_is_reported_when_storing(
        self,
        tmp_path: Path,
    ) -> None:
        bot = self._broken(tmp_path)
        commands = SettingsCommands(cast(Any, bot))
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "timezone", interaction, "Europe/Berlin")
        bot.settings_store.close()

        assert "could not be written" in settings_reply(interaction)
        bot.apply_settings_change.assert_not_awaited()

    async def test_a_failed_snapshot_read_is_reported_when_unsetting(
        self,
        tmp_path: Path,
    ) -> None:
        bot = self._broken(tmp_path)
        commands = SettingsCommands(cast(Any, bot))
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "timezone", interaction, " ")
        bot.settings_store.close()

        assert "could not be written" in settings_reply(interaction)
        bot.apply_settings_change.assert_not_awaited()


class TestInteractionDeferral:
    """A change can outrun Discord's three-second response window.

    Validating a channel reaches Discord, and applying one rebuilds the API
    client and can restart the calendar. Past the window the token expires and
    the confirmation is lost with the value already stored, which is the worst
    of both: changed, and reported as failed.
    """

    async def test_storing_defers_before_the_slow_work(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "timezone", interaction, "Europe/Berlin")
        bot.settings_store.close()

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        # The confirmation therefore arrives as a followup.
        interaction.followup.send.assert_awaited_once()
        assert "Europe/Berlin" in settings_reply(interaction)

    async def test_unsetting_defers_too(self, tmp_path: Path) -> None:
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "timezone", interaction, " ")
        bot.settings_store.close()

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)

    async def test_reading_answers_directly(self, tmp_path: Path) -> None:
        # A read is a SQLite lookup; deferring would only cost the caller the
        # immediate answer.
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "timezone", interaction, None)
        bot.settings_store.close()

        interaction.response.defer.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()

    async def test_a_refused_defer_does_not_stop_the_change(
        self,
        tmp_path: Path,
    ) -> None:
        # The value is still worth storing, and the notice reports what
        # happened; losing the reply must not lose the change.
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))
        interaction.response.defer = AsyncMock(side_effect=forbidden_error(50013))

        await _run(commands, "timezone", interaction, "Europe/Berlin")
        stored = bot.settings_store.get_raw(definition_for("timezone"))
        bot.settings_store.close()

        assert stored == "Europe/Berlin"

    async def test_an_unauthorized_caller_is_refused_without_deferring(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction()

        await _run(commands, "timezone", interaction, "Europe/Berlin")
        bot.settings_store.close()

        interaction.response.defer.assert_not_awaited()
        assert "do not have the required role" in settings_reply(interaction)


class TestLedgerReadFailure:
    async def test_a_failed_ledger_read_is_reported(self, tmp_path: Path) -> None:
        # The binding check runs before the guarded write, so it needs its own
        # guard or the command ends with an unhandled error.
        commands, bot = _commands(tmp_path)
        bot.raffle_store = SimpleNamespace(
            bound_guild=MagicMock(side_effect=SQLAlchemyError("read failed"))
        )
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "gw2_guild_id", interaction, FIRST_GUILD)
        stored = bot.settings_store.is_set(definition_for("gw2_guild_id"))
        bot.settings_store.close()

        assert not stored
        assert "could not be read" in settings_reply(interaction)
        bot.apply_settings_change.assert_not_awaited()


class TestSettingsAutocomplete:
    """The wrapper promises never to raise; these are the ways it could.

    An autocomplete that raises shows the officer a Discord error instead of
    suggestions, on a command whose whole point is that nobody has to paste a
    snowflake by hand.
    """

    @pytest.mark.parametrize(
        "error",
        [
            aiohttp.ClientError("connection reset"),
            asyncio.TimeoutError(),
            discord.DiscordException("forbidden"),
        ],
        ids=["transport", "timeout", "discord"],
    )
    async def test_a_failure_reaching_discord_yields_no_choices(
        self,
        tmp_path: Path,
        error: Exception,
    ) -> None:
        commands, bot = _commands(tmp_path)
        # The forum-tag branch is the only one that leaves the process, and
        # fetch_channel raises transport errors that are not DiscordException.
        bot.fetch_channel = AsyncMock(side_effect=error)
        autocomplete = _autocomplete_for(
            commands,
            definition_for("trial_accepted_tag", CHANNELS_GROUP),
        )
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        choices = await autocomplete(cast(discord.Interaction, interaction), "")
        bot.settings_store.close()

        assert choices == []

    async def test_an_unauthorized_caller_is_offered_nothing(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        autocomplete = _autocomplete_for(commands, definition_for("timezone"))
        interaction = settings_interaction()

        choices = await autocomplete(cast(discord.Interaction, interaction), "")
        bot.settings_store.close()

        assert choices == []

    async def test_roles_are_offered_by_name(self, tmp_path: Path) -> None:
        commands, bot = _commands(tmp_path)
        guild = SimpleNamespace(
            id=5678,
            owner_id=999,
            roles=(
                SimpleNamespace(id=11, name="Officers", is_default=lambda: False),
                SimpleNamespace(id=12, name="Members", is_default=lambda: False),
                SimpleNamespace(id=13, name="@everyone", is_default=lambda: True),
            ),
            channels=(),
            get_role=lambda role_id: None,
            get_channel=lambda channel_id: None,
        )
        autocomplete = _autocomplete_for(
            commands,
            definition_for("raffle_officer", ROLES_GROUP),
        )
        interaction = settings_interaction(
            role_ids=(OFFICER_ROLE_ID,),
            guild=guild,
        )

        choices = await autocomplete(
            cast(discord.Interaction, interaction),
            "offic",
        )
        bot.settings_store.close()

        # @everyone is filtered out: it is not a role anybody means to name,
        # and offering it would suggest a gate that lets the server through.
        assert [(choice.name, choice.value) for choice in choices] == [
            ("Officers", "11")
        ]


class TestUnreadableStoredValues:
    """A row the parser rejects must not read as a value somebody chose.

    compose_config drops it and falls back to the default, logging to the
    console. Without a signal on screen, /settings reports the default as set
    and the officer has no way to tell why their change did nothing.
    """

    async def test_the_single_setting_reply_says_it_could_not_be_read(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        # Written straight to the store: the command would have refused it,
        # but a hand-edited database or a parser tightened in a later release
        # both land here.
        bot.settings_store.set_raw(definition_for("timezone"), "Not/A/Zone")
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "timezone", interaction, None)
        bot.settings_store.close()

        reply = settings_reply(interaction)
        assert "could not be read" in reply
        assert "set it again" in reply
        # The running value is still shown, because that is what the bot is
        # actually using.
        assert "`UTC`" in reply

    async def test_the_list_marks_it_unreadable(self, tmp_path: Path) -> None:
        commands, bot = _commands(tmp_path)
        bot.settings_store.set_raw(definition_for("timezone"), "Not/A/Zone")
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "list", interaction, None)
        bot.settings_store.close()

        reply = settings_reply(interaction)
        assert "(unreadable)" in reply
        assert "`timezone` — `UTC` (default" in reply

    async def test_a_readable_row_still_reads_as_set(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))
        await _run(commands, "timezone", interaction, "Europe/Berlin")
        bot._config = default_config(event_timezone="Europe/Berlin")

        listing = settings_interaction(role_ids=(OFFICER_ROLE_ID,))
        await _run(commands, "list", listing, None)
        bot.settings_store.close()

        reply = settings_reply(listing)
        assert "`timezone` — `Europe/Berlin` (set)" in reply
        assert "unreadable" not in reply


class TestGuildIdVerification:
    """A wrong guild id has to be caught before it claims the ledger.

    RaffleStore records the first guild id it is given and refuses a different
    one from then on, with no unbind. So the checks here are not politeness -
    they are the only chance to stop a typo costing the guild its ledger.
    """

    async def test_a_value_that_is_not_a_guid_is_refused(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "gw2_guild_id", interaction, "my-guild")
        stored = bot.settings_store.is_set(definition_for("gw2_guild_id"))
        bot.settings_store.close()

        assert not stored
        assert "116E0C0E" in settings_reply(interaction)
        bot.apply_settings_change.assert_not_awaited()

    async def test_a_guild_the_key_does_not_lead_is_refused(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        bot._api = SimpleNamespace(
            get_account=AsyncMock(return_value={"guild_leader": [SECOND_GUILD]})
        )
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "gw2_guild_id", interaction, FIRST_GUILD)
        stored = bot.settings_store.is_set(definition_for("gw2_guild_id"))
        bot.settings_store.close()

        assert not stored
        assert "does not lead a guild with that id" in settings_reply(interaction)
        bot.apply_settings_change.assert_not_awaited()

    async def test_a_guild_the_key_leads_is_accepted(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        bot._api = SimpleNamespace(
            get_account=AsyncMock(
                return_value={"guild_leader": [SECOND_GUILD, FIRST_GUILD]}
            )
        )
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "gw2_guild_id", interaction, FIRST_GUILD)
        stored = bot.settings_store.get_raw(definition_for("gw2_guild_id"))
        bot.settings_store.close()

        assert stored == FIRST_GUILD
        bot.apply_settings_change.assert_awaited_once()

    async def test_without_an_api_key_the_shape_alone_decides(
        self,
        tmp_path: Path,
    ) -> None:
        # Setting the guild id before the key is an ordinary order to do it
        # in, and refusing until a key exists would make the pair impossible
        # to configure from a clean install.
        commands, bot = _commands(tmp_path)
        assert bot._api is None
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "gw2_guild_id", interaction, FIRST_GUILD)
        stored = bot.settings_store.get_raw(definition_for("gw2_guild_id"))
        bot.settings_store.close()

        assert stored == FIRST_GUILD

    @pytest.mark.parametrize(
        "error",
        [aiohttp.ClientError("connection reset"), asyncio.TimeoutError()],
        ids=["transport", "timeout"],
    )
    async def test_an_api_outage_does_not_block_the_change(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        error: Exception,
    ) -> None:
        # The shape check has already run, and refusing here would mean an
        # API outage stopped an officer from correcting a setting.
        commands, bot = _commands(tmp_path)
        bot._api = SimpleNamespace(get_account=AsyncMock(side_effect=error))
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        with caplog.at_level(logging.WARNING, logger="gw2bot"):
            await _run(commands, "gw2_guild_id", interaction, FIRST_GUILD)
        stored = bot.settings_store.get_raw(definition_for("gw2_guild_id"))
        bot.settings_store.close()

        assert stored == FIRST_GUILD
        assert "storing it unchecked" in caplog.text

    async def test_the_ledger_binding_is_checked_before_the_api(
        self,
        tmp_path: Path,
    ) -> None:
        # A ledger that already belongs elsewhere is refused whatever the API
        # says, and the API is not asked - the answer cannot change it.
        commands, bot = _commands(tmp_path)
        bot.raffle_store = SimpleNamespace(
            bound_guild=MagicMock(return_value=SECOND_GUILD)
        )
        bot._api = SimpleNamespace(
            get_account=AsyncMock(return_value={"guild_leader": [FIRST_GUILD]})
        )
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "gw2_guild_id", interaction, FIRST_GUILD)
        stored = bot.settings_store.is_set(definition_for("gw2_guild_id"))
        bot.settings_store.close()

        assert not stored
        assert "already belongs to a different guild" in settings_reply(interaction)
        bot._api.get_account.assert_not_awaited()


class TestGuildIdVerificationOnACleanInstall:
    """The first guild id is the one that matters, and it had no client.

    Gw2Bot builds _api only once the key and the guild id are both set, so
    after setting just the key there is none - and that is precisely when the
    next command claims the ledger for good.
    """

    async def test_a_wrong_id_is_refused_with_only_a_key_configured(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        # The state a clean install is in between the two commands.
        bot._api = None
        bot._config = default_config(gw2_api_key="gw2-key")
        account = AsyncMock(return_value={"guild_leader": [SECOND_GUILD]})
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        with patch(
            "gw2bot.settings.commands.Gw2ApiClient",
            return_value=SimpleNamespace(get_account=account),
        ):
            await _run(commands, "gw2_guild_id", interaction, FIRST_GUILD)
        stored = bot.settings_store.is_set(definition_for("gw2_guild_id"))
        bot.settings_store.close()

        assert not stored
        assert "does not lead a guild with that id" in settings_reply(interaction)
        bot.apply_settings_change.assert_not_awaited()

    async def test_a_correct_id_is_accepted_with_only_a_key_configured(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        bot._api = None
        bot._config = default_config(gw2_api_key="gw2-key")
        account = AsyncMock(return_value={"guild_leader": [FIRST_GUILD]})
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        with patch(
            "gw2bot.settings.commands.Gw2ApiClient",
            return_value=SimpleNamespace(get_account=account),
        ):
            await _run(commands, "gw2_guild_id", interaction, FIRST_GUILD)
        stored = bot.settings_store.get_raw(definition_for("gw2_guild_id"))
        bot.settings_store.close()

        assert stored == FIRST_GUILD
        assert "could not be checked" not in settings_reply(interaction)

    async def test_with_no_key_at_all_the_reply_says_it_was_not_checked(
        self,
        tmp_path: Path,
    ) -> None:
        # Nothing can be asked, so the id binds on shape alone. That cannot be
        # helped; it can stop being silent.
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "gw2_guild_id", interaction, FIRST_GUILD)
        stored = bot.settings_store.get_raw(definition_for("gw2_guild_id"))
        bot.settings_store.close()

        reply = settings_reply(interaction)
        assert stored == FIRST_GUILD
        assert "could not be checked" in reply
        assert "gw2_api_key" in reply
        assert "cannot be pointed at another one" in reply


class TestGuildIdSpelling:
    """One guild written several ways has to stay one guild."""

    @pytest.mark.parametrize(
        "typed",
        [
            "116E0C0E-0035-44A9-BB22-4AE3E23127E5",
            "{116e0c0e-0035-44a9-bb22-4ae3e23127e5}",
            "116e0c0e003544a9bb224ae3e23127e5",
            "  116e0c0e-0035-44a9-bb22-4ae3e23127e5  ",
        ],
        ids=["upper", "braces", "unhyphenated", "padded"],
    )
    async def test_every_accepted_form_stores_the_same_text(
        self,
        tmp_path: Path,
        typed: str,
    ) -> None:
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "gw2_guild_id", interaction, typed)
        stored = bot.settings_store.get_raw(definition_for("gw2_guild_id"))
        bot.settings_store.close()

        assert stored == FIRST_GUILD

    async def test_an_upper_case_api_answer_matches_a_lower_case_id(
        self,
        tmp_path: Path,
    ) -> None:
        # /v2/account.guild_leader answers in upper case, so comparing the raw
        # text would refuse the operator's own correct guild.
        commands, bot = _commands(tmp_path)
        bot._api = SimpleNamespace(
            get_account=AsyncMock(
                return_value={"guild_leader": [FIRST_GUILD.upper()]}
            )
        )
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "gw2_guild_id", interaction, FIRST_GUILD)
        stored = bot.settings_store.get_raw(definition_for("gw2_guild_id"))
        bot.settings_store.close()

        assert stored == FIRST_GUILD
        bot.apply_settings_change.assert_awaited_once()

    async def test_a_ledger_bound_in_upper_case_is_the_same_guild(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        bot.raffle_store = SimpleNamespace(
            bound_guild=MagicMock(return_value=FIRST_GUILD.upper())
        )
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "gw2_guild_id", interaction, FIRST_GUILD)
        stored = bot.settings_store.get_raw(definition_for("gw2_guild_id"))
        bot.settings_store.close()

        assert stored == FIRST_GUILD
        assert "different guild" not in settings_reply(interaction)


class TestValidationTransportFailures:
    """A validator that raises leaves the officer with nothing.

    _handle defers before validating, so an escaping exception surfaces as a
    Discord error on an interaction that is already waiting, with no notice
    and nothing in the log to trace it to.
    """

    @pytest.mark.parametrize(
        "error",
        [aiohttp.ClientError("connection reset"), asyncio.TimeoutError()],
        ids=["transport", "timeout"],
    )
    async def test_a_channel_that_cannot_be_fetched_is_reported(
        self,
        tmp_path: Path,
        error: Exception,
    ) -> None:
        commands, bot = _commands(tmp_path)
        bot.fetch_channel = AsyncMock(side_effect=error)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(
            commands,
            "raffle_contribution",
            interaction,
            "424242",
            CHANNELS_GROUP,
        )
        stored = bot.settings_store.is_set(
            definition_for("raffle_contribution", CHANNELS_GROUP)
        )
        bot.settings_store.close()

        assert not stored
        assert "No channel with the id" in settings_reply(interaction)

    @pytest.mark.parametrize(
        "error",
        [aiohttp.ClientError("connection reset"), asyncio.TimeoutError()],
        ids=["transport", "timeout"],
    )
    async def test_a_forum_that_cannot_be_fetched_is_reported(
        self,
        tmp_path: Path,
        error: Exception,
    ) -> None:
        commands, bot = _commands(tmp_path)
        bot.fetch_channel = AsyncMock(side_effect=error)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(
            commands,
            "trial_accepted_tag",
            interaction,
            "424242",
            CHANNELS_GROUP,
        )
        stored = bot.settings_store.is_set(
            definition_for("trial_accepted_tag", CHANNELS_GROUP)
        )
        bot.settings_store.close()

        assert not stored
        assert "could not be read" in settings_reply(interaction)


class TestRaffleExclusionSetting:
    """The first setting whose value is a list rather than a scalar."""

    async def test_a_comma_separated_list_round_trips(
        self,
        tmp_path: Path,
    ) -> None:
        # The stored text has to be something the parser accepts again, or
        # the value would read as unreadable the moment anybody looked.
        commands, bot = _commands(tmp_path)
        definition = definition_for("raffle_excluded_accounts")
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(
            commands,
            "raffle_excluded_accounts",
            interaction,
            "Banned.1234, Another.5678",
        )
        stored = bot.settings_store.get_raw(definition)
        bot.settings_store.close()

        assert stored == "Banned.1234, Another.5678"
        assert definition.parse(stored) == ("Banned.1234", "Another.5678")

    async def test_the_reply_shows_the_names_not_a_python_tuple(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(
            commands,
            "raffle_excluded_accounts",
            interaction,
            "Banned.1234, Another.5678",
        )
        bot.settings_store.close()

        reply = settings_reply(interaction)
        assert "`Banned.1234, Another.5678`" in reply
        assert "(" not in reply.split("is now")[1]

    def test_duplicates_are_dropped_case_insensitively(self) -> None:
        definition = definition_for("raffle_excluded_accounts")

        parsed = definition.parse("Banned.1234, banned.1234, Other.5678")

        # One account listed twice would read as two exclusions.
        assert parsed == ("Banned.1234", "Other.5678")

    def test_surrounding_whitespace_and_empty_entries_are_ignored(self) -> None:
        definition = definition_for("raffle_excluded_accounts")

        assert definition.parse(" A.1 ,, B.2 , ") == ("A.1", "B.2")

    def test_a_list_of_only_separators_is_refused(self) -> None:
        definition = definition_for("raffle_excluded_accounts")

        with pytest.raises(ConfigurationError, match="one or more account"):
            definition.parse(",,,")

    async def test_an_unset_list_reads_as_not_set(self, tmp_path: Path) -> None:
        # The default is an empty tuple, and rendering that as `()` would
        # claim a value nobody set.
        commands, bot = _commands(tmp_path)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "raffle_excluded_accounts", interaction, None)
        bot.settings_store.close()

        assert UNSET_DISPLAY in settings_reply(interaction)

    async def test_clearing_it_empties_the_list(self, tmp_path: Path) -> None:
        commands, bot = _commands(tmp_path)
        definition = definition_for("raffle_excluded_accounts")
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))
        await _run(
            commands,
            "raffle_excluded_accounts",
            interaction,
            "Banned.1234",
        )

        clearing = settings_interaction(role_ids=(OFFICER_ROLE_ID,))
        await _run(commands, "raffle_excluded_accounts", clearing, " ")
        still_set = bot.settings_store.is_set(definition)
        bot.settings_store.close()

        assert not still_set

    def test_it_composes_onto_the_config_as_a_tuple(
        self,
        store: SettingsStore,
    ) -> None:
        definition = definition_for("raffle_excluded_accounts")
        store.set_raw(definition, "Banned.1234, Another.5678")

        config = compose_config(
            bootstrap_from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "DISCORD_COMMAND_GUILD_ID": "5678",
                }
            ),
            store.raw_values(),
        )

        assert config.raffle_excluded_accounts == (
            "Banned.1234",
            "Another.5678",
        )


class TestUndecryptableSecrets:
    """A credential the key cannot read is not a credential in force.

    SettingsStore.is_set deliberately does not decrypt, so /settings can say
    a value is there and unreadable. It has to actually say it: the feature
    that needs the credential is already switched off, and the usual "cannot
    be viewed once set" would read as working.
    """

    def _unreadable(self, tmp_path: Path) -> tuple[Any, Any]:
        commands, bot = _commands(tmp_path)
        definition = definition_for("gw2_api_key")
        bot.settings_store.set_raw(definition, "gw2-secret")
        # The key changes, exactly as it would if settings.key were lost and
        # regenerated. The row survives; nothing can read it.
        bot.settings_store._cipher = SettingsCipher(Fernet.generate_key())
        return commands, bot

    async def test_the_reply_says_it_could_not_be_decrypted(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = self._unreadable(tmp_path)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "gw2_api_key", interaction, None)
        bot.settings_store.close()

        reply = settings_reply(interaction)
        assert "could not be decrypted" in reply
        assert "set it again" in reply
        # Still no value, and not the placeholder that reads as healthy.
        assert "gw2-secret" not in reply
        assert SECRET_PLACEHOLDER not in reply

    async def test_the_list_marks_it_unreadable(self, tmp_path: Path) -> None:
        commands, bot = self._unreadable(tmp_path)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "list", interaction, None)
        bot.settings_store.close()

        reply = settings_reply(interaction)
        assert "(unreadable)" in reply
        assert "gw2-secret" not in reply

    async def test_a_readable_secret_still_shows_the_placeholder(
        self,
        tmp_path: Path,
    ) -> None:
        commands, bot = _commands(tmp_path)
        bot.settings_store.set_raw(definition_for("gw2_api_key"), "gw2-secret")
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "gw2_api_key", interaction, None)
        bot.settings_store.close()

        reply = settings_reply(interaction)
        assert SECRET_PLACEHOLDER in reply
        assert "could not be decrypted" not in reply
        assert "gw2-secret" not in reply


class TestExclusionListLength:
    """A list too long to show back would be stored and then unusable."""

    def test_a_list_that_would_not_fit_a_message_is_refused(self) -> None:
        definition = definition_for("raffle_excluded_accounts")
        oversized = ", ".join(f"Account{index}.1234" for index in range(200))

        with pytest.raises(ConfigurationError, match="characters and the limit"):
            definition.parse(oversized)

    def test_a_list_within_the_limit_is_accepted(self) -> None:
        definition = definition_for("raffle_excluded_accounts")
        accounts = [f"Account{index}.1234" for index in range(50)]

        parsed = definition.parse(", ".join(accounts))

        assert parsed == tuple(accounts)

    async def test_an_oversized_list_is_not_stored(self, tmp_path: Path) -> None:
        commands, bot = _commands(tmp_path)
        oversized = ", ".join(f"Account{index}.1234" for index in range(200))
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "raffle_excluded_accounts", interaction, oversized)
        stored = bot.settings_store.is_set(
            definition_for("raffle_excluded_accounts")
        )
        bot.settings_store.close()

        assert not stored
        assert "Remove some accounts" in settings_reply(interaction)

    async def test_the_longest_accepted_list_still_fits_one_message(
        self,
        tmp_path: Path,
    ) -> None:
        # The point of the cap: whatever it lets through has to render inside
        # Discord's limit, both in the confirmation and in /settings list.
        commands, bot = _commands(tmp_path)
        accounts = [f"Account{index}.1234" for index in range(93)]
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(
            commands,
            "raffle_excluded_accounts",
            interaction,
            ", ".join(accounts),
        )
        bot._config = default_config(raffle_excluded_accounts=tuple(accounts))
        listing = settings_interaction(role_ids=(OFFICER_ROLE_ID,))
        await _run(commands, "list", listing, None)
        bot.settings_store.close()

        assert len(settings_reply(interaction)) < 2000
        for message in listing.followup.send.await_args_list:
            assert len(message.args[0]) < 2000


def _response_error(status: int) -> aiohttp.ClientResponseError:
    return aiohttp.ClientResponseError(
        cast(Any, None),
        (),
        status=status,
        message=f"HTTP {status}",
    )


class TestGuildIdApiFailures:
    """ClientResponseError is a ClientError, and the two mean opposite things.

    A rejected key is the operator's own misconfiguration and must stop the
    write, because the ledger binding cannot be undone from Discord. A service
    that is down is an outage and must not block a correction - but the reply
    has to say the check never happened either way.
    """

    def _bot_with(self, tmp_path: Path, error: Exception) -> tuple[Any, Any]:
        commands, bot = _commands(tmp_path)
        bot._api = SimpleNamespace(get_account=AsyncMock(side_effect=error))
        return commands, bot

    @pytest.mark.parametrize("status", [400, 401, 403, 404], ids=str)
    async def test_a_rejected_key_refuses_the_change(
        self,
        tmp_path: Path,
        status: int,
    ) -> None:
        commands, bot = self._bot_with(tmp_path, _response_error(status))
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "gw2_guild_id", interaction, FIRST_GUILD)
        stored = bot.settings_store.is_set(definition_for("gw2_guild_id"))
        bot.settings_store.close()

        # Nothing stored means the ledger is not claimed on a check that
        # never ran - the whole point, since it cannot be unclaimed.
        assert not stored
        reply = settings_reply(interaction)
        assert "rejected the configured key" in reply
        assert f"HTTP {status}" in reply
        bot.apply_settings_change.assert_not_awaited()

    @pytest.mark.parametrize("status", [500, 502, 503, 429], ids=str)
    async def test_a_service_failure_stores_it_with_the_caveat(
        self,
        tmp_path: Path,
        status: int,
    ) -> None:
        commands, bot = self._bot_with(tmp_path, _response_error(status))
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "gw2_guild_id", interaction, FIRST_GUILD)
        stored = bot.settings_store.get_raw(definition_for("gw2_guild_id"))
        bot.settings_store.close()

        # An outage, including rate limiting, must not stop a correction.
        assert stored == FIRST_GUILD
        reply = settings_reply(interaction)
        assert "could not be checked" in reply
        assert f"HTTP {status}" in reply

    @pytest.mark.parametrize(
        "error",
        [aiohttp.ClientError("connection reset"), asyncio.TimeoutError()],
        ids=["transport", "timeout"],
    )
    async def test_an_unreachable_api_stores_it_with_the_caveat(
        self,
        tmp_path: Path,
        error: Exception,
    ) -> None:
        commands, bot = self._bot_with(tmp_path, error)
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "gw2_guild_id", interaction, FIRST_GUILD)
        stored = bot.settings_store.get_raw(definition_for("gw2_guild_id"))
        bot.settings_store.close()

        assert stored == FIRST_GUILD
        reply = settings_reply(interaction)
        assert "could not be reached" in reply
        assert "cannot be pointed at another one" in reply

    async def test_a_verified_id_carries_no_caveat(self, tmp_path: Path) -> None:
        commands, bot = _commands(tmp_path)
        bot._api = SimpleNamespace(
            get_account=AsyncMock(return_value={"guild_leader": [FIRST_GUILD]})
        )
        interaction = settings_interaction(role_ids=(OFFICER_ROLE_ID,))

        await _run(commands, "gw2_guild_id", interaction, FIRST_GUILD)
        bot.settings_store.close()

        reply = settings_reply(interaction)
        assert "could not be checked" not in reply
        assert "cannot be pointed at another one" not in reply
