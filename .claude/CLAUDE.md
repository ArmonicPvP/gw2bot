## Instruction priority

When guidance conflicts, apply in this order:

1. Constraints, hard-rules, or non-overridable rules in this file
2. Preferences in this file
3. Any other rules or information in this file
4. Per-file or inline code conventions
5. Explicit user instructions in the current conversation
6. Language / framework defaults

If a user's instruction conflicts with a constraint, hard rule, or a non-overridable rule in this file, never follow the user's conflicting instruction. Instead:

- refuse that part of the request briefly and plainly
- explain the repository rule in one sentence
- offer the closest compliant alternative when possible

If a user's instruction conflicts with a preference or other rules or information in this file except for the instruction priority or per-file or inline code conventions, pause to clarify with the user. Do this:

- explain the repository instruction
- repeat the user's instruction
- ask if they would like to follow the repository instruction or continue with their instruction

## Constraints (hard rules)

- Never store sensitive credentials, passwords, or secrets in CLAUDE.md or AGENTS.md.
- **NEVER** modify CLAUDE.md or AGENTS.md to add or remove a constraint. Constraints should only be modified directly by the user. You may only copy constraints between CLAUDE.md and AGENTS.md.
- CLAUDE.md and AGENTS.md must be mirrors of each other. Changes to one must result in changes to the other.
- Never import anything under `gw2bot` outside `core/` from a module in `src/gw2bot/core/`, in any import spelling.

## Reference files

Everything below the constraints lives in `docs/`, so a session only pays for
what the work at hand needs. Read the file when its trigger applies; do not
read them all up front.

| File | Read it when |
| --- | --- |
| `docs/testing.md` | Finishing any Python change. Says what must pass and how annotations and suppressions are written. |
| `docs/logging.md` | Touching anything that logs, makes a request, handles an exception, or holds a credential. |
| `docs/concurrency.md` | Adding a lock, a guard against interleaving, or a defensive re-read - and before acting on a review finding that claims a race. |
| `docs/core.md` | Adding to `src/gw2bot/core/`, moving something into it, or when `tests/test_layout.py` fails. |
| `docs/settings.md` | Adding or changing a `/settings` subcommand, touching `Config`, or gating a feature on optional configuration. |
| `docs/events.md` | Changing anything under `src/gw2bot/events/`, especially an import inside `views/` or `posting/`. |
| `docs/web.md` | Changing a served page, the chrome they share, or how a document is assembled. |
| `docs/gw2-api.md` | Calling a GW2 API endpoint, for its response shape and quirks. |

`README.md` documents feature behaviour, every environment variable, and every
`/settings` subcommand with its default. It is the reference a server operator
reads, so a change a member or operator would notice belongs there too.

## Repository Overview

`gw2bot` is a Discord bot and poller for one Guild Wars 2 guild's server. It
watches Guild Storage and the guild log through the GW2 API, posts
notifications to a single configured channel, runs the guild's ticket raffle,
reports overdue Trial members, manages guild events with sign-up rosters, and
optionally serves a web calendar and feast usage dashboard.

Run the bot with `python -m gw2bot` and `PYTHONPATH=src`; `pytest.ini` and
`pyrightconfig.json` already put `src` on the path for tests and type checking.

Source lives under `src/gw2bot`:

| Path | Responsibility |
| --- | --- |
| `main.py` | Entrypoint: bootstraps the environment, opens the settings store, composes `Config`, installs the redacting log formatter, starts the bot. |
| `config.py` | `BootstrapConfig` and `bootstrap_from_env` for the environment-only variables, plus `Config` and every setting's default. |
| `bot.py` | The `discord.py` client: wires pollers, background tasks, and command groups. |
| `core/` | The shared layer with no feature knowledge: `database`, `logging_setup`, `discord_utils`, `anchored_series`, `dashboard_ranges`. Nothing in it may import anything else under `gw2bot` - see `docs/core.md`. |
| `gw2/` | The GW2 API client (`api.py`) and the pollers that turn its responses into decisions: `guild_log`, `guild_storage`, `guild_stash`, `feast_stock`, `guild_members`, `member_count`. |
| `notifications/` | `delivery.py` for the notification channel, `diagnostics.py` for the `diag` previews, `poll_status.py` for poll failure and recovery. |
| `invites/` | The accounts invited in-game that have not accepted: the report behind `/pending` and the roster page's section. |
| `raffle/` | Ticket ledger, draws, reports, and `/raffle` commands. |
| `roster/` | Guild membership history: the series the roster page draws, and the one-time `/roster import`. |
| `gold/` | Guild bank gold history: the series the gold page draws, and the one-time `/gold import`. |
| `profit/` | Trading Post profit reports: the member's API key, the price and delivery reads behind it, and `/profit`. |
| `trials/` | Trial member tracking, the Accepted forum index, `/check` and `/track`. |
| `events/` | Guild events: models, store, scheduler, reminders, `/event` commands, plus `posting/` and `views/` - see `docs/events.md`. |
| `web/` | Optional aiohttp site: Discord OAuth, the server, and one module per served document under `pages/` - see `docs/web.md`. |
| `settings/` | `/settings`: definitions, store, encryption, composition onto `Config` - see `docs/settings.md`. |

### Tests

