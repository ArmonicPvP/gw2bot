"""Delivery to the notification channel, the `diag` previews, and poll status.

The delivery helpers are re-exported here because the rest of the bot reaches
them through `notifications.<name>`, which is how `bot.py` wires them.
"""

from gw2bot.notifications.delivery import (
    format_legacy_configuration_warning as format_legacy_configuration_warning,
    get_notification_channel as get_notification_channel,
    send_legacy_configuration_warning as send_legacy_configuration_warning,
    send_notification as send_notification,
    try_send_notification as try_send_notification,
)
from gw2bot.notifications.diagnostics import (
    format_automated_message_diagnostics as format_automated_message_diagnostics,
    send_automated_message_diagnostics as send_automated_message_diagnostics,
    try_send_automated_diagnostic as try_send_automated_diagnostic,
)
