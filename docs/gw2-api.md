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
also require `guilds`. A member key saved with `/profit setkey` is separate and
needs `tradingpost`; the command verifies that permission here before storing
the key. If `urls` is present, it also requires
`/v2/commerce/transactions/history/buys`,
`/v2/commerce/transactions/history/sells`,
`/v2/commerce/transactions/current/sells`,
`/v2/commerce/transactions/current/buys`, and
`/v2/commerce/delivery`; an unrestricted key has no `urls` field.

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

## Trading Post

The personal profit dashboard reads the endpoints below with the API key saved
for the signed-in Discord member. Every collection and cache row is keyed by
that Discord user ID.

### `/v2/commerce/transactions/history/buys`

### `/v2/commerce/transactions/history/sells`

These `tradingpost`-permission endpoints return completed purchases and sales,
newest first, and reach about ninety days back. The client reads them with
`page` and `page_size=200` and keeps every row it has ever seen, so a member's
history grows past what GW2 itself still serves.

How much is read depends on what is already stored:

- **First sync, or a store written before the watermark existed.** Page 0 is
  read to learn `X-Page-Total`, then the remaining pages are read together,
  eight at a time. An active trader has around sixty pages; reading them one
  after another took about forty seconds, and reading them this way takes
  about three.
- **Every later refresh.** Reading resumes at the newest transaction already
  stored and stops at the first page that reaches behind it, which is normally
  page 0. History is append-only and ordered newest first, so nothing older
  can have appeared. A five-minute overlap covers entries that land either side
  of the boundary.

Purchases and sales inside the requested window are matched FIFO per item.

### `/v2/commerce/transactions/current/sells`

Returns the member's current sale listings. The cached collection is replaced
as a snapshot rather than merged, so cancelled or completed listings disappear
from the next unrealized-profit report. It is one page, so it is always read
whole. All four transaction collections are refreshed after five minutes, and
the collections one dashboard section needs are read without waiting on
another section's.

### `/v2/commerce/transactions/current/buys`

Returns the member's outstanding buy orders, in the same shape as the sell
routes: `id`, `item_id`, `price`, `quantity`, and `created`. The Trading Post
splits one purchase into many orders, so this collection is long and repetitive;
the dashboard's **Open Orders** table collapses it to one row per item and
price. Like the current sells above, it is stored as a replacing snapshot so a
cancelled or filled order disappears from the next report.

This is the newest required route, so a member subtoken saved before it existed
may be restricted away from it. That single 401 or 403 leaves Open Orders marked
unavailable and does not store an empty snapshot, so a replacement key picks the
orders up on the next report; every other transaction collection failing
authorization still fails the report.

### `/v2/commerce/prices`

Returns `buys.unit_price` (the highest standing buy order) and
`sells.unit_price` (the lowest sell listing) for each requested item, in chunks
of 200 ids. The realized report reads it for the items flipped in the window
and the Open Orders section for the items on order; neither waits on the other.
An item whose response has a zero price on either side is skipped. It needs no
API key, and prices are never cached, because a stale spread is worse than no
spread.

### `/v2/items`

Names the requested items, in chunks of 200 ids, and needs no API key. Names
are stored and re-used for a month rather than for the five minutes a
transaction snapshot lasts: an item's name is fixed for the life of a game
build, and re-reading a few thousand of them on every page load was one of the
slower parts of a report. One failed chunk falls back to `Item <id>` for those
ids and never fails the report.

### `/v2/commerce/delivery`

Returns the items and copper waiting for pickup from the Trading Post as
`{"coins": <copper>, "items": [{"id": <item id>, "count": <quantity>}]}`. The
profit dashboard shows `coins` as unclaimed Trading Post gold and lists the
items one row per item; a single item delivered as several stacks has its counts
added together. Route-restricted member subtokens must allow this endpoint along
with the four transaction endpoints above. A legacy subtoken accepted before this
route was required can still load its transaction report; only the unclaimed
amount is marked unavailable after a 401 or 403, with the page prompting the
member to replace the key. Other delivery errors still fail the report.

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

Gold deposits are `stash` events with `operation` set to `deposit`, and
withdrawals the same events with `operation` set to `withdraw`; a third
operation, `move`, shuffles items between vault tabs and carries no coins. The
`coins` field is measured in copper, where `10000` copper is one gold. An item
deposit or withdrawal is a `stash` event too and reports `coins` as zero, so
the amount is what tells a coin movement from an item one.

