# GW2 Discord Bot

A small Python service that monitors Guild Storage and the guild log, then posts
notifications to a Discord server channel. The API client also supports the
account, token, and guild endpoints documented in
[docs/gw2-api.md](docs/gw2-api.md).

## Configuration

For local development, copy `.env.example` to `.env`.

### Required Environment Variables

| Variable | Description |
| --- | --- |
| `DISCORD_TOKEN` | Token for the Discord bot application. |
| `DISCORD_COMMAND_GUILD_ID` | Positive integer ID of the Discord server where commands are registered. |
| `DISCORD_NOTIFICATION_CHANNEL_ID` | Positive integer ID of the Discord text channel that receives all automated notifications. The channel must belong to `DISCORD_COMMAND_GUILD_ID`. |
| `GW2_API_KEY` | Guild Wars 2 API key with `account` and `guilds` permissions. |
| `GW2_GUILD_ID` | Guild ID listed in `/v2/account.guild_leader`. |

### Optional Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `DISCORD_FEAST_NOTIFICATION_USER_ID` | unset | Discord user ID that also receives feast stock alerts by private message. |
| `GW2_POLL_INTERVAL_SECONDS` | `300` | Guild Storage polling interval in seconds. Must be a positive integer of at least `30`. |
| `GW2_GUILD_LOG_POLL_INTERVAL_SECONDS` | `60` | Guild log polling interval in seconds. Must be a positive integer of at least `30`. |
| `GW2_GUILD_MEMBER_CACHE_SECONDS` | `900` | Guild member cache lifetime in seconds. Must be a positive integer. |
| `RAFFLE_DB_PATH` | `data/gw2bot.db` | SQLite database path. The Docker image overrides this default with `/app/data/gw2bot.db`. |
| `GW2_API_BASE_URL` | `https://api.guildwars2.com` | Base URL used for Guild Wars 2 API requests. Trailing slashes are removed. |

The application loads `.env` automatically. Existing environment variables take
precedence over `.env`, so an Unraid container can inject the same variables at
runtime without using or mounting a `.env` file. The `.env` file is excluded
from Git and the Docker build context.

The bot must have `View Channel` and `Send Messages` permissions in the
configured notification channel. Users running raffle commands must have
`Use Application Commands` permission.

## Feast Stock Alerts

The monitor tracks these fixed Guild Storage consumable IDs:

| Guild Storage ID | Feast |
| --- | --- |
| `1078` | Bowl of Fruit Salad with Mint Garnish |
| `1089` | Cilantro and Cured Meat Flatbread |
| `1102` | Cilantro Lime Sous-Vide Steak |
| `1112` | Spherified Cilantro Oyster Soup |

A missing storage entry is treated as zero. Storage is checked every five
minutes. When a feast is at or below 10, the configured Discord channel
receives:

```text
Guild Storage is low on **<item>**: <count> left
```

When `DISCORD_FEAST_NOTIFICATION_USER_ID` is configured, the bot sends the same
alert to that Discord user by private message after posting it to the channel.
A private-message failure is logged but does not cause the channel alert to
repeat early.

While a feast remains at or below 10, its alert repeats once every eight hours.
When its count rises above 10, the reminder timer is cleared so a later drop
triggers an immediate alert. Reminder times are persisted across restarts.

## Overdue Trial Member Report

After connecting to Discord, the bot immediately checks `/v2/guild/:id/members`
for accounts whose in-game guild rank is `Trial` and whose `joined` timestamp is
at least 14 days old. If any are found, the configured notification channel
receives a report like:

```text
Trial members past the 14-day mark
Please confirm whether these users have completed the challenges and can be ranked up to Sunborne:
- Username.1234
```

The check runs again every day at 17:00 UTC. Reports are split into multiple
messages when necessary to stay within Discord's message-length limit. Nothing
is posted when no Trial members are past the 14-day mark.

## Raffle Deposits

Every minute, the bot checks `/v2/guild/:id/log` for new gold deposits into the
guild vault. One complete gold purchases one raffle ticket. For example:

```text
Username.1234 deposited 3 gold and purchased 3 raffle tickets
```

The SQLite ledger stores exact lifetime deposited coins, current raffle tickets,
gold-purchased tickets, manually added tickets, credited event IDs, pending
notifications, completed raffle runs, and the last processed guild-log event
ID. On the first run, the cursor starts at the latest existing event so
historical deposits are not credited. Deposits made while the bot is offline
are processed when it starts again.

