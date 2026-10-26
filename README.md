# GW2 Discord Bot

A Discord bot for one Guild Wars 2 guild's server. It watches Guild Storage and
the guild log, posts notifications to a channel, runs the guild's ticket
raffle, reports overdue Trial members, manages guild events with sign-up
rosters, and can serve a web calendar and dashboards.

## Quick Start

Either way, start by copying `.env.example` to `.env` and filling in
`DISCORD_TOKEN` and `DISCORD_COMMAND_GUILD_ID`:

```powershell
Copy-Item .env.example .env
```

With Docker:

```powershell
docker compose up --build -d
```

Locally:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
$env:PYTHONPATH = "$PWD\src"
python -m gw2bot
```

Then configure the rest from Discord with `/settings` (see [Settings](#settings)).

Docker Compose keeps the database, `settings.key` and the `log` folder in the
`bot-data` volume. The bot does not make backups; back the database and key up
together. If you set `SETTINGS_ENCRYPTION_KEY`, no `settings.key` is written,
so back that value up instead.

For Unraid, map `/mnt/user/appdata/gw2bot` to `/app/data` (read/write) and
leave `RAFFLE_DB_PATH` unset. The image runs as UID `99` and GID `100`.

## Environment Variables

Only what the bot needs before it can open its database stays in the
environment. `.env` is loaded automatically; variables already set in the
environment take precedence.

| Variable | Default | Description |
| --- | --- | --- |
| `DISCORD_TOKEN` | **required** | Discord bot token. |
| `DISCORD_COMMAND_GUILD_ID` | **required** | ID of the Discord server where commands are registered. |
| `DEBUG` | `false` | `true` for detailed `gw2bot` diagnostics in the console and [log file](#log-files). Credentials are always redacted. |
| `RAFFLE_DB_PATH` | `data/gw2bot.db` | SQLite database path (`/app/data/gw2bot.db` in the Docker image). Logs go in a `log` folder beside it. |
| `WEB_ENABLED` | `false` | `true` to serve the [web site](#web-site). |
| `WEB_PORT` | `2222` | Port the web site listens on. |
| `GW2_API_BASE_URL` | `https://api.guildwars2.com` | GW2 API base URL. Not settable from Discord because it decides where the API key is sent. |
| `SETTINGS_ENCRYPTION_KEY` | unset | Fernet key for encrypted settings and member profit keys. Unset, the bot generates `settings.key` beside the database. |

## Settings

Everything else is configured with `/settings` and stored in the database.
Run a subcommand with no value to see its description and current value, pass
a value to set it, or pass a space to clear it. Changes take effect
immediately, and `/settings list` shows every setting. Replies are private.

`/settings` can be run by the `raffle_officer` role, the server owner, and
anyone with `Administrator` (so a bad role value can never lock everyone out).

### General Settings

| Subcommand | Default | Description |
| --- | --- | --- |
| `/settings discord_notification_channel_id` | unset | Channel for automated notifications. Must be in `DISCORD_COMMAND_GUILD_ID`. |
| `/settings gw2_api_key` | unset | GW2 API key with `account` and `guilds` permissions. **Encrypted.** |
| `/settings gw2_guild_id` | unset | Guild ID from `/v2/account.guild_leader`. Set `gw2_api_key` first so the ID can be checked. The raffle ledger locks to the first guild ID it sees and refuses a different one later. |
| `/settings raffle_excluded_accounts` | unset | Comma-separated GW2 account names that earn no raffle tickets (case-insensitive). |
| `/settings feast_notification_user_id` | unset | Discord user who also gets feast stock alerts by DM. |
| `/settings gw2_poll_interval_seconds` | `300` | Guild Storage and stash polling interval (minimum `30`). |
| `/settings guild_log_poll_interval_seconds` | `60` | Guild log polling interval (minimum `30`). |
| `/settings gw2_guild_member_cache_seconds` | `900` | Guild member cache lifetime. |
| `/settings timezone` | `UTC` | IANA timezone for typed event times, thread names and weekly repeat days. |
| `/settings web_base_url` | unset | Public base URL of the web site, such as `https://gw2bot.example.com`. Must be a host root, not a path. |
| `/settings discord_oauth_client_id` | unset | OAuth2 client ID of the bot's Discord application. |
| `/settings discord_oauth_client_secret` | unset | OAuth2 client secret. **Encrypted.** |
| `/settings web_session_secret` | unset | Random secret (32+ characters) that signs web session cookies. Changing it signs everyone out. **Encrypted.** |
| `/settings web_session_ttl_seconds` | `604800` | How long a web sign-in lasts. Guild membership is re-checked every few minutes regardless. |

