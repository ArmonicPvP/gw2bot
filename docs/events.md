# Guild events

*Read before changing anything under `src/gw2bot/events/`, and especially
before adding an import between modules in `views/` or `posting/`.*

`events/` is the largest feature in the repository: models, store, scheduler,
reminders and `/event` commands sit at the top level, with two packages under
it that were split out of single modules of 6,600 and 4,300 lines.

Both splits follow the dependency graph rather than the old file order, and
both hit the same wall: parts of the event UI and the posting pipeline are
genuinely mutually recursive. Where that happens the import goes inside the
function that needs it, with a comment saying why. Everything else is a
module-level import, and both packages are acyclic at import time.

## `views/` - the Discord UI

One module per flow, in dependency order:

| Module | What it holds |
| --- | --- |
| `shared.py` | The leaf. The draft a flow carries, the option builders its selects are filled from, the base views its modals subclass, and Discord's limits. It never imports a flow back. |
| `preview.py` | The preview a commander answers, and the confirmation attached to it. |
| `create.py` | Creating an event: three modals collecting the draft a step at a time, each retryable, and the confirm views that write it. |
| `field_edit.py` | The "Change something" flow: pick a field, then edit or re-pick it. |
| `roster.py` | Editing a posted event's roster: taking members off and putting them on. |
| `lifecycle.py` | Applying an edit, moving an event's channel, deleting and cancelling. |
| `signup.py` | What a member does with a posted message: signing up, out, and the settings behind it. |

The cycle is that previews open flows and flows re-render previews. So
`preview.py` imports `EventDetailsConfirmView`, `EventConfirmView`,
`EventEditConfirmView` (from `create`), `EventRosterEditView` (from `roster`)
and `ChangeFieldView` (from `field_edit`) inside the functions that build
them. At module scope those would be import cycles; deferred, the package
loads in the order above.

`views/__init__.py` re-exports the public names, so `bot.py`, the commands and
the tests import from `gw2bot.events.views` as before. Private helpers are not
re-exported: a test that needs one imports it from the module that owns it.

## `posting/` - what an event does to Discord

Also in dependency order:

| Module | What it holds |
| --- | --- |
| `state.py` | The leaf. Pure reads over an event and its occurrence - where it was posted, what status it is in, whether it has finished - with no Discord call behind any of them. |
| `channels.py` | Resolving the channel or thread an occurrence lives in, and reopening, renaming or deleting it. |
| `pings.py` | The separate ping message an occurrence announces itself with, and sweeping, retiring or dropping it. |
| `roster.py` | Who is seated, and every way that changes: seating, removal, rebalancing, membership re-checks, and a commander's edits. |
| `messages.py` | Posting the event message, refreshing it, and taking it down. One posting lock per occurrence serialises these. |
| `occurrences.py` | Cancelling an occurrence, pruning superseded ones, and seeding the next run of a recurring series. |

The cycle here is real in the domain: refreshing a post can seed the next
occurrence, and that occurrence's auto-sign-ups refresh a post. So
`roster.py` imports `refresh_occurrence_message` inside `seat_signup`,
`remove_signup` and `apply_signup_edit`, and `messages.py` imports
`ensure_next_recurring_occurrence` inside `refresh_occurrence_message`.

`occurrence_finished` and `occurrence_status` live in `state.py` rather than
with the code that reads them. That is what turned the roster and message
layers from mutually recursive into ordered - they only ever wanted the
predicate, not the module it used to sit in.

## Run history

`store.py` copies a run into `gw2_event_runs` and its roster into
`gw2_event_run_participants` inside the same transaction that marks the
occurrence OVER - `set_occurrence_status` and `retire_event` both call
`_record_run` - so every path that ends a run records it and none can forget
to. The occurrence rows cannot serve as the history themselves: `/event cancel`
and "delete previous on repeat" delete them. A cancelled run is never recorded
for that reason, and a run retired before it started (its post deleted ahead of
time) is judged against the `now` the status write is given, which is why the
posting paths pass their own clock through.

`stats.py` is the pure arithmetic the `/admin/events` page is drawn from, over
the runs one window holds. Nothing in it reads the store or Discord.

## Tests

`tests/test_event_commands.py` covers all of `views/`;
`tests/test_event_posting.py` covers `posting/`;
`tests/test_event_stats.py` covers `stats.py`. A test that intercepts a name
patches it on the module that looks it up, not on the package: for example
`gw2bot.events.views.roster.occurrence_has_ended`, because that is where
`apply_roster_addition` resolves it.
