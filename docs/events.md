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
| `create.py` | Creating an event: three modals collecting the draft a step at a time, each retryable, the leader's role pickers, and the confirm views that write it. |
| `field_edit.py` | The "Change something" flow: pick a field, then edit or re-pick it. |
| `roster.py` | Editing a posted event's roster: taking members off and putting them on. |
| `lifecycle.py` | Opening and applying an edit (`open_event_edit` is `/event edit` itself), moving an event's channel, deleting and cancelling. |
| `restore.py` | `/event restore`: putting back a deleted event, or undoing an edit by saving the old details through `apply_event_edit`. |
| `signup.py` | What a member does with a posted message: signing up, out, and the settings behind it, and a leader handing their event over. |

The cycle is that previews open flows and flows re-render previews. So
`preview.py` imports `EventDetailsConfirmView`, `EventConfirmView`,
`EventEditConfirmView` (from `create`), `EventRosterEditView` (from `roster`)
and `ChangeFieldView` (from `field_edit`) inside the functions that build
them. At module scope those would be import cycles; deferred, the package
loads in the order above.

`views/__init__.py` re-exports the public names, so `bot.py`, the commands and
the tests import from `gw2bot.events.views` as before. Private helpers are not
re-exported: a test that needs one imports it from the module that owns it.

`shared.py` owns `role_pick_options` for the member and commander role
pickers, `_flex_role_options` for the member and leader flex pickers, and the
typed `_FlexRolesSelect` and `_FlexRolesView` bases. The flow-specific views
still decide what picking or skipping a role does.

An edit in `lifecycle.py` saves once, then delegates primary-run rescheduling,
roster reseating, and posted-message updates to `_sync_primary_start`,
`_reseat_occurrence`, and `_push_occurrence_update`. Reseating reports the
latest event and run together with whether the run is ready, retired, or
unreadable. An unreadable posted run counts as a failed refresh; a retired
one is skipped. Channel rollback and audit delivery stay in
`apply_event_edit`; `_edit_result_message` only formats the reply.

Batch roster additions in `views/roster.py` delegate seating and direct
messages to `_seat_roster_additions` and `_send_addition_notices`. Both return
the latest event and run, while `_AdditionOutcome` carries the results for
the single roster announcement and commander summary.

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

`posting/roster.py` shares the pure `_admission_solution` between live
signups and unpublished rosters; membership checks, writes and notifications
remain with their callers. Signup edits use `_write_signup_edit` to spend
and, when needed, refund a token, `_read_edited_signup` to describe the saved
result even if its read-back fails, and `_sync_auto_signup_roles` to carry
the new roles into future runs. A failure after the declaration is saved
does not report that edit as unsaved.

In `store.py`, `_delete_occurrence_dependents` and
`_delete_event_preferences` use the caller's session. Snapshot capture stays
before those deletes, and retirement still records the retained runs in the
same transaction. `formatting.event_embed` assembles the common frame and
delegates roster fields to the role-based or headcount field builder.

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

The history outlives the rows it copied, and SQLite hands a deleted row's id to
the next one created, so no copied id identifies anything alone: a run is found
by its occurrence id and start, and runs are grouped into events by the event
id and the event's `created_at` together.

## Restore snapshots

`gw2_event_snapshots` holds one row per event: the last change `/event
restore` can undo. The store writes it inside the transaction that makes the
change - `update_event(remember_previous=True)` for an edit, `delete_event` and
`retire_event` with `remember=True` for a deletion - so no path can change an
event without it, or keep it for a change that did not land. The payload is
the rows themselves, column for column, as JSON.

An edit that changes nothing does not replace the snapshot, and nor does
retiring an event that was already retired: either would throw away the one
change that can still be undone. Undoing uses the snapshot up
(`consume_snapshot=True`, or `restore_deleted_event`), so there is no undo of
an undo. Snapshots are not offered once they are a week old, and the
maintenance pass deletes them after that.

A deleted event keeps its id, because `create_event` will not hand the id of
an event with a deletion snapshot to a new one. That is what lets the restored
event keep its footer id and group with its run history, which is keyed by id
and `created_at` together.

Restored runs are new rows with no message, flagged `needs_refresh`. The
restore posts them straight away, but the flag is what lets the maintenance
pass post them if that fails: it skips an unposted run of a series with no
post of its own unless the run is flagged.

## Audit lines

`audit.py` records who created, edited, deleted or restored an event in the
notification channel, with the event as embeds: as posted, before and after an
edit, as it stood when it was deleted, or as it came back. The call sites are
`post_with_leader` in `views/create.py`, `apply_event_edit` and
`EventDeleteConfirmView.delete` in `views/lifecycle.py`, and the restore of a
deleted event in `views/restore.py`. A leader handover saves through
`apply_event_edit`, so it is recorded as an edit; so is undoing an edit, whose
line says it was undone. Each line is sent only once the change is stored,
and before the reply to the commander, so a reply Discord refuses cannot lose
the record. A line that fails to send never undoes the change.

The start an embed shows is a run's start, never the event's own: a repeating
event stores when its series began. An edit compares the run the draft was
seeded from with the start it saved, and a deletion shows the soonest run it
called off.

## Tests

`tests/test_event_commands.py` covers all of `views/`;
`tests/test_event_posting.py` covers `posting/`;
`tests/test_event_stats.py` covers `stats.py`. A test that intercepts a name
patches it on the module that looks it up, not on the package: for example
`gw2bot.events.views.roster.occurrence_has_ended`, because that is where
`apply_roster_addition` resolves it.