An excluded raffle account still has its deposits recorded, but earns no
tickets and cannot be given any with `/raffle addticket`. Removing the
exclusion does not award tickets retroactively.

### Roles And Channels

Each suggests matching roles, channels or tags as you type and refuses IDs that
do not resolve. `roles event_ping` is the exception: it takes a typed list of
role IDs, because a suggestion holds too few of them. Clearing one restores its
default.

| Subcommand | Default | Controls |
| --- | --- | --- |
| `/settings roles raffle_draw` | `1317638909735342201` | `/raffle draw`, `/raffle removetickets`. |
| `/settings roles raffle_addticket` | `1318357141521825872` | `/raffle addticket`, `/raffle addtickets`, `/raffle bulkaddtickets`. |
| `/settings roles raffle_officer` | `1317638909735342201` | Recording gold purchases, `/check`, `/track`, `/pending`, `/roster import`, `/gold import`, `/settings`. |
| `/settings roles guild_roster` | `1317202210152513606` | Who gets account names in the raffle autocompletes. |
| `/settings roles event_create` | `1318357141521825872` | All `/event` commands and roster editing. |
| `/settings roles trial` | `1450164501696741597` | Marks a member as a Trial in the Trial reports. |
| `/settings roles sunborne` | `1317140660188352584` | Marks a member as a full member in the Trial reports. |
| `/settings roles food_page` | follows `raffle_draw` | The feast usage dashboard. |
| `/settings roles roster_page` | `1317638909735342201` | The roster history dashboard. |
| `/settings roles gold_page` | `1317638909735342201` | The guild bank dashboard. |
| `/settings roles events_page` | `1317638909735342201` | The event statistics dashboard. |
| `/settings roles event_ping` | unset | Roles events may ping: up to 25 role IDs separated by commas, such as `123456789012345678, 234567890123456789`. Unset, no event pings anyone. |
| `/settings channels event_ping` | unset | Text channel where events posted into a forum post ping their roles. Unset, they ping inside the post. |
| `/settings channels raffle_contribution` | `856343628984746014` | Ticket purchases, reward milestones and the six-hourly contribution report. |
| `/settings channels trial_forum` | `1317206104727621693` | Forum holding Trial applications. Set before its tags. |
| `/settings channels trial_accepted_tag` | `1317349209619562587` | Tag marking an accepted application. |
| `/settings channels trial_in_review_tag` | `1317349421821726790` | Tag applied to new application posts. |

### Encrypted Settings

Encrypted settings and members' `/profit` API keys are never shown back; set
them again to change them. The key comes from `SETTINGS_ENCRYPTION_KEY` or the
generated `settings.key` (kept at `0600`). Back it up with the database.

- If the key is lost or replaced, the bot keeps running with the affected
  features off; set those values (and `/profit setkey`) again.
- If `settings.key` is corrupt, the bot refuses to start rather than overwrite
  it. Restore it, or delete it and set the encrypted values again.

### Migrating From Environment Variables

Older releases read every setting from the environment. On the first start
after upgrading, the bot imports those variables into `/settings` once and
then ignores them, warning about each leftover until you remove it. Keep `TZ`
set, since the container uses it for its own clock; change the bot's timezone
with `/settings timezone`.

