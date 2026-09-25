"""The Discord UI for guild events: every modal, view and button a member sees.

One module per flow. `shared` is the leaf they all build on, `preview` renders
the draft each flow returns to, and the rest are the flows themselves: creating
an event, changing one field of it, editing its roster, applying or undoing the
edit, and signing up to the posted message.
"""
from gw2bot.events.views.create import (
    ContinueToRepeatView as ContinueToRepeatView,
    EventConfirmView as EventConfirmView,
    EventDetailsConfirmView as EventDetailsConfirmView,
    EventDetailsModal as EventDetailsModal,
    EventEditConfirmView as EventEditConfirmView,
    EventRepeatModal as EventRepeatModal,
    EventScheduleModal as EventScheduleModal,
    RepeatChoiceView as RepeatChoiceView,
    RetryDetailsView as RetryDetailsView,
    RetryRepeatView as RetryRepeatView,
    RetryScheduleView as RetryScheduleView,
)
from gw2bot.events.views.field_edit import (
    CategoryPickSelect as CategoryPickSelect,
    CategoryPickView as CategoryPickView,
    ChangeFieldSelect as ChangeFieldSelect,
    ChangeFieldView as ChangeFieldView,
    ChannelPickSelect as ChannelPickSelect,
    ChannelPickView as ChannelPickView,
    EventFieldEditModal as EventFieldEditModal,
    LeaderPickSelect as LeaderPickSelect,
    LeaderPickView as LeaderPickView,
    MenteeSlotPickView as MenteeSlotPickView,
    PingRolesPickView as PingRolesPickView,
    PingRolesSelect as PingRolesSelect,
    RetryFieldEditView as RetryFieldEditView,
)
from gw2bot.events.views.lifecycle import (
    ChannelMoveConfirmView as ChannelMoveConfirmView,
    EventCancelConfirmView as EventCancelConfirmView,
    EventDeleteConfirmView as EventDeleteConfirmView,
    apply_event_edit as apply_event_edit,
)
from gw2bot.events.views.preview import (
    build_details_preview as build_details_preview,
    build_event_preview as build_event_preview,
    send_event_preview as send_event_preview,
)
from gw2bot.events.views.roster import (
    AddSignupsRoleSelect as AddSignupsRoleSelect,
    AddSignupsRoleView as AddSignupsRoleView,
    AddSignupsSelect as AddSignupsSelect,
    AddSignupsView as AddSignupsView,
    EventRosterEditView as EventRosterEditView,
    RemoveSignupsSelect as RemoveSignupsSelect,
    RemoveSignupsView as RemoveSignupsView,
    apply_roster_addition as apply_roster_addition,
    open_roster_addition as open_roster_addition,
    open_roster_removal as open_roster_removal,
    prune_departed_members as prune_departed_members,
)
from gw2bot.events.views.shared import (
    ADD_SELECT_MAX_MEMBERS as ADD_SELECT_MAX_MEMBERS,
    EVENT_CHANNEL_TYPES as EVENT_CHANNEL_TYPES,
    PING_ROLE_OPTION_LIMIT as PING_ROLE_OPTION_LIMIT,
    REMOVE_SELECT_PAGE_SIZE as REMOVE_SELECT_PAGE_SIZE,
    EventDraft as EventDraft,
    draft_from_event as draft_from_event,
    occurrence_has_ended as occurrence_has_ended,
    pingable_roles as pingable_roles,
)
from gw2bot.events.views.signup import (
    AutoSignupChoiceView as AutoSignupChoiceView,
    DisableAutoSignupView as DisableAutoSignupView,
    EditSignupFlow as EditSignupFlow,
    EditWaitlistConfirmView as EditWaitlistConfirmView,
    EventSettingsButton as EventSettingsButton,
    EventSignOutButton as EventSignOutButton,
    EventSignUpButton as EventSignUpButton,
    FlexRolesSelect as FlexRolesSelect,
    FlexRolesView as FlexRolesView,
    MenteeChoiceView as MenteeChoiceView,
    RememberChoiceView as RememberChoiceView,
    RolePickSelect as RolePickSelect,
    RolePickView as RolePickView,
    SignOutChoiceView as SignOutChoiceView,
    SignOutConfirmView as SignOutConfirmView,
    SignUpOfferView as SignUpOfferView,
    SignupFlow as SignupFlow,
    SignupSettingsView as SignupSettingsView,
    UpdateRememberedRolesView as UpdateRememberedRolesView,
    build_signup_view as build_signup_view,
    start_signup_flow as start_signup_flow,
)
