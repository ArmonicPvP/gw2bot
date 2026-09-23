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
| `core/` | The shared layer with no feature knowledge: `database`, `logging_setup`, `discord_utils`, `command_access`, `anchored_series`, `dashboard_ranges`. Nothing in it may import anything else under `gw2bot` - see `docs/core.md`. |
| `gw2/` | The GW2 API client (`api.py`) and the pollers that turn its responses into decisions: `guild_log`, `guild_storage`, `guild_stash`, `feast_stock`, `guild_members`, `member_count`. |
| `notifications/` | `delivery.py` for the notification channel, `diagnostics.py` for the `diag` previews, `poll_status.py` for poll failure and recovery. |
| `help/` | `/help`: walks the command tree and lists what the caller may run, judged by the access each command declares in its `extras` through `core/command_access`. |
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