Three were renamed: `DISCORD_FEAST_NOTIFICATION_USER_ID` →
`feast_notification_user_id`, `GW2_GUILD_LOG_POLL_INTERVAL_SECONDS` →
`guild_log_poll_interval_seconds`, and `TZ` → `timezone`.

### Running Without Optional Settings

The bot starts with just the two required variables. Anything missing
configuration is switched off (and named in a startup warning) rather than
stopping the bot, and turns on as soon as you set it:

- **No `gw2_api_key` or `gw2_guild_id`:** no polling, so no guild log messages,
  feast alerts, raffle deposits, history, Trial reports or member count.
  Commands that need the API reply with what to set. Existing raffle data still
  works for `/raffle draw`, `audit`, `list` and `leaderboard`.
- **No `discord_notification_channel_id`:** notification-channel messages and
  `diag` are skipped. The raffle contribution channel is separate and still
  receives posts.
- **`WEB_ENABLED=true` without the four web settings:** the site stays off.

## Discord Setup

Install the application with the `bot` and `applications.commands` scopes. If
command registration fails with `50001 Missing Access`, check
`DISCORD_COMMAND_GUILD_ID` and reinstall with both scopes.

In the Developer Portal, enable the **Message Content Intent** (for `diag`).
The **Server Members Intent** is optional; it lets the event statistics page
list commanders who led no runs.

Permissions the bot needs:

| Where | Permissions |
| --- | --- |
| Notification channel | View Channel, Send Messages, Manage Channels (for the member count description) |
| Trial application forum | View Channel, Read Message History, Manage Threads (to tag new posts) |
| Event text channels | View Channel, Send Messages, Create Public Threads, Manage Threads (to delete signup threads) |
| Event forum posts | View Channel, Send Messages in Threads, Manage Threads (to reopen archived posts) |
| Event channels and ping channel | Mention @everyone, @here and All Roles (to ping roles) |

A `50013 missing_permissions` error in the log means one of these is missing;
`50001 missing_access` means the bot cannot see the channel at all. Guild log
and raffle notifications are retried once the permission is fixed; an event's
role ping that Discord refuses is not, and that run goes without its ping.

## Log Files

The console output is also written as JSON lines to `log/gw2bot.jsonl` beside
the database (`/app/data/log/` in Docker), with `time`, `level`, `logger`,
`message` and, when present, `exception`. Files rotate at midnight and the
newest 30 are kept. For example, every warning and error:

```sh
jq -r 'select(.level == "WARNING" or .level == "ERROR") | "\(.time) \(.logger): \(.message)"' gw2bot.jsonl
```

## Features

### Help

`/help` privately lists the commands you can run, based on your roles right
now. In this README and in `/help`, `option:<value>` is required and
`[option:<value>]` is optional.

### Notifications

Posted to the notification channel:

- **Guild log:** joins, leaves, kicks, invites, rank changes, and gold
  withdrawals from the guild bank. Each is posted once, even across restarts.
- **Member count:** the channel description is kept at `x/500 (y pending)`,
  where `y` is accounts invited but not yet joined.
- **Feast stock:** an alert when any of the four tracked feasts (Guild Storage
  IDs `1078`, `1089`, `1102`, `1112`) is at 10 or fewer, repeated every eight
  hours while it stays low. Also sent by DM to `feast_notification_user_id`.
