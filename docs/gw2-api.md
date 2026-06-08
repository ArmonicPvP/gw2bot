# GW2 Account And Guild API Notes

The API base URL is `https://api.guildwars2.com`. Authenticated requests use:

```text
Authorization: Bearer <GW2 API key or subtoken>
```

The bot's API client implements every endpoint listed below. Guild data is not
yet polled by the bot because notification rules have not been defined.

## Authentication

### `/v2/tokeninfo`

Use this endpoint to inspect an API key or subtoken before relying on it.
Relevant response fields are:

- `permissions`: includes `account`, `guilds`, and any other granted scopes
- `type`: `APIKey` or `Subtoken`
- `expires_at` and `issued_at`: present for subtokens
- `urls`: present when a subtoken is restricted to specific endpoints

The bot needs `account` for `/v2/account`. The listed guild detail endpoints
also require `guilds`.

### `/v2/createsubtoken`

This endpoint creates a more restricted token from an existing key. It accepts:

- `expire`: ISO-8601 timestamp, capped at one year from creation
- `permissions`: comma-separated inherited permissions
- `urls`: optional comma-separated endpoint allowlist

Unrecognized or ungranted permissions are silently ignored, so verify a new
subtoken with `/v2/tokeninfo`. Deleting the parent API key invalidates its
subtokens.

## Account

### `/v2/account`

Requires `account`. Important fields for guild polling are:

- `id`: persistent account GUID; use this instead of the changeable account name
- `name`: display account name
- `guilds`: guild IDs associated with the account
- `guild_leader`: guild IDs led by the account; requires `guilds`
- `world`, `created`, `access`, and progression-related account fields

The base account response has some optional fields that depend on additional
permissions, including `guilds` and `progression`.

## Guilds

The detailed endpoints below require both `account` and `guilds`. They only
work when the API key belongs to an account that leads the requested guild.
Check that a configured guild ID appears in `/v2/account.guild_leader` before
polling it.

### `/v2/guild/:id/log`

Returns roughly the latest 100 events of each event type. Event IDs are unique
only within a guild. Pass `?since=<event-id>` to receive events newer than that
ID. Known event types include membership, rank, treasury, stash, MOTD, and
upgrade changes.

This endpoint is the best fit for incremental Discord notifications. Persisting
the last processed event ID will be necessary to avoid replaying events after a
restart.

Gold deposits are `stash` events with `operation` set to `deposit`. The `coins`
field is measured in copper, where `10000` copper is one gold. The API does not
identify which guild-vault section received the deposit.

Voluntary member departures are `kick` events where `user` and `kicked_by` are
the same account. A `kick` event with a different `kicked_by` account means
someone removed the member and is not reported as a voluntary leave. The bot
persists voluntary departures before posting the exact leave message to
Discord.

### `/v2/guild/:id/members`

Returns account name, rank, join timestamp, and WvW membership selection for
each guild member. The bot checks this endpoint at startup and daily at 17:00
UTC, then reports members whose rank is `Trial` and whose join timestamp is at
least 14 days old.

### `/v2/guild/:id/ranks`

Returns each rank's ID, sort order, permission IDs, and icon URL.

### `/v2/guild/:id/stash`

Returns guild vault sections, coins, notes, and slot-by-slot item contents.

### `/v2/guild/:id/storage`

Returns guild consumable IDs and counts. These IDs resolve against
`/v2/guild/upgrades`, not `/v2/items`. The feast monitor uses fixed consumable
IDs resolved from `/v2/guild/upgrades`.

### Item endpoints

`/v2/items` returns definitions for inventory items, including the crafted
Ascended Feast items. Those item IDs are not the IDs returned by Guild Storage.
`/v2/itemstats` describes selectable equipment attribute combinations and is
not relevant to feast storage counts.

### `/v2/guild/:id/treasury`

Returns treasury item IDs, current counts, and the in-progress upgrades that
need each item. The wiki notes that results may vary inconsistently by language.

## Sources

- [Account](https://wiki.guildwars2.com/wiki/API:2/account)
- [Create subtoken](https://wiki.guildwars2.com/wiki/API:2/createsubtoken)
- [Token info](https://wiki.guildwars2.com/wiki/API:2/tokeninfo)
- [Guild log](https://wiki.guildwars2.com/wiki/API:2/guild/:id/log)
- [Guild members](https://wiki.guildwars2.com/wiki/API:2/guild/:id/members)
- [Guild ranks](https://wiki.guildwars2.com/wiki/API:2/guild/:id/ranks)
- [Guild stash](https://wiki.guildwars2.com/wiki/API:2/guild/:id/stash)
- [Guild storage](https://wiki.guildwars2.com/wiki/API:2/guild/:id/storage)
- [Guild treasury](https://wiki.guildwars2.com/wiki/API:2/guild/:id/treasury)
- [Guild upgrades](https://wiki.guildwars2.com/wiki/API:2/guild/upgrades)
- [Items](https://wiki.guildwars2.com/wiki/API:2/items)
- [Item stats](https://wiki.guildwars2.com/wiki/API:2/itemstats)