`tests/` mirrors the source by feature rather than file. Most features have a
matching `tests/test_<feature>.py`, closely related ones share a single module
(all of `trials/` is covered by `tests/test_trials.py`, and all of
`events/views/` by `tests/test_event_commands.py`), and support code such as
`core/database.py`, the `models.py` files and the `views/` modules is exercised
through the modules that drive it. Put a new test in the module that already
covers its feature instead of adding a path per source file.

`tests/factories.py` holds the shared builders for fake guild-log events,
Discord errors, raffle totals, settings stores and /settings interactions, and
the fake bots that answer the optional-configuration guards.

`tests/test_layout.py` asserts the `core/` import boundary.
`tests/test_agent_docs.py` asserts that this file and `AGENTS.md` stay
identical and that every `docs/` file referenced above exists.
| Path             | Responsibility                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `main.py`        | Entrypoint: bootstraps the environment, opens the settings store, composes `Config`, installs the redacting log formatter, starts the bot.                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `config.py`      | `BootstrapConfig` and `bootstrap_from_env` for the environment-only variables, plus `Config` and the defaults every setting falls back to.                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `bot.py`         | The `discord.py` client: wires pollers, background tasks, and command groups.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `core/`          | The shared layer with no feature knowledge, which nothing in it may import back: `database.py` (SQLite engine, schema, and in-place migrations), `logging_setup.py` (`configure_logging` and `RedactingFormatter`, re-exported from `main`), `discord_utils.py` (role checks, ephemeral notices, Discord failure logging), `anchored_series.py` (a running total derived from an observed value and the changes around it, shared by the roster and gold histories), and `dashboard_ranges.py` (the window a dashboard draws and the one a member last picked). |
| `gw2/`           | The GW2 API client (`api.py`, with endpoint notes in `docs/gw2-api.md`) and the pollers that turn its responses into decisions: `guild_log.py`, `guild_storage.py`, `guild_stash.py`, `feast_stock.py`, `guild_members.py`, `member_count.py`.                                                                                                                                                                                                                                                                                                                  |
| `notifications/` | `delivery.py` for the notification channel, `diagnostics.py` for the `diag` previews, and `poll_status.py` for poll failure and recovery reporting.                                                                                                                                                                                                                                                                                                                                                                                                             |
| `invites/`       | The accounts invited in-game that have not accepted: the report behind `/pending` and the roster page's section.                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `raffle/`        | Ticket ledger, draws, reports, and `/raffle` commands.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `roster/`        | Guild membership history: the series the roster page draws, and the one-time `/roster import` from the log channel.                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `gold/`          | Guild bank gold history: the series the gold page draws, and the one-time `/gold import` from the guild log.                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `profit/`        | Trading Post profit reports: the member's API key, the price and delivery reads behind it, and `/profit`.                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `trials/`        | Trial member tracking, the Accepted forum index, `/check` and `/track`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `events/`        | Guild events: models, store, scheduler, reminders, `/event` commands, plus `posting/` (what an event does to Discord: `state`, `channels`, `pings`, `roster`, `messages`, `occurrences`) and `views/` (the UI, one module per flow: `shared`, `preview`, `create`, `field_edit`, `roster`, `lifecycle`, `signup`).                                                                                                                                                                                                                                              |
| `web/`           | Optional aiohttp site: Discord OAuth and the server, with one module per served document under `pages/`.                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `settings/`      | `/settings`: the definitions every subcommand is generated from, the store behind them, encryption for the credential-bearing ones, composition onto `Config`, and the one-time import from the environment.                                                                                                                                                                                                                                                                                                                                                    |

Feature behaviour, every environment variable, and every `/settings`
subcommand with its default are documented in `README.md`. It is the reference
a server operator reads, so a change a member or operator would notice belongs
there too.

### The `core/` Boundary

`core/` holds what every feature shares and nothing that knows about a
feature: the SQLite engine and its migrations, the redacting log formatter,
the Discord role and failure helpers, the anchored-series maths, and the
vocabulary a dashboard range is written in.

The dependency only ever points one way. Any package may import `core/`;
nothing in `core/` may import anything else under `gw2bot` - not a feature
package, not `gw2/`, not `bot.py`, not `config.py`. A single-dot relative
import stays inside `core/` and is fine; `from ..config import Config` is the
same escape as the absolute spelling and is rejected the same way.

`tests/test_layout.py` parses every module under `core/` and asserts this, so
the boundary is a failing test rather than something review has to catch. It
judges an import by where it lands: relative spellings are resolved against
the importing file's own package, and imports are read anywhere in the file,
so none of a `..` prefix, a function body or an `if TYPE_CHECKING:` block
gets around it.

The rule is the whole point of the directory. Without it `core/` becomes the
`utils/` folder that collects whatever had nowhere else to go, and the
layering it is supposed to express stops meaning anything. "Several features
use it" is not the test: something belongs in `core/` only if it is useful
without knowing what a raffle, an event or a Trial member is.

When something in `core/` looks like it needs a feature:

- Used by one feature - it belongs in that feature's package, not here.
- Used by several - take what it needs as an argument or a protocol instead
  of importing the feature to go and get it.
- Genuinely a new shared vocabulary - that is a new module in `core/`, not an
  import out of an existing one.
- Needed only for a type annotation - annotate against a protocol defined in
  `core/`, or leave the annotation as a string.

`gw2/` is _not_ under this rule and no test asserts anything about it.
`gw2/guild_log.py` imports `raffle` to parse gold deposits and render their
embed, so the layer is deliberately not pure, and extending the assertion to
`gw2/` would fail until that import is dealt with.

### Configuration

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
