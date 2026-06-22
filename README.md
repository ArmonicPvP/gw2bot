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
| `DEBUG` | `false` | Set to `true` to enable detailed `gw2bot` application diagnostics in console logs. |
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

When `DEBUG=true`, detailed `gw2bot` diagnostics are written to the console.
Third-party library debug logging remains disabled, and credentials and full
notification contents are not included in application debug messages. All
console records, including third-party logs and exception tracebacks, pass
through a final credential-redacting formatter.

The bot must have `View Channel` and `Send Messages` permissions in the
configured notification channel. Users running raffle commands must have
`Use Application Commands` permission.

If notification delivery reports Discord HTTP 403, use the logged Discord error
code to correct the channel configuration:

- Error code `50001` (`missing_access`): verify
  `DISCORD_NOTIFICATION_CHANNEL_ID` identifies a channel in
  `DISCORD_COMMAND_GUILD_ID`, the bot is installed in that server, and the bot
  can view the channel.
- Error code `50013` (`missing_permissions`): grant the bot `View Channel` and
  `Send Messages` in the notification channel, checking category and
  channel-specific permission overrides. Grant `Manage Channels` there as well
  so it can update the channel description with the current guild member count.

Failed raffle-deposit audit messages remain pending and are retried during each
guild-log poll after permissions are corrected.

Enable the privileged `Message Content Intent` for the bot in the Discord
Developer Portal so it can respond to the notification-channel `diag` message.
The bot also needs `View Channel` and `Read Message History` permissions for
forum channel `1317206104727621693` so it can link Trial applications to
Discord members. Grant `Manage Threads` in that forum channel so the bot can
automatically tag new posts as `In Review`.

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

After connecting to Discord, the bot checks `/v2/guild/:id/members` for accounts
whose in-game guild rank is `Trial` and posts up to two reports to the configured
notification channel:

- **Trial members before the 14-day mark** — Trial accounts whose `joined`
  timestamp is less than 14 days old, restricted to members who are still `Trial`
  in-game but have already been given the Sunborne role in Discord (a premature
  promotion). This report is omitted when no such member exists.
- **Trial members past the 14-day mark** — Trial accounts whose `joined`
  timestamp is at least 14 days old, awaiting confirmation that they can be ranked
  up to Sunborne.

```text
Trial members before the 14-day mark
These users are still Trial in-game but already Sunborne in Discord:
* EarlySunborne.1234 - @DiscordUser - Sunborne

Trial members past the 14-day mark
Please confirm whether these users have completed the challenges and can be ranked up to Sunborne:
* Linked.1234 - @DiscordUser - Sunborne
* Unresolved.5678
```

For each reported account, the bot first scans `Accepted` thread-title metadata
in forum channel `1317206104727621693` without reading message histories. It
then uses Discord's indexed guild message search for bodies and comments only
for account names that remain unresolved. If Discord's search endpoint is
unavailable, it falls back to the full forum history scan. Indexed searches run
without a per-member delay; when Discord returns rate-limit error code `110000`,
the bot waits for the returned `retry_after` duration before retrying. When found,
the post creator is linked using their Discord user ID. The creator's
cached Discord roles determine the status: Sunborne role `1317140660188352584`
or Trial role `1450164501696741597`. A matched post always includes the creator
mention; accounts without a matching post remain plain usernames.
Report entries are grouped with Sunborne first, Trial second, and unresolved
roles last. Names are alphabetical within each group.

The check runs once every day at 17:00 UTC and does not run immediately when
the bot starts. Each report is split into multiple
messages when necessary to stay within Discord's message-length limit. A report
is omitted entirely when it has no members, and nothing is posted when neither
report has any members.

- `/check`: builds the same before- and past-14-day reports on demand and
  returns them only to the invoker as ephemeral replies, without posting to the
  notification channel. It requires Officer role `1317359168285573171`, and
  replies "No Trial members to report." when neither report has any members.

## Raffle Deposits

Every minute, the bot checks `/v2/guild/:id/log` for new gold deposits into the
guild vault. One complete gold purchases one raffle ticket. For example:

```text
Username.1234 deposited 3 gold and purchased 3 raffle tickets
```

