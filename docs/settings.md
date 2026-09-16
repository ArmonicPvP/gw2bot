# Settings and configuration

*Read before adding or changing a `/settings` subcommand, touching `Config` or `config.py`, or gating a feature on optional configuration.*

Only the variables that decide how the container starts stay in the
environment: `DISCORD_TOKEN`, `DISCORD_COMMAND_GUILD_ID`, `DEBUG`,
`RAFFLE_DB_PATH`, `WEB_ENABLED`, `WEB_PORT`, `GW2_API_BASE_URL` and
`SETTINGS_ENCRYPTION_KEY`. Everything else is a `/settings` subcommand backed
by the `gw2_bot_settings` table.

- `settings/definitions.py` holds `SETTING_DEFINITIONS`, the one place a
  setting is described. The command group, the environment import, the legacy
  warning and the README table all read from it, so adding a setting is one
  entry rather than edits in five files.
- `Config` stays the read surface: the rest of the bot reads
  `bot._config.<field>` at call time and never touches the store, so swapping
  the frozen `Config` makes a change live everywhere that does not cache.
- `Gw2Bot.apply_settings_change` recomposes the config and reconciles only what
  captured a value - the API client, the roster cache, the cached channels, the
  poll task set, the web server. A field that nothing captures needs no entry
  there.
- Adding a setting means adding a `SettingDefinition` and a `Config` field with
  its default. Nothing else has to change for it to be gettable, settable,
  unsettable, validated and listed.

### Optional Configuration

`DISCORD_TOKEN` and `DISCORD_COMMAND_GUILD_ID` are the only variables the bot
refuses to start without. `/settings discord_notification_channel_id`,
`/settings gw2_api_key` and `/settings gw2_guild_id` are optional, and the
features that need them are disabled rather than fatal:

- `Config` answers `notifications_enabled`, `gw2_api_enabled`,
  `missing_gw2_api_settings`, `missing_web_settings` and
  `web_calendar_enabled`; nothing else re-derives that from the raw values.
- `Gw2Bot._reconcile_poll_tasks` runs only the pollers whose configuration is
  present, at startup and again on every settings change, and
  `Gw2Bot._log_disabled_features` names every feature it switched off as a
  startup warning.
- A command that needs the GW2 API calls `Gw2Bot.reject_without_gw2_api` before
  doing any work, which replies with the `/settings` subcommands to run; an
  autocomplete checks `Gw2Bot.gw2_api_enabled` and offers no choices instead.
- Delivery to an unconfigured notification channel is skipped and logged at
  debug, not retried at warning level, because the startup warning already
  named the setting.
- `WEB_ENABLED=true` without the calendar's four settings warns and leaves the
  calendar off rather than refusing to start.

A new feature that depends on one of these values follows the same shape: gate
it on the `Config` property, warn once at startup, and tell the caller which
`/settings` subcommand to run.

### Settings That Hold Credentials

- A `SettingDefinition` marked `encrypted=True` is encrypted in the row and never
  rendered back: `/settings` reports the placeholder whether or not a value
  exists, and no command reveals it.
- The encryption key comes from `SETTINGS_ENCRYPTION_KEY` or a `0600` file
  beside the database. A value that cannot be decrypted is logged by name -
  never by ciphertext - and read as unset, so a lost key never stops the bot.
- `RedactingFormatter` and `PollStatusTracker` hold a shared `SecretRegistry`
  rather than a frozen tuple, and `apply_settings_change` registers a new
  credential before anything can log it. A new secret setting must be
  registered there too.