The bot records both directions in a ledger of its own, separate from the
raffle's deposit table, and announces each withdrawal in the notification
channel. Because the log returns only about a hundred events per type, that
ledger reaches back only as far as the log does; `/gold import` reads the whole
log once, without `since`, to recover as much of it as remains. The API does not
identify which guild-vault section received the deposit. The stash snapshot
endpoint exposes current tab contents but cannot reliably attribute a tab
balance change to a specific guild-log event or depositor. As a result,
Treasure Trove-only deposit exclusions cannot be enforced from the API.
The bot instead checks the depositor's current rank from `/v2/guild/:id/members`.
Accounts with the exact rank `Officer` receive raffle tickets only for
individual deposits of 10 gold or less. Larger Officer deposits are ignored by
the raffle workflow and do not produce deposit notifications.

Voluntary member departures are `kick` events where `user` and `kicked_by` are
the same account. A `kick` event with a different `kicked_by` account means
someone removed the member and is not reported as a voluntary leave. The bot
persists voluntary departures before posting the exact leave message to
Discord. It also persists `joined` events before posting the exact join message.

Raffle gold deposits are also aggregated into fixed six-hour UTC reporting
windows ending at `00:00`, `06:00`, `12:00`, and `18:00`. The bot refreshes the
guild log at each boundary before posting contributors.

### `/v2/guild/:id/members`

Returns account name, rank, join timestamp, and WvW membership selection for
each guild member. The bot checks this endpoint daily at 17:00 UTC, then
reports members whose rank is `Trial` and whose join timestamp is at
least 14 days old. Before posting the report, the bot searches the configured
Discord Trial application forum's `Accepted` posts for each GW2 account name
and includes the linked post creator's Discord mention. The cached or current
Trial or Sunborne role is included when available.

### `/v2/guild/:id/ranks`

Returns each rank's ID, sort order, permission IDs, and icon URL.

### `/v2/guild/:id/stash`

Returns guild vault sections, coins, notes, and slot-by-slot item contents.

The bot polls this endpoint on the Guild Storage interval and sums `coins`
across every section into one balance. The guild log never says which section a
coin movement reached, and the gold history tracks the bank rather than any one
tab, so a single balance is what it records. That reading is the anchor every
derived balance on the `/gold` page is measured back from.

### `/v2/guild/:id/storage`

Returns guild consumable IDs and counts. These IDs resolve against
`/v2/guild/upgrades`, not `/v2/items`. The feast monitor uses fixed consumable
IDs resolved from `/v2/guild/upgrades`.

### Item endpoints

`/v2/items` returns definitions for inventory items, including the crafted
Ascended Feast items. The profit dashboard also resolves Trading Post item IDs
here in chunks of 200 and caches their display names. Those item IDs are not the
IDs returned by Guild Storage.
`/v2/itemstats` describes selectable equipment attribute combinations and is
not relevant to feast storage counts.

### `/v2/guild/:id/treasury`

Returns treasury item IDs, current counts, and the in-progress upgrades that
need each item. The wiki notes that results may vary inconsistently by language.

## Sources

- [Account](https://wiki.guildwars2.com/wiki/API:2/account)
- [Create subtoken](https://wiki.guildwars2.com/wiki/API:2/createsubtoken)
- [Token info](https://wiki.guildwars2.com/wiki/API:2/tokeninfo)
- [Trading Post transactions](https://wiki.guildwars2.com/wiki/API:2/commerce/transactions)
- [Guild log](https://wiki.guildwars2.com/wiki/API:2/guild/:id/log)
- [Guild members](https://wiki.guildwars2.com/wiki/API:2/guild/:id/members)
- [Guild ranks](https://wiki.guildwars2.com/wiki/API:2/guild/:id/ranks)
- [Guild stash](https://wiki.guildwars2.com/wiki/API:2/guild/:id/stash)
- [Guild storage](https://wiki.guildwars2.com/wiki/API:2/guild/:id/storage)
- [Guild treasury](https://wiki.guildwars2.com/wiki/API:2/guild/:id/treasury)
- [Guild upgrades](https://wiki.guildwars2.com/wiki/API:2/guild/upgrades)
- [Items](https://wiki.guildwars2.com/wiki/API:2/items)
- [Item stats](https://wiki.guildwars2.com/wiki/API:2/itemstats)