- **Trial reports:** daily at 17:00 UTC (see [Trial Members](#trial-members)).
- **Audit lines** for raffle deposits and raffle commands.

Send `diag` in the notification channel to preview every automated message
without changing any state.

### Raffle

Every whole gold deposited into the guild bank buys one raffle ticket.

- Each member can buy at most 10 tickets per raffle, and can receive at most one
  free ticket from officers.
- Accounts with the in-game `Officer` rank only earn tickets from deposits of
  10 gold or less; larger Officer deposits are ignored.
- Purchases, reward milestones, and a six-hourly report (00:00, 06:00, 12:00,
  18:00 UTC) go to the raffle contribution channel.
- Purchased tickets unlock reward tiers, which set the number of winners:

| Purchased tickets | Tier | Winners |
| ---: | --- | ---: |
| 0 | Guaranteed | 2 |
| 50 | Tier 1 | 2 |
| 100 | Tier 2 | 3 |
| 150 | Tier 3 | 4 |
| 200 | Tier 4 | 5 |

Winners are drawn weighted by tickets, removing one ticket per win, so a member
can win more than once. Tiers are defined by `RAFFLE_REWARD_TIERS` and
`RAFFLE_DRAW_TIERS` in `gw2bot.raffle`.

| Command | Role | What it does |
| --- | --- | --- |
| `/raffle draw` | `raffle_draw` | Draws the winners, archives the run, and resets everyone's tickets. |
| `/raffle audit run_id:<run>` | everyone | Shows every entrant's ticket range and each winning number for a past draw. |
| `/raffle addticket username:<account> [amount:<number>]` | `raffle_addticket`, or `raffle_officer` with `amount` | Gives one free ticket. With `amount`, records a gold purchase instead. |
| `/raffle addtickets [username1:<account> ... username10:<account>]` | `raffle_addticket` | Gives one free ticket to each of up to ten members. |
| `/raffle bulkaddtickets` | `raffle_addticket` | Gives a free ticket to each member in pasted squad attendance lines. |
| `/raffle removetickets username:<account> [amount:<number>]` | `raffle_draw` | Removes purchased tickets (default one). |
| `/raffle tickets [username:<account>]` | everyone | Shows a member's tickets, defaulting to your linked account. |
| `/raffle list` | everyone | Lists current ticket holders. |
| `/raffle leaderboard [sortby:<purchased\|free\|total>]` | everyone | Lists lifetime tickets across all raffles. |

### Trial Members

The bot compares in-game `Trial` members against their Discord roles, matching
accounts to members through `Accepted` posts in the Trial application forum. It
posts up to three reports daily:

- **Before the 14-day mark:** still Trial in-game but already Sunborne in
  Discord, with a congratulations message to copy.
- **Past the 14-day mark:** ready to be reviewed for promotion.
- **Past the 7-day warning:** members tracked with `/track` at least 7 days ago.

| Command | Role | What it does |
| --- | --- | --- |
| `/check` | `raffle_officer` | Shows the same reports privately. |
| `/track username:<account>` | `raffle_officer` | Starts or stops the 7-day warning for a member past 14 days. |
| `/pending` | `raffle_officer` | Lists accounts invited in-game that have not accepted, with their Discord member if matched. |

### Guild Events

| Command | What it does |
| --- | --- |
| `/event new` | Creates and posts an event through three short forms: details, schedule, and repeat. You are signed up for it as its leader. |
| `/event edit event_id:<event>` | Edits an event and its roster. Events that have started can only have their roster edited. |
| `/event remind event_id:<event>` | Pings the next run's seated members now. |
| `/event cancel event_id:<event>` | Cancels the next run of a repeating event and posts the one after. |
| `/event delete event_id:<event>` | Ends an event. Past runs stay on the calendar and in statistics. |

All `/event` commands need the `event_create` role. The `event_id` is shown in
each post's footer.

**Squads.** The category sets the squad size and roles:

| Category | Size | Healers | DPS | Quickness | Alacrity |
| --- | ---: | ---: | ---: | ---: | ---: |
| Raid, Strike | 10 | 2 | 8 | 2 | 2 |
| Fractal | 5 | 1 | 4 | 1 | 1 |
| Dungeon | 5 | 0 | 5 | 1 | 1 |
| Player vs. Player | 5 | 1 Support | 4 | 0 | 0 |
| Story | 5 | — | — | — | — |
| World vs. World, Open World | 50 | — | — | — | — |
| General | no limit | — | — | — | — |

Headcount events (no roles) can set their own size from 1 to 500, or 0 for no
limit.

**Signing up.** Members use the **Sign up**, **Sign out** and ⚙️ buttons on the
post. On role-based events they pick a main role and any flex roles; the bot
seats them where they fit, keeps the last seats for missing boons and healers,
and otherwise puts them on the waitlist (⌛️). Signing out promotes from the
waitlist. On repeating events, members can opt into automatic sign-up and
remembered roles from the ⚙️ menu.

**Leaders.** An event's leader is signed up for it when they post it. On a
role-based event, `/event new` asks for the leader's main and flex roles before
posting. If the leader was changed to someone else before posting, nobody is
signed up, since only the leader can pick their roles. On a repeating event the leader is signed up for every run
automatically, which they can turn off from the ⚙️ menu. A leader can still sign
out, after one extra confirmation. They are then offered a member picker to
hand the event to a new leader, which opens the `/event edit` preview with that
member as leader, to review and save. The new leader needs the `event_create`
role, and the leader of an event that has started cannot be changed.

**Mentee slot.** An event can offer one mentee slot for commander training.
Members who sign up are asked if they want it; extra takers go on a mentee
waitlist.

**Status.** The embed color and signup thread name (`<status> | MM.dd.yyyy |
HH:mm`) show 🟢 open, 🔴 full, 🟡 ongoing, or ⚫️ over.

**Destinations.** An event can be posted to a text channel, where the bot opens
a signup thread, or into an existing forum post. The bot only ever removes its
own messages from a forum post, never the post itself.

**Role pings.** An event can ping up to three of the roles listed in
`/settings roles event_ping`, which commanders pick from when they create or
edit it. They are pinged when each run is posted, and again if it is moved to
another channel, but never when the post is only edited. A role taken off the
list stops being pinged by the events that carry it, and listing it again
resumes them. For events in a forum post, set `/settings channels event_ping`
to ping from a text channel instead.

When upgrading from a release that picked roles by name, the roles whose names
start with `[GW2]` are stored in `/settings roles event_ping` once, on the first
start, so existing events keep pinging them. After that, a role's name makes no
difference.

**Reminders.** Seated members are pinged in the signup thread or forum post an
hour before, 15 minutes before, and at the start.

**Repeating events** run daily, weekly or monthly. Each new run is posted when
the previous one ends, and the previous post can be deleted automatically.

Members who leave the Discord server are removed from rosters automatically.

### Guild History

The bot records the guild's member count and guild bank balance over time,
along with every membership change and gold movement it sees. These power the
roster and gold dashboards. The guild log only keeps about the last 100 events
of each type, so changes that drop out of it while the bot is down are lost.

To backfill history from before the bot started recording, run each once
(`raffle_officer` role, safe to repeat):

- `/roster import` reads past join and leave messages from the notification
  channel.
- `/gold import` reads the guild log (about the last 100 events) and the
  current bank balance.

## Web Site

An optional site with the event calendar and dashboards. Members sign in with
Discord, and only members of the guild's server can view it.

To enable it:

1. In the [Discord developer portal](https://discord.com/developers/applications),
   copy the bot's OAuth2 Client ID and Client Secret.
2. Add `<web_base_url>/oauth/callback` to the application's OAuth2 redirects.
3. Set `WEB_ENABLED=true`, then set `/settings web_base_url`,
   `discord_oauth_client_id`, `discord_oauth_client_secret` and
   `web_session_secret` (generate one with
   `python -c "import secrets; print(secrets.token_urlsafe(48))"`).
4. Uncomment the `ports` block in `compose.yaml`.

Serve it behind an HTTPS reverse proxy at the root of a host; it cannot be
mounted under a path such as `/gw2bot/`.

| Path | Page | Access |
| --- | --- | --- |
| `/calendar` | Event calendar (`/` redirects here) | any member |
| `/profit` | Your Trading Post profit | any member |
| `/admin/food` | Feast usage | `food_page` role |
| `/admin/roster` | Guild roster history and pending invites | `roster_page` role |
| `/admin/gold` | Guild bank history | `gold_page` role |
| `/admin/events` | Event statistics | `events_page` role |
| `/healthz` | Returns `{"status": "ok", "version": "..."}` | any member |

The old `/food`, `/roster` and `/gold` addresses redirect to their `/admin`
pages.

**Calendar.** Day, week and month views of every event, including upcoming runs
of repeating events. Events you are signed up for are highlighted. Every event
post in Discord links to it.

**Profit.** Each member can save their own GW2 API key (with the `tradingpost`
permission) to see their Trading Post flipping profit. Sales are matched to
purchases first-in-first-out, after the 5% listing and 10% exchange fees. The
page shows realized profit by item and by day, matched trades, open orders,
unsold listings, and items waiting for pickup. Items or single trades that are
not flips can be hidden. The bot keeps every transaction it reads, so history
reaches back further than GW2's own ~90 days the longer the bot runs.

| Command | What it does |
| --- | --- |
| `/profit setkey` | Saves your API key (encrypted). |
| `/profit view [days:<number>]` | Links to your profit page, optionally for the last N days (1–3650). |
| `/profit deletekey` | Deletes your key and all your stored profit data. |

**Feast usage.** Feast stock over time, with removals, restocks and who made
them. Officers can record what each restock cost to chart how much feasts cost
the guild.

**Roster history.** The member count over time with every join, leave and
kick, plus a list of pending invites.

**Guild bank.** The bank balance over time, with every deposit and withdrawal.

**Event statistics.** Runs, hours, participants, runs per commander, and mentee
activity.

Dashboards offer 24h, 7d, 30d and custom ranges (up to 366 days, or 3650 for
profit), and remember your choice.

## Development

```powershell
python -m pytest   # tests
pyright            # type checking
```

The `CI` workflow runs on pull requests to `main`, pushes to `main`, and merge
groups, with three checks: `Branch name` (pull requests only), `Python checks`
(type checking and tests), and `Docker build`. Require all three in a branch
ruleset on `main`.

## Versioning And Releases

The bot follows [Semantic Versioning](https://semver.org/). A release is a
`vX.Y.Z` tag on `main`, a GitHub release, and a Docker image.

The version covers the bot's public API:

- environment variables
- `/settings` subcommands and their values
- slash commands and their options
- the database (every release upgrades an existing one in place)
- the Docker image: the `/app/data` volume, UID and GID, and default port
- web addresses, including the `/oauth/callback` redirect
- buttons on messages the bot has already posted

| Part | Goes up when |
| --- | --- |
| MAJOR | Something above is removed or renamed, or an upgrade needs operator action. |
| MINOR | Something is added or deprecated, and everything still works. |
| PATCH | A bug is fixed. |

Upgrades within a major version never need operator action. Databases are not
downgraded, so back up the data volume before an upgrade you may want to undo.

### Image Tags

Images are published to `ghcr.io/armonicpvp/gw2bot` (no longer Docker Hub).
For release `1.4.2`:

| Tag | Points at |
| --- | --- |
| `latest` | The newest release. |
| `1` | The newest `1.x.y`: features and fixes, never breaking. |
| `1.4` | The newest `1.4.x`: fixes only. |
| `1.4.2` | Exactly that release. |

Pin to the major tag (`:1`) to get updates without breaking changes. The bot
logs its version at startup; builds that are not releases report `0.0.0-dev`.

### How A Release Is Cut

The pull request's branch name decides the next version:

| Branch | For | Release |
| --- | --- | --- |
| `feat/<name>` | New behaviour members or operators can use | MINOR |
| `fix/<name>` | A bug fix | PATCH |
| `chore/<name>` | Tests, docs, CI, refactoring | none |

`<name>` is lowercase words joined by hyphens, such as `feat/profit-page`. A
breaking change also gets the `breaking` label, which makes it MAJOR.

After `CI` passes on `main`, the `Release` workflow takes the largest change
merged since the last tag, then builds and pushes the image, creates the tag
and GitHub release, and updates the image tags. If everything merged is a
chore, nothing is released. To release anyway, run **Actions > Release > Run
workflow** on `main`.

Never create, move or delete `v` tags by hand. A tag ruleset on `v*` that
restricts updates and deletions enforces this.
