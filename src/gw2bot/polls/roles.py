from __future__ import annotations

from gw2bot.events.roles import EVENT_CREATE_ROLE_ID

# Poll management reuses the event-creator role. Aliasing it here keeps the poll
# feature's authorization in one place, so switching polls to a dedicated role
# later is a one-line change.
POLL_MANAGE_ROLE_ID = EVENT_CREATE_ROLE_ID