The SQLite ledger stores exact lifetime deposited coins, current raffle tickets,
gold-purchased tickets, manually added tickets, credited event IDs, pending
notifications and reward milestones, completed raffle runs, and the last
processed guild-log event ID. On the first run, the cursor starts at the latest
existing event so historical deposits are not credited. Deposits made while the
bot is offline are processed when it starts again.

Gold deposits can purchase at most 10 tickets per user in the current raffle.
Deposited gold above that limit still contributes to the user's lifetime gold
total. Accounts with the exact in-game rank `Officer` receive tickets only when
an individual deposit is 10 gold or less. Larger Officer deposits are ignored
by the raffle workflow, so they create no purchase record, lifetime-deposit
total, or deposit notification. The gold-purchased ticket count resets when a
raffle runs.

On the first startup after upgrading to the one-free-ticket limit, existing
players with multiple free tickets are reduced to one free ticket. Purchased
tickets are preserved. The correction is recorded and does not run again.

Deposit notifications are posted to both the raffle contribution channel and
`DISCORD_NOTIFICATION_CHANNEL_ID`, alongside join and leave logs. Delivery to
each channel is tracked independently and retried after failures. Every six
hours at `00:00`, `06:00`, `12:00`, and `18:00` UTC, the bot also posts the
players who purchased tickets or received free tickets during the preceding
six-hour window to the raffle contribution channel. The report uses the same
mobile-friendly layout as `/raffle list`: each bolded account name is followed
by separate `Purchased`, `Free`, and `Total` lines. It is ordered by total
tickets descending and then username without regard to case, with page buttons
when more than ten players contributed. Empty windows do not produce a message.
If the boundary-time guild-log refresh times out, the bot logs the refresh
failure and still posts the report from contributions already persisted by the
one-minute guild-log poller.

Purchased-ticket reward milestones are also posted to the raffle contribution
channel once per raffle. The defaults are:

| Purchased tickets | Reward tier |
| ---: | --- |
| 50 | Tier 1 |
| 100 | Tier 2 |
| 150 | Tier 3 |
| 200 | Tier 4 |

Modify `RAFFLE_REWARD_TIERS` in `gw2bot.raffle` to add tiers or change their
thresholds and labels. Pending milestone announcements persist across restarts
and retry after Discord delivery failures.

The raffle draw count is also data-driven through `RAFFLE_DRAW_TIERS`:

| Current purchased-ticket tier | Winners drawn |
| --- | ---: |
| Guaranteed / Tier 0 | 2 |
| Tier 1 | 2 |
| Tier 2 | 3 |
| Tier 3 | 4 |
| Tier 4 | 5 |

Each winner is selected from the remaining weighted ticket pool, then exactly
one of that winner's tickets is removed before the next draw. A player may win
multiple times while they still have tickets in the pool. If fewer tickets
remain than the configured winner count, every remaining ticket is drawn once.
Free tickets participate in the weighted draw but do not increase the current
purchased-ticket reward tier.

ArenaNet's guild-log API does not identify which guild-vault tab received a
coin deposit. The bot therefore cannot safely exclude only Officer or Guild
Master deposits made into a tab named `Treasure Trove`; excluding those ranks
would necessarily exclude their deposits into every guild-vault tab. The
Officer deposit-size rule above applies regardless of the destination tab.

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

- `/raffle draw`: requires role `1317124663847157880`. Randomly selects the
  tier-configured number of winners, weighted by each user's current tickets
  after refreshing the guild log. One winning ticket leaves the pool after
  each selection, so users with multiple tickets may win multiple times. The
  ordered winners and participant ticket counts are archived, then every
  user's current, gold-purchased, and manually added ticket counts reset to
  zero. A completed draw remains pending until Discord accepts its winner
  announcement; running the command again retries that announcement before
  allowing another draw.
- `/raffle addticket username:<account> [amount:<number>]`: without `amount`,
  adds one manual ticket to a current guild member and requires role
  `1318357141521825872`. Supplying `amount` requires Officer role
  `1317359168285573171` and records that many gold-purchased tickets as a real
  deposit event, including lifetime deposited gold, purchase notifications,
  contribution reports, and reward milestones. The purchase fails without
  adding tickets if it would exceed the per-user purchased-ticket cap. The
  command uses a case-insensitive guild-member cache and returns an error for
  accounts outside the configured guild. Username autocomplete immediately
  searches the current cached snapshot and refreshes expired data in the
  background, while command submission still waits for current guild
  membership validation. Each user may receive at most one manually added
  ticket per raffle.
