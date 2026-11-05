"""Pack complete entries into Discord messages with repeated framing."""

from collections.abc import Sequence

DISCORD_MESSAGE_LIMIT = 2_000


def pack_message_lines(
    lines: Sequence[str],
    *,
    prefix: str,
    separator: str = "\n",
    suffix: str = "",
    limit: int = DISCORD_MESSAGE_LIMIT,
) -> list[str]:
    """Keep entries intact, reserving room for each message's suffix.

    An entry too large for an empty frame is kept on its own rather than
    producing a separate message containing only the frame.
    """
    messages: list[str] = []
    current = prefix
    holds_entry = False
    for line in lines:
        candidate = f"{current}{separator}{line}"
        if holds_entry and len(candidate) + len(suffix) > limit:
            messages.append(current + suffix)
            current = f"{prefix}{separator}{line}"
        else:
            current = candidate
        holds_entry = True
    messages.append(current + suffix)
    return messages
