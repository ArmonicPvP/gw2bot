"""Everything an event does to Discord once it has been created.

One module per concern: `state` reads an occurrence, `channels` resolves where
it lives, `pings` owns its announcement, `roster` owns who is seated on it,
`messages` posts and refreshes it, and `occurrences` cancels, prunes and
seeds runs.
"""
from gw2bot.events.posting.channels import (
    is_thread_channel as is_thread_channel,
    reopen_occurrence_thread as reopen_occurrence_thread,
    resolve_channel as resolve_channel,
    update_thread_membership as update_thread_membership,
)
from gw2bot.events.posting.messages import (
    delete_event_posts as delete_event_posts,
    post_occurrence as post_occurrence,
    post_pending_occurrence as post_pending_occurrence,
    refresh_occurrence_message as refresh_occurrence_message,
    refresh_retired_posts as refresh_retired_posts,
    repost_occurrence as repost_occurrence,
    split_event_history as split_event_history,
)
from gw2bot.events.posting.occurrences import (
    OccurrenceCancellation as OccurrenceCancellation,
    cancel_occurrence as cancel_occurrence,
    ensure_next_recurring_occurrence as ensure_next_recurring_occurrence,
    prune_superseded_occurrences as prune_superseded_occurrences,
)
from gw2bot.events.posting.pings import (
    sweep_stale_announcement as sweep_stale_announcement,
)
from gw2bot.events.posting.roster import (
    AutoSignupDisableResult as AutoSignupDisableResult,
    RosterUnreadable as RosterUnreadable,
    SignupEditResult as SignupEditResult,
    apply_auto_signups as apply_auto_signups,
    apply_signup_edit as apply_signup_edit,
    check_roster_membership as check_roster_membership,
    complete_signup as complete_signup,
    departed_roster_members as departed_roster_members,
    disable_auto_signup as disable_auto_signup,
    mentee_movement as mentee_movement,
    mentee_snapshot as mentee_snapshot,
    merge_roster_updates as merge_roster_updates,
    notify_roster_update as notify_roster_update,
    prune_departed_signups as prune_departed_signups,
    rebalance_occurrence_roster as rebalance_occurrence_roster,
    remove_signup as remove_signup,
    seat_signup as seat_signup,
    set_mentee_request as set_mentee_request,
)
from gw2bot.events.posting.state import (
    leading_occurrence as leading_occurrence,
    occurrence_channel_id as occurrence_channel_id,
    occurrence_finished as occurrence_finished,
    occurrence_status as occurrence_status,
)