- `/raffle addtickets [username1:<account> ... username10:<account>]`: adds one
  manual ticket to each of up to ten selected guild members and requires the
  manual-ticket role `1318357141521825872`.
- `/raffle bulkaddtickets`: opens a large text field for pasting squad
  attendance lines such as `:Username.1234, Character Name`, then adds one
  manual ticket to each unique current guild member. It requires the
  manual-ticket role `1318357141521825872`.
- `/raffle removetickets username:<account> [amount:<number>]`: requires the
  same officer role as `/raffle draw` and removes only current purchased
  tickets. The amount defaults to one. Free tickets and lifetime deposited gold
  are unchanged.
- `/raffle tickets [username:<account>]`: shows purchased, free, and total
  current raffle tickets. Without a username, the command uses the caller's
  linked GW2 account and prompts unlinked users to enter their account name.
- `/raffle list`: publicly lists players who currently have tickets, with each
  account name bolded above its purchased, free, and total ticket counts. It
  shows ten players per page, ordered by total tickets descending and then
  username without regard to case. Retained lifetime records with zero current
  tickets are omitted.

`/raffle draw` announces the ordered winners publicly. Ticket-addition and
removal confirmations and errors and `/raffle tickets` results are visible only
to the command user. Successful ticket additions and removals also send audit
logs through the same destination as guild-leave messages:

```text
@DiscordUser added 1 raffle ticket to Username.1234.
```

## Guild Membership Messages

The one-minute guild-log poller also detects new members and voluntary member
departures. For every `joined` event, the bot posts:

```text
Username.1234 has joined the guild.
```

For every voluntary departure event, reported by the GW2 API as a `kick` event
where `user` and `kicked_by` are the same account, the bot posts:

```text
Username.1234 has left the guild.
```

Guild membership messages, raffle deposit audit messages, raffle command audit
messages, stock alerts, and
polling-status messages are posted in `DISCORD_NOTIFICATION_CHANNEL_ID`.
Every minute, the bot updates that channel's description to the current GW2
guild member count as `x/500 (y pending)`, excluding `invited` records from
`x` and reporting them in `y`.
Raffle-deposit notifications are also posted in the raffle contribution
channel. Join, leave, and deposit delivery state is persisted so each message
is posted once per destination, including across restarts. Startup status and
guild-log polling failures and recovery are written only to the application
console logs.

## Automated Message Diagnostics

When a non-bot user sends exactly `diag`, ignoring case and surrounding spaces,
in `DISCORD_NOTIFICATION_CHANNEL_ID`, the bot posts read-only previews of:

- the next six-hour raffle contribution report using contributions currently
  recorded in its active interval, including free tickets;
- a gold-deposit ticket purchase, guild join, guild leave, and next reward-tier
  message;
- a low feast-stock alert, overdue Trial member report, and polling
  failure/recovery messages.

If the raffle is already at the highest configured reward tier, the highest-tier
message is shown with a note that it has already been reached. Running `diag`
does not refresh the guild log, advance either scheduled report, or mark any
pending notification as sent. Feast alerts can also be sent as a configured
private message. Startup status and Guild Log poll failure/recovery messages are
console-only and therefore are not previewed as Discord messages. Every
diagnostic preview delivery is attempted independently, so one failed preview
does not prevent later previews from being sent.

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
python -m pip install -r requirements-dev.txt
$env:PYTHONPATH = "$PWD\src"
python -m gw2bot
```

Run the tests:

```powershell
python -m pytest
```

Run the same Pylance/Pyright type checking used by CI:

```powershell
pyright
```

## Continuous Integration

The `CI` GitHub Actions workflow runs for pull requests targeting `main`, pushes
to `main`, and merge-queue groups. It provides these status checks:

- `Python checks`: installs dependencies, compiles and type-checks the Python
  source, and runs the pytest suite
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
