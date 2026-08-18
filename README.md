# GW2 Discord Bot

A Python service for one Guild Wars 2 guild's Discord server. It polls Guild
Storage and the guild log and posts notifications to a server channel, runs the
guild's ticket raffle off gold deposits, reports overdue Trial members, manages
guild events with sign-up rosters, and optionally serves a web calendar. The API
client supports the account, token, and guild endpoints documented in
[docs/gw2-api.md](docs/gw2-api.md).

## Configuration

Almost everything the bot uses is a **setting**, changed from Discord with
`/settings` and stored in the bot's database. The environment keeps only what
has to be known before the bot can read that database or decide how the
container starts.

For local development, copy `.env.example` to `.env`.

### Required Environment Variables

| Variable | Description |
| --- | --- |
| `DISCORD_TOKEN` | Token for the Discord bot application. |
| `DISCORD_COMMAND_GUILD_ID` | Positive integer ID of the Discord server where commands are registered. |

These two are the only variables the bot refuses to start without.

### Optional Environment Variables

These stay in the environment because each one decides how the container
itself runs — where it writes, whether it opens a listening socket, and where
it is allowed to send the API key. Everything else is a `/settings` subcommand.

| Variable | Default | Description |
| --- | --- | --- |
| `DEBUG` | `false` | Set to `true` to enable detailed `gw2bot` application diagnostics in console logs. |
| `RAFFLE_DB_PATH` | `data/gw2bot.db` | SQLite database path. Settings live here too. The Docker image overrides this default with `/app/data/gw2bot.db`. |
| `WEB_ENABLED` | `false` | Set to `true` to serve the web calendar (see [Web Calendar](#web-calendar)). It opens the listening port; the calendar's four credentials are settings. |
| `WEB_PORT` | `2222` | Port the web calendar listens on. |
| `GW2_API_BASE_URL` | `https://api.guildwars2.com` | Base URL used for Guild Wars 2 API requests. Trailing slashes are removed. It decides where the API key is sent, which is why it is not settable from Discord. |
| `SETTINGS_ENCRYPTION_KEY` | unset | Fernet key used to encrypt the settings that hold credentials. Leave it unset and the bot generates `settings.key` next to the database on first run. See [Encrypted Settings](#encrypted-settings). |

The application loads `.env` automatically. Existing environment variables take
precedence over `.env`, so an Unraid container can inject the same variables at
runtime without using or mounting a `.env` file. The `.env` file is excluded
from Git and the Docker build context.

## Settings

`/settings` is how the bot is configured. Running a subcommand with no value
prints what the setting does and what it is currently set to; passing a value
validates and stores it; passing a space clears it.

Changes take effect immediately. Setting the Guild Wars 2 credentials starts
the pollers that need them, changing the notification channel redirects the
next message, and changing a calendar credential restarts the calendar — none
of it waits for a container restart. The reply says what had to be restarted.

Every reply is private to the person who ran the command.

### Who May Change Settings

Anyone holding the raffle officer role — `/settings roles raffle_officer`, see
below — plus the server owner and anyone with the `Administrator` permission.

The owner and administrator arms are deliberate: the officer role is itself a
setting, so if it were the only way in, one wrong value would lock everybody
out of the command that could fix it, with no environment variable left to
override it.

### Settings That Replaced Environment Variables

| Subcommand | Default | Description |
| --- | --- | --- |
| `/settings discord_notification_channel_id` | unset | Discord text channel that receives all automated notifications. Must belong to `DISCORD_COMMAND_GUILD_ID`. See [Running Without The Optional Credentials](#running-without-the-optional-credentials). |
| `/settings gw2_api_key` | unset | Guild Wars 2 API key with `account` and `guilds` permissions. Set it together with `gw2_guild_id`. **Encrypted.** |
| `/settings gw2_guild_id` | unset | Guild ID listed in `/v2/account.guild_leader`. Set it together with `gw2_api_key`. |
| `/settings feast_notification_user_id` | unset | Discord user who also receives feast stock alerts by private message. |
| `/settings gw2_poll_interval_seconds` | `300` | Guild Storage polling interval in seconds. At least `30`. |
| `/settings guild_log_poll_interval_seconds` | `60` | Guild log polling interval in seconds. At least `30`. |
| `/settings gw2_guild_member_cache_seconds` | `900` | Guild member cache lifetime in seconds. |
| `/settings timezone` | `UTC` | IANA timezone name (for example `America/New_York`) used to interpret typed `/event new` times, to name event threads, and to define weekly repeat days. |
| `/settings web_base_url` | unset | Public base URL of the web calendar, for example `https://calendar.example.com`. Trailing slashes are removed. |
| `/settings discord_oauth_client_id` | unset | OAuth2 client ID of the bot's Discord application. |
| `/settings discord_oauth_client_secret` | unset | OAuth2 client secret of the bot's Discord application. **Encrypted.** |
| `/settings web_session_secret` | unset | Random secret of at least 32 characters that signs web session cookies. Changing it signs every calendar user out. **Encrypted.** |
| `/settings web_session_ttl_seconds` | `604800` | How long a web sign-in stays valid. Guild membership is re-checked periodically regardless, so a departed member loses access without waiting for the session to expire. |

Three subcommands are named differently from the variable they replaced,
because Discord caps a command name at 32 characters:
`DISCORD_FEAST_NOTIFICATION_USER_ID` became `feast_notification_user_id`,
`GW2_GUILD_LOG_POLL_INTERVAL_SECONDS` became `guild_log_poll_interval_seconds`,
and `TZ` became `timezone`.

### Role, Channel And Forum Settings

These were fixed IDs in the source until they became settings. They keep those
values as defaults, so nothing changes until you change one, and clearing one
restores its default rather than switching the feature off.

Each subcommand suggests the server's roles, matching channels, or the Trial
forum's tags as you type, and refuses an ID that does not resolve — storing one
that nothing answers to is how a guild silently loses a feature.

| Subcommand | Default | Gates |
| --- | --- | --- |
| `/settings roles raffle_draw` | `1317124663847157880` | `/raffle draw` and `/raffle removetickets`. |
| `/settings roles raffle_addticket` | `1318357141521825872` | `/raffle addticket`, `/raffle addtickets`, `/raffle bulkaddtickets`. |
| `/settings roles raffle_officer` | `1317359168285573171` | Recording a gold purchase for someone, `/check`, `/track`, and `/settings`. |
| `/settings roles guild_roster` | `1317202210152513606` | Who gets in-game account names from the raffle autocompletes. |
| `/settings roles event_create` | `1318357141521825872` | Creating, editing, moving, cancelling and deleting events, and editing rosters. |
| `/settings roles trial` | `1450164501696741597` | Marks a Discord member as a Trial in `/check` and the overdue report. |
| `/settings roles sunborne` | `1317140660188352584` | Marks a Discord member as a full member in the same reports. |
| `/settings roles food_page` | follows `raffle_draw` | The feast usage dashboard. While unset it follows `/settings roles raffle_draw`. |
| `/settings channels raffle_contribution` | `856343628984746014` | Ticket purchase embeds, reward-tier milestones and the six-hourly contribution report. Separate from the notification channel. |
| `/settings channels trial_forum` | `1317206104727621693` | Forum holding Trial applications. Set this before its two tags, which are checked against whichever forum is configured. |
| `/settings channels trial_accepted_tag` | `1317349209619562587` | Tag marking an accepted application; only tagged posts are indexed. |
| `/settings channels trial_in_review_tag` | `1317349421821726790` | Tag the bot applies to a new application post. |

`raffle_addticket` and `event_create` are the same role today but are separate
settings, because they grant unrelated powers. Changing one no longer changes
the other.

### Encrypted Settings

The three settings marked **Encrypted** hold credentials and are encrypted in
the database. `/settings` never shows one back: once set, it reports
`This secret cannot be viewed once set`, and an unset one reports `Not set`.
There is no command that reveals the value — set it again to change it.

The key comes from `SETTINGS_ENCRYPTION_KEY` if you set it, and otherwise from
`settings.key`, generated beside the database on first run with `0600`
permissions. If a key file you restored or wrote yourself is readable by
anyone else, the bot narrows it to `0600` and says so — and refuses to start
if it cannot, rather than protecting your credentials with a key other local
users can read.

Back it up with the database: without the key the secrets cannot be read.

- **Replacing the key** is recoverable. The bot reports each secret it can no
  longer read, treats it as unset, and carries on; set those three values
  again.
- **A key file that is corrupt or truncated** — a half-finished restore, say —
  stops the bot with a message naming the file. It is not overwritten, because
  that would destroy the one thing that can read your stored secrets. Restore
  it from a backup, or delete it to start with a new key and set the three
  values again.

Setting the key yourself is what keeps it off the data volume. Everything else
about the feature works without it.

### Migrating From Environment Variables

The first time the bot starts after this upgrade it copies every legacy
variable it finds into the settings, once, and then never reads them again.
Nothing to do: an existing install comes up with exactly the values it had.

From then on the database is authoritative and a leftover variable is ignored.
The bot says so on every startup — a console warning, and one message in the
notification channel naming each stale variable and the `/settings` subcommand
that replaced it. Remove them from `.env` and the notices stop.

Because the import runs only once, a value you later clear with `/settings`
stays cleared even if the old variable is still in the environment.

Run `/settings list` to see every setting, its value, and whether it came from
`/settings` or is still the default.

### Running Without The Optional Credentials

The bot starts with only `DISCORD_TOKEN` and `DISCORD_COMMAND_GUILD_ID`. It
registers its commands, serves guild events, and keeps the raffle ledger it
already has, while every feature that needs a value you did not set is switched
off instead of stopping the bot. Each disabled feature is named in a startup
warning in the console.

Without `/settings gw2_api_key` **and** `/settings gw2_guild_id` — either one
missing disables all of it, because every Guild Wars 2 request needs both:

- Guild Storage polling stops, so there are no feast stock alerts and no feast
  count history.
- Guild log polling stops, so there are no join, leave, invite or rank-change
  messages, and no gold deposits are turned into raffle tickets.
- The overdue Trial member report and the guild member count channel
  description stop.
- `/raffle addticket`, `/raffle addtickets`, `/raffle bulkaddtickets`,
  `/raffle removetickets`, `/raffle tickets <username>`, the GW2 account link
  prompt, `/check` and `/track` answer privately with the commands to run,
  and their account-name autocompletes offer no choices. `/raffle draw`,
  `/raffle audit`, `/raffle list`, `/raffle leaderboard`, and `/raffle
  tickets` for a member who has already linked their account keep working
  from the recorded ledger.

The raffle database records which guild it belongs to the first time a guild id
is configured, so a database created without a guild id is claimed by the first
one you set afterwards. Setting a *different* one later is refused, and
`/settings` reports why.

Without `/settings discord_notification_channel_id`, nothing is posted to the
notification channel:

- Guild membership messages, raffle deposit audit lines, raffle command audit
  lines, feast stock alerts and Trial member reports are skipped.
- The channel description is not updated with the guild member count.
- The `diag` message is ignored, so the previews in
  [Automated Message Diagnostics](#automated-message-diagnostics) do not run.
- Commands that post an audit line still do their work and report that the
  audit log could not be delivered.

The bot is not silent, though: the raffle contribution channel is a separate
destination that the notification channel setting does not control. With the
Guild Wars 2 credentials set, every guild-log poll still posts gold-deposit
ticket purchase embeds and reward-tier milestone announcements there, and the
six-hourly contribution report is posted there whether or not those credentials
are set. Raffle draw announcements and the replies to every `/raffle`, `/check`
and `/track` command go to the channel the command was run in and are likewise
unaffected.

`WEB_ENABLED=true` without the calendar's four settings is not an error: the
bot names the missing ones in a startup warning and leaves the calendar off,
the same way an unset API key disables Guild Wars 2 polling.

Run the missing `/settings` subcommand and the feature switches on; nothing
else has to be reconfigured and the bot does not have to be restarted.

When `DEBUG=true`, detailed `gw2bot` diagnostics are written to the console.
Third-party library debug logging remains disabled, and credentials and full
notification contents are not included in application debug messages. All
console records, including third-party logs and exception tracebacks, pass
through a final credential-redacting formatter — including a credential set
with `/settings` while the bot is running.

The bot must have `View Channel` and `Send Messages` permissions in the
configured notification channel. Users running raffle commands must have
`Use Application Commands` permission.

If notification delivery reports Discord HTTP 403, use the logged Discord error
code to correct the channel configuration:

- Error code `50001` (`missing_access`): verify
  `/settings discord_notification_channel_id` identifies a channel in
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
the Trial application forum — `/settings channels trial_forum`, by default
`1317206104727621693` — so it can link Trial applications to Discord members.
Grant `Manage Threads` there as well so it can automatically tag new posts as
`In Review`.

## Guild Events

`/event` runs guild events end to end: a commander creates one, the bot posts it
with a live roster embed, members sign themselves up from that post, and a
one-minute maintenance pass keeps the status, the thread name, the reminders,
and a repeating series moving without anyone touching it again.

Every `/event` subcommand requires the event role `1318357141521825872`. The
sign-up buttons on a posted event are open to everyone who can see it.

| Command | What it does |
| --- | --- |
| `/event new` | Builds an event through a three-step flow and posts it. |
| `/event edit` | Reopens that flow for an existing event, and its roster with it. |
| `/event remind` | Pings the next occurrence's roster on demand (see [Event Reminders](#event-reminders)). |
| `/event cancel` | Calls off a repeating event's next occurrence (see [Cancelling An Event Occurrence](#cancelling-an-event-occurrence)). |
| `/event delete` | Removes an event, its messages, its signup threads, and its sign-ups. |

Every subcommand except `/event new` takes an `event_id`, autocompleted from the
active events as `[Category] Title — id N`. That id is also printed in the footer
of each event post as `eventID: N`, so it can be read off the message itself.

### Creating An Event

`/event new` walks three modals:

1. **Details** — category, title, description, destination channel or forum
   post, and the roles to ping (see [Event Role Pings](#event-role-pings)).
2. **Schedule** — start as `MM.dd.yyyy HH:mm`, duration as `HH:mm`, and whether
   the event repeats. Typed times are read in the timezone set with
   `/settings timezone` and must be in the future.
3. **Repeat** — only when the event repeats: frequency, which days, and whether
   posting the next occurrence should delete the previous one.

A private preview of the finished post follows each step. **Change something**
reopens any single field — category, title, description, channel, date & time,
duration, repeat settings, leader, or roles to ping — without walking the flow
again, and **Post event** sends it. Nothing is written to the database until the
event is posted, so abandoning the flow leaves nothing behind.

`/event edit` opens the same preview for an existing event, with **Save changes**
in place of **Post event**. Changing the channel re-posts the event at the new
destination and is confirmed first, because it deletes the current message and
any signup thread the bot opened for it. An event that has already started is
frozen: its stored details can no longer be changed, and the edit session opens
with its roster buttons alone. An event that is over cannot be edited at all.

### Squad Size And Roles

The category fixes the squad, and the bot enforces it:

| Category | Squad | Healers | DPS | Quickness | Alacrity |
| --- | ---: | ---: | ---: | ---: | ---: |
| Raid | 10 | 2 | 8 | 2 | 2 |
| Strike | 10 | 2 | 8 | 2 | 2 |
| Fractal | 5 | 1 | 4 | 1 | 1 |
| World vs. World | 50 | — | — | — | — |
| Open World | 50 | — | — | — | — |

Raid, Strike, and Fractal events are role-based: members pick one of Just DPS,
Quickness DPS, Alacrity DPS, Quickness Heal, or Alacrity Heal, and the healer,
DPS, quickness, and alacrity caps are all honoured at once. World vs. World and
Open World events are a plain headcount with no roles to pick.

### Signing Up

A posted event carries three buttons: **Sign up**, **Sign out**, and a ⚙️
settings button. Everything they open is private to the member who clicked.

On a role-based event, **Sign up** asks for a main role and then any flex roles
— other roles the member is willing to take. The picker marks each role as full,
or as a waitlist-only pick, before it is chosen. A member is seated in their
main role when it still fits; otherwise a flex role is used, and the bot fills
the scarcer seats first, so a flexer lands on an open heal or boon seat before a
plain DPS one. When no acceptable seat is free the member joins the waitlist,
marked ⌛️ in the embed. A World vs. World or Open World event has no roles to
pick, so one click seats the member or waitlists them.

Seats are re-shuffled on every roster change: signing out promotes waitlisted
members and can move seated members between their acceptable roles to make room,
and each mutation posts one batched note in the signup thread naming who moved.
**Sign out** confirms first, and on a repeating event with automatic sign-up
still on it offers to switch that off too, so signing out of one occurrence does
not leave the member seated for the next.

The ⚙️ button shows the member's settings for that event and offers
**Edit my signup** (role-based events only), **Enable**/**Disable auto sign-up**,
and **Reset role memory for this event**. Editing a signup is rate limited to
three edits back to back, refilling one every three hours, so a roster is not
churned by one member re-picking repeatedly. Signing out and back in resets the
allowance but costs the member their queue position. An edit that no longer fits
the roster asks before dropping the member to the waitlist.

### Status

An occurrence's status drives both its embed colour and the name of its signup
thread, `<status> | MM.dd.yyyy | HH:mm`:

| Status | Meaning |
| --- | --- |
| 🟢 open | Seats or boon coverage still missing. |
| 🔴 full | Every seat taken **and** the required boon coverage present. |
| 🟡 ongoing | Started, not yet finished. |
| ⚫️ over | Past its start plus duration. |

A roster that occupies every seat without covering its required boons keeps
reading as open rather than full, because it is still short of a squad.

### Repeating Events

A repeating event recurs daily, weekly on named days, or monthly on numbered
days; a monthly day past the end of a short month lands on that month's last
day. The next occurrence is created when the current one ends and posted by the
maintenance pass, carrying a fresh roster.

**Delete the previous post on repeat** is asked when the repeat is set up. With
it on, posting a new occurrence removes the superseded message and its signup
thread, so the channel holds only the current run; with it off, the old posts
stay as a record. A forum post the event was only sent into is never removed
either way.

### Automatic Sign-Up And Role Memory

Both are per event and offered only on repeating events, where signing up more
than once is the point. After a sign-up the bot offers to remember the roles
picked and to sign the member up automatically for later occurrences; either
prompt can be answered with "never ask again for this event". Remembered roles
are re-applied when a new occurrence is seeded, subject to the same capacity
rules as a manual sign-up, so a member can still land on the waitlist. Both
settings are visible and resettable behind the ⚙️ button, and a preference
stored for an event that is later deleted is dropped with it.

### Editing A Roster

The `/event edit` preview also carries **Add sign-ups** and **Remove sign-ups**,
so a commander can seat a member who cannot click the button themselves and
remove one who is not coming. Added members are picked from the server and then
given a role; the reply says who was seated and who went to the waitlist.

A removed member is sent a direct message telling them so, and automatic
sign-up for that event is switched off for them at the same time — otherwise the
next occurrence would simply seat them again. Opening `/event edit` also checks
the roster against the server and drops members who have left it.

## Guild Event Destinations

`/event new` (and **Change something → Channel** on an existing event) can post
an event to a text channel or into a forum post that already exists:

- **Text channel** — the event is sent as a message and the bot opens a signup
  thread under it, named `<status> | MM.dd.yyyy | HH:mm` and renamed whenever the
  status changes or the occurrence is rescheduled.
- **Existing forum post** — the event is sent as a message inside the post,
  which stands in for the signup thread, since a forum post cannot hold threads
  of its own. Roster changes are announced in the post, and members who sign up
  are added to it. Nobody is ever removed from it, because its members are not
  one event's roster — the same post can hold several events, and members join
  it just to read it. Removing someone who signed out of one event would drop
  them from the others. Members leave the post themselves when they are done
  with it.

Forum *channels* are not offered, so the bot never opens a forum post of its own.
Threads under a text channel are not supported either: Discord's picker cannot
narrow public threads down to forum posts, so a thread picked from a text channel
is refused on submission and the picker reopens. An archived forum post may not
appear in the picker at all until someone reopens it.

A post the event was only sent into is never renamed and never deleted:
`/event delete`, `/event cancel`, a channel move, and a superseded recurring
occurrence each remove only the event's own message and leave the post (and
everything else in it) standing. Each event in a shared post therefore manages
just its own message.

Moving an event between a channel and a post works in both directions. Because
the roster is keyed to the occurrence rather than to the message, the message is
re-sent at the new destination and the roster carries over.

Discord archives a quiet post and then refuses messages and message edits in it,
so a dormant post is reopened before the bot posts an event, announces a roster
change, or refreshes an embed in it. Reopening needs `Manage Threads`; without it
the update is logged as a `50013` (`missing_permissions`) failure and retried on
the next maintenance pass.

Any destination selected for an event needs `View Channel` and `Send Messages`.
A text channel also needs `Create Public Threads` for the signup thread, and
`Manage Threads`: moving an event to a new channel, pruning a superseded
recurring occurrence, cancelling an occurrence, and deleting an event all
delete that occurrence's signup thread explicitly, because Discord does not
remove a thread on its own when its starter message is deleted. Without
`Manage Threads` those operations still remove the message but log a `50013`
(`missing_permissions`) error and leave the orphaned thread behind. A forum
post destination needs `Send Messages in Threads` instead of `Send Messages`,
and `Manage Threads` only to reopen it once Discord has archived it.

## Event Role Pings

An event can announce itself to up to three roles. They are asked for on the
first `/event new` screen, alongside the category, title, description and
destination, and can be changed later from **Change something → Roles to ping**
in either `/event new` or `/event edit`.

Only roles whose name starts with `[GW2]` are offered — the brackets are part of
the marker, and case does not matter, so `[gw2] WvW` is offered too. Everything
else in the server, `@everyone` included, is unreachable from the picker, and a
pick that was never offered is refused rather than trusted. The picker lists the
marked roles alphabetically; a server with none of them simply shows no question,
and the change flow says so instead of opening an empty picker.

The marker is checked again against the server immediately before each post goes
out, so it describes the roles actually notified rather than only what the picker
once offered. An event outlives that pick — a weekly series carries its roles for
months — so a role that has since been deleted, or renamed and repurposed out of
`[GW2]`, is skipped and the rest of the post goes up as usual. The role is
skipped, not forgotten: its id stays on the event, so renaming it back resumes
its pings.

The mentions are sent as the message text carrying the event, so they appear
above the embed and actually notify. Each occurrence is its own announcement: a
repeating event pings the roles again when the next occurrence is posted, and a
channel move pings them at the new destination, because that is where the post
now is. Refreshing an embed — a sign-up, a status change, a rename — never
re-sends the mentions, so nobody is pinged twice for one post.

The bot needs the `Mention @everyone, @here and All Roles` permission where the
event is posted to ping a role that members cannot mention themselves. Only the
event's own roles are ever allowed to be mentioned, so nothing in a title or
description can widen an event post into an `@everyone`.

Role pings belong to the post alone. Reminders ping the members who signed up,
never these roles.

## Event Reminders

Members who hold a seat on an event are pinged where the event was posted — its
signup thread, or the forum post it was sent into — as the start approaches:

- an hour before,
- fifteen minutes before,
- and as it starts.

Each ping reads:

```text
@Member1 @Member2: Kitty Cleanup starts in 15 minutes
```

The start is a Discord relative timestamp, so every member reads it in their own
locale and it stays accurate whichever reminder they are looking at. Mentions
that do not fit one Discord message are split over as many messages as they need,
so nobody on a large roster is dropped from the ping.

Only seated members are reminded. A waitlisted member has no place in the squad
yet, so a ping telling them it is starting would invite them to show up for a
seat they do not have. The roster is read at the moment the reminder goes out, so
a member who signs up between two reminders is included in the later one.

Each reminder names exactly the members it pings, so mention syntax in an event
title — `@everyone`, a role, or another user — stays inert text and a reminder
can only ever notify the roster it is for. An event's ping roles are not
reminded either: they were told the event exists when it was posted, and a
reminder is for the people who answered.

Each reminder is recorded once it has been resolved, so a restart, a second
maintenance pass, or an event that is edited never pings a roster twice. A
reminder is recorded without being sent when there is nobody seated, when the
occurrence has no thread or forum post to ping in, or when the occurrence has
already finished.

Reminders are resolved by the one-minute event maintenance pass, so one can
arrive up to a minute after its moment. If the bot is down across a reminder, it
sends only the most imminent reminder that is still due on the way back up and
records the ones it overtook, rather than delivering a burst of stale pings. A
reminder whose moment passed more than ten minutes ago is dropped entirely — an
event that already started is news, not a reminder. That window also bounds
retries: a reminder Discord refuses is retried on each maintenance pass until it
leaves the window, then recorded and dropped.

`/event remind event_id:<event>` sends the same ping on demand and requires the
event role `1318357141521825872`. It pings the roster of the event's earliest
unfinished occurrence and leaves the automatic reminders untouched, so a manual
ping never costs the event one of its scheduled ones. It reports back privately
how many members were pinged, and refuses when the event is over, has nobody
signed up, or has no thread or forum post yet.

## Cancelling An Event Occurrence

`/event cancel event_id:<event>` calls off a single run of a repeating event
and requires the event role `1318357141521825872`. It targets the event's
earliest unfinished occurrence — the same run `/event remind` pings — and
confirms which date it is about before anything is removed.

Confirming removes that occurrence's message, the signup thread the bot opened
for it, and everyone's sign-ups for it. A forum post the event was only posted
into is kept, as everywhere else. The event itself survives: its next
occurrence is created, seeded with the members who asked to be signed up
automatically, and posted straight away, so the channel never sits without a
post for the series.

An event that does not repeat has nothing after the run being cancelled, so
`/event cancel` offers the `/event delete` confirmation for one instead of
leaving an event behind that can never be posted again.

Nothing is removed until the next occurrence has been secured, so a
cancellation that fails leaves the run posted and can simply be retried. If the
next occurrence cannot be posted afterwards — the bot lost `Send Messages` in
the channel, say — the cancellation still stands and the reply names the
channel to check. That posting is then retried by every maintenance pass until
it goes through, so fixing the permission is enough to bring the series back
even though the cancellation removed its last post.

## Feast Stock Alerts

The monitor tracks these fixed Guild Storage consumable IDs:

| Guild Storage ID | Feast |
| --- | --- |
| `1078` | Bowl of Fruit Salad with Mint Garnish |
| `1089` | Cilantro and Cured Meat Flatbread |
| `1102` | Cilantro Lime Sous-Vide Steak |
| `1112` | Spherified Cilantro Oyster Soup |

`/v2/guild/:id/storage` reports a genuinely empty consumable as an entry with
`count: 0`; it does not omit depleted items. A tracked feast missing from the
response therefore means its count is unknown for that poll (e.g. a partial
API response), not that it is empty, so missing entries are ignored rather
than treated as zero. Storage is checked every five minutes. When a feast is
at or below 10, the configured Discord channel receives:

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

### Feast Stock Count History

Each poll also records a per-feast stock history: the on-hand count of each
tracked feast is written to the database only when it changes from the last
recorded value, and only the feasts that changed are written. Identical polls
produce no writes. The last-known count per feast is cached in memory after
being loaded from the database once (on the first poll, or after a restart),
so unchanged polls require no database read either. As with the low-stock
alerts above, a feast missing from a poll response leaves its cached count and
history untouched rather than logging it as zero.

## Overdue Trial Member Report

After connecting to Discord, the bot checks `/v2/guild/:id/members` for accounts
whose in-game guild rank is `Trial` and posts up to two reports to the configured
notification channel:

- **Trial members before the 14-day mark** — Trial accounts whose `joined`
  timestamp is less than 14 days old, restricted to members who are still `Trial`
  in-game but have already been given the Sunborne role in Discord (a premature
  promotion). A copy-and-paste congratulations code block is attached below the
  list so officers can announce the promotions. This report is omitted when no
  such member exists.
- **Trial members past the 14-day mark** — Trial accounts whose `joined`
  timestamp is at least 14 days old, awaiting confirmation that they can be ranked
  up to Sunborne. Members are grouped by resolved status (Sunborne in Discord,
  then Trial in Discord, then a linked Discord account with no resolved rank, then
  no Discord account resolved) and sorted alphabetically within each group.
  Accounts that an officer has marked with `/track` are excluded here and appear
  in the 7-day warning report instead.
- **Trial members past the 7-day warning mark (to be kicked)** — Trial accounts
  past the 14-day mark that an officer tracked with `/track` at least 7 days ago.
  The 7-day countdown starts from the moment `/track` is invoked, so a tracked
  member only appears here once 7 days have elapsed since they were warned. During
  that grace window they appear on neither report (they are removed from the
  past-14-day report when tracked and not yet due for kicking). The report is
  omitted when no tracked member is past the warning mark. A tracked member is
  automatically untracked once they are no longer an overdue Trial member (for
  example after promotion to Sunborne or leaving the guild), so they drop off
  both reports.

~~~text
Trial members before the 14-day mark
These users are still Trial in-game but already Sunborne in Discord:
* EarlySunborne.1234 - @DiscordUser

```
Congratulations to our members who have become Sunborne!
* EarlySunborne.1234 - @DiscordUser
```

Trial members past the 14-day mark
Please confirm whether these users have completed the challenges and can be ranked up to Sunborne:
* Linked.1234 - @DiscordUser - Sunborne
* Unresolved.5678
~~~

To match each reported account to its application, the bot maintains a
persistent index of the `Accepted` posts in the Trial application forum
(`/settings channels trial_forum`, tagged with
`/settings channels trial_accepted_tag`), stored in the same SQLite database as
the raffle data. On the first run the bot reads every
Accepted post's title and message bodies once and stores their normalized text
and post author. On later runs it only re-reads posts whose most recent activity
is newer than the last successful run minus a one-hour grace window; unchanged
posts are served from the index, and posts that lost the Accepted tag are dropped
from it. Account names are then matched locally against the cached post text, so
the report no longer issues a Discord message search per member (which previously
caused heavy `429` rate limiting).

When a post matches, its creator is linked using their Discord user ID, and the
creator's current Discord roles determine the status: Sunborne role
`1317140660188352584` or Trial role `1450164501696741597`. Role lookups are
resolved live each run (cached guild roles, then a member fetch), so a status
reflects the member's current rank rather than the indexed snapshot. A matched
post always includes the creator mention; accounts without a matching post remain
plain usernames. Report entries are grouped with Sunborne first, Trial second,
and unresolved roles last. Names are sorted within each group by a case-sensitive
order, so uppercase names come before lowercase ones (for example, `Zebra` before
`apple`).

The check runs once every day at 17:00 UTC and does not run immediately when
the bot starts. Each report is split into multiple
messages when necessary to stay within Discord's message-length limit. A report
is omitted entirely when it has no members, and nothing is posted when neither
report has any members.

- `/check`: builds the same before-, past-14-day, and 7-day warning reports on
  demand and returns them only to the invoker as ephemeral replies, without
  posting to the notification channel. It requires Officer role
  `1317359168285573171`, and replies "No Trial members to report." when no report
  has any members.
- `/track username:<account>`: toggles 7-day warning tracking for a guild
  member and requires Officer role `1317359168285573171`. Username autocomplete
  and submission resolve against the case-insensitive guild-member cache and
  reject accounts outside the configured guild. Running it for an untracked
  member starts tracking them (moving them from the past-14-day report to the
  7-day warning report); running it again untracks them. The ephemeral reply
  confirms the new state to the invoker, and an audit message is posted to the
  notification channel:

  ```text
  Username.1234 warning tracked by @DiscordUser
  Username.1234 warning untracked by @DiscordUser
  ```

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
the notification channel, alongside join and leave logs. Delivery to
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
- `/raffle audit run_id:<run>`: publicly shows everything needed to verify a
  past draw — every entrant's ticket range in the numbered line the winners were
  picked from, alphabetical by username, and each draw's winning ticket number
  and range. `run_id` is autocompleted from the recorded runs. After each draw
  one ticket left that winner and the line was renumbered, so each draw shows
  the winner's range at that moment. Entrant ranges are paged when more than a
  screenful of players entered. Runs drawn before entrant snapshots were kept
  show their recorded results with a note that the snapshot is unavailable.
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
- `/raffle leaderboard`: publicly lists every player's lifetime earned and
  purchased tickets across all raffles, using the same bolded `Purchased`,
  `Free`, and `Total` layout as `/raffle list`. Totals are summed from the
  persisted gold-deposit and free-ticket history, so they are unaffected by the
  ticket reset performed each `/raffle draw`. Players are ordered by total
  tickets descending and then username without regard to case, with page buttons
  when more than ten players have history.

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

The poller also logs guild invitations and rank changes to the same channel. For
every `invited` event the bot posts the inviter when the GW2 API reports one:

```text
Officer.5678 invited Username.1234 to the guild.
Username.1234 was invited to the guild.
```

For every `rank_change` event the bot posts the account that changed the rank
when the GW2 API reports one (an account that changes its own rank, or an
unattributed change, is reported without an actor):

```text
Officer.5678 changed Username.1234's guild rank from Trial to Sunborne.
Username.1234's guild rank changed from Trial to Sunborne.
```

Invite and rank-change delivery state is persisted like joins and leaves, so each
message is posted once per event, including across restarts.

Guild membership messages, raffle deposit audit messages, raffle command audit
messages, stock alerts, and
polling-status messages are posted in the channel named by
`/settings discord_notification_channel_id`.
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
in the notification channel, the bot posts read-only previews of:

- the next six-hour raffle contribution report using contributions currently
  recorded in its active interval, including free tickets;
- a gold-deposit ticket purchase embed, a raffle draw announcement, and a raffle
  audit;
- a guild join, guild leave, guild invite, guild rank change, gold-deposit audit
  log, manual ticket audit, purchased-ticket removal audit, and next
  reward-tier message;
- a low feast-stock alert, overdue Trial member report, and Trial 7-day warning
  report;
- the guild member count channel description as it currently stands.

If the raffle is already at the highest configured reward tier, the highest-tier
message is shown with a note that it has already been reached. Running `diag`
does not refresh the guild log, advance either scheduled report, or mark any
pending notification as sent. Feast alerts can also be sent as a configured
private message. Startup status and Guild Log poll failure/recovery messages are
console-only and therefore are not previewed as Discord messages. Every
diagnostic preview delivery is attempted independently, so one failed preview
does not prevent later previews from being sent.

Docker Compose stores the database in the persistent `bot-data` volume, along
with `settings.key` unless you set `SETTINGS_ENCRYPTION_KEY` yourself. Back
both up together: the key is what makes the encrypted settings readable. To
view the current totals:

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

## Web Calendar

An optional website shows the guild event calendar in day, week, and month
views. Events appear as one-line entries on their day, and hovering over an
entry shows the full details: category, description, times, duration, leader,
status, and roster counts. Future occurrences of repeating events that the
scheduler has not posted yet appear with dashed borders as projections, and
finished events stay visible dimmed on past days.

Access requires signing in with Discord. The site only requests the `identify`
OAuth scope and then checks with the bot that the signed-in user is a member
of `DISCORD_COMMAND_GUILD_ID`. Non-members receive a members-only page.
Sessions last `/settings web_session_ttl_seconds` (seven days by default). Membership is
re-checked at sign-in and then at most every five minutes for the life of the
session, so a member who leaves or is banned loses access within minutes rather
than keeping it until the cookie expires.

To enable it:

1. Open the bot's application in the
   [Discord developer portal](https://discord.com/developers/applications),
   go to OAuth2, and copy the Client ID and Client Secret.
2. Add `<web_base_url>/oauth/callback` (for example
   `https://calendar.example.com/oauth/callback`) to the application's
   OAuth2 redirect URIs.
3. Set `WEB_ENABLED=true` in `.env`, then run `/settings web_base_url`,
   `/settings discord_oauth_client_id`,
   `/settings discord_oauth_client_secret` and `/settings web_session_secret`.
   Generate the session secret with
   `python -c "import secrets; print(secrets.token_urlsafe(48))"`. The
   calendar starts as soon as the fourth one is set.
4. Publish the port by uncommenting the `ports` block in `compose.yaml`. It
   follows `WEB_PORT`, so there is nothing to change there if you move the port.

Serve the calendar behind a reverse proxy that terminates TLS. Discord only
accepts `https` redirect URIs (localhost excepted), and session cookies are
marked `Secure` only when `/settings web_base_url` uses `https`.

Times are shown in each viewer's local timezone. Weekly repeat days are
defined in the timezone set with `/settings timezone`, so viewers far from it
may
correctly see a repeating event land on an adjacent local weekday.

### Feast Usage Dashboard

The same site serves a **Feast Usage** page at `/food`, built from the per-feast
stock history described under
[Feast Stock Count History](#feast-stock-count-history). It charts each tracked
feast's on-hand count over the last 24 hours, 7 days, or 30 days, and lists the
removals in that window — when each drop happened, how large it was, and what
was left afterwards — with one tab per feast.

Access is narrower than the calendar's: on top of being a signed-in guild
member, the viewer must hold role `1317124663847157880`, the role that also
gates `/raffle draw` and `/raffle removetickets`. Everyone else gets an
officers-only page. Membership and the role are re-checked on the same schedule
as calendar access, so a member who loses the role loses the page within
minutes. The page is not linked from the calendar; browse to `/food` directly.

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
the matching feature module: `src/gw2bot/guild_log.py` for guild-log events,
`src/gw2bot/guild_storage.py` and `src/gw2bot/feast_stock.py` for feast stock
alerts and history, `src/gw2bot/raffle/` for raffle reports and commands,
`src/gw2bot/trials/` for Trial member tracking, `src/gw2bot/events/` for guild
events, `src/gw2bot/web/` for the calendar and feast usage site, and
`src/gw2bot/notifications.py` for delivery to the notification channel.
`src/gw2bot/database.py` owns the SQLite schema and its migrations,
`src/gw2bot/bot.py` wires the pollers and commands together, and
`src/gw2bot/main.py` is the entrypoint that loads configuration and installs
the redacting log formatter. `src/gw2bot/settings/` owns `/settings`: the
definitions every subcommand is generated from, the store behind them, the
encryption for the credential-bearing ones, and the one-time import from the
environment. Secrets are read only from `/settings` and the bootstrap
variables, and `.env` is excluded from both Git and the Docker build context.