Gold deposits can purchase at most 10 tickets per user in the current raffle.
Deposited gold above that limit still contributes to the user's lifetime gold
total. The gold-purchased ticket count resets when a raffle runs.

## Raffle Commands

The commands are server-only and require the bot application to be installed
with the `applications.commands` scope. Discord does not support hiding
individual application commands from arbitrary roles through normal command
registration, so authorization is enforced when each command runs.

If Discord reports `403 Forbidden (error code: 50001): Missing Access` during
command registration, verify that `DISCORD_COMMAND_GUILD_ID` is the Discord
server ID, then reinstall the application into that server with both the `bot`
and `applications.commands` scopes. The bot continues monitoring while command
registration is unavailable.

- `/raffle draw`: requires role `1317124663847157880`. Randomly selects a winner,
  weighted by each user's current tickets after refreshing the guild log. The
  run and participant ticket counts are archived, then every user's current,
  gold-purchased, and manually added ticket counts reset to zero. A completed
  draw remains pending until Discord accepts its winner announcement; running
  the command again retries that announcement before allowing another draw.
- `/raffle addticket username:<account>`: adds one manual ticket to a current
  guild member and requires role `1318357141521825872`. The command uses a
  case-insensitive guild-member cache and returns an error for accounts outside
  the configured guild. Each user may receive at most three manually added
  tickets per raffle.

`/raffle draw` announces the winner publicly. `/raffle addticket` confirmations
and errors are visible only to the command user. Successful ticket additions
also send this audit log through the same destination as guild-leave messages:

```text
@DiscordUser added 1 raffle ticket to Username.1234.
```

## Guild Leave Messages

The one-minute guild-log poller also detects new voluntary member departures.
For every voluntary departure event, reported by the GW2 API as a `kick` event
where `user` and `kicked_by` are the same account, the bot posts this exact
message:

```text
Username.1234 has left the guild.
```

Guild-leave messages, raffle audit messages, raffle-deposit notifications,
stock alerts, startup notices, and polling-status messages are posted in
`DISCORD_NOTIFICATION_CHANNEL_ID`. Leave events and delivery state are persisted
so each departure is posted once, including across restarts. Guild-log polling
failures and recovery are written only to the application console logs.

Docker Compose stores the database in the persistent `bot-data` volume. To view
the current totals:

```powershell
docker compose exec bot python -m gw2bot.raffle_totals
```

For Unraid, map persistent app data to `/app/data`:

| Host path | Container path | Access mode |
| --- | --- | --- |
| `/mnt/user/appdata/gw2bot` | `/app/data` | Read/Write |

Leave `RAFFLE_DB_PATH` unset, or set it to `/app/data/gw2bot.db`. Do not set it
to the host path. The image runs as UID `99` and GID `100`, matching Unraid's
usual `nobody:users` appdata ownership. If you are running an older image built
with UID `10001`, either rebuild the image or set Unraid's extra Docker
parameters to `--user 99:100`.

## Run With Docker

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Stop the bot with `Ctrl+C`, or run it in the background with:

```powershell
docker compose up --build -d
```

## Run Locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:PYTHONPATH = "$PWD\src"
python -m gw2bot
```

Run the unit tests:

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m unittest discover -s tests
```

## Continuous Integration

The `CI` GitHub Actions workflow runs for pull requests targeting `main`, pushes
to `main`, and merge-queue groups. It provides these status checks:

- `Python checks`: installs dependencies, compiles the Python source, and runs
  the unit tests
- `Docker build`: builds the production Docker image

To prevent merges when either check fails, configure an active GitHub branch
ruleset targeting `main`. Require pull requests and require both `Python checks`
and `Docker build` to pass before merging.

After `CI` succeeds for a push to `main`, the `Publish Docker image` workflow
publishes `DOCKERHUB_USERNAME/gw2bot` with `latest` and commit-SHA tags. Configure
the `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` repository secrets before merging.

## Extending Notifications

Add GW2 API methods in `src/gw2bot/gw2_api.py` and notification decisions in
`src/gw2bot/main.py`. Secrets are read only from environment variables and are
excluded from both Git and the Docker build context.
