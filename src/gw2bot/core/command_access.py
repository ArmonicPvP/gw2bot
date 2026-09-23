"""Who may run a slash command, declared on the command itself.

Each command still enforces its own gate when it runs; this is the description
of that gate that `/help` reads to decide what to show a caller. It lives in
the command's ``extras`` so the declaration sits next to the check it
describes, and a role is named by the settings field that holds it rather than
by its id, so a role changed through `/settings` is what `/help` answers with
on the very next call.

An option can carry a stricter rule than its command - `/raffle addticket`'s
``amount`` is for officers only - and a caller who fails it does not see the
option at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .discord_utils import user_has_role

ACCESS_EXTRA = "access"
OPTION_ACCESS_EXTRA = "option_access"


@dataclass(frozen=True)
class Everyone:
    """Anyone who can see the command in Discord may run it."""

    def allows(self, user: Any, guild: Any, settings: object) -> bool:
        return True


@dataclass(frozen=True)
class RoleSetting:
    """Holders of the role whose id is stored in the settings field ``field``."""

    field: str

    def allows(self, user: Any, guild: Any, settings: object) -> bool:
        return user_has_role(user, getattr(settings, self.field))


@dataclass(frozen=True)
class ServerAdministrator:
    """The server owner, or anyone holding the Administrator permission."""

    def allows(self, user: Any, guild: Any, settings: object) -> bool:
        if guild is not None and getattr(guild, "owner_id", None) == getattr(
            user, "id", None
        ):
            return True
        permissions = getattr(user, "guild_permissions", None)
        return bool(getattr(permissions, "administrator", False))


@dataclass(frozen=True)
class AnyOf:
    """Callers any one of ``rules`` lets through."""

    rules: tuple[AccessRule, ...]

    def allows(self, user: Any, guild: Any, settings: object) -> bool:
        return any(rule.allows(user, guild, settings) for rule in self.rules)


type AccessRule = Everyone | RoleSetting | ServerAdministrator | AnyOf


def access_extras(
    access: AccessRule,
    options: Mapping[str, AccessRule] | None = None,
) -> dict[Any, Any]:
    """The ``extras`` a command or group declares its gate with."""
    extras: dict[Any, Any] = {ACCESS_EXTRA: access}
    if options:
        extras[OPTION_ACCESS_EXTRA] = dict(options)
    return extras


def declared_access(command: Any) -> AccessRule | None:
    """The rule gating ``command``, inherited from its nearest declaring group.

    A group can declare one rule for everything under it, which is how the
    generated `/settings` subcommands are covered without each one repeating
    it. ``None`` means nothing on the path declared a rule.
    """
    current = command
    while current is not None:
        access = getattr(current, "extras", {}).get(ACCESS_EXTRA)
        if access is not None:
            return access
        current = getattr(current, "parent", None)
    return None


def declared_option_access(command: Any) -> Mapping[str, AccessRule]:
    """The options of ``command`` gated more strictly than the command."""
    return getattr(command, "extras", {}).get(OPTION_ACCESS_EXTRA, {})


def iter_role_settings(rule: AccessRule) -> list[str]:
    """Every settings field ``rule`` reads a role id from."""
    if isinstance(rule, RoleSetting):
        return [rule.field]
    if isinstance(rule, AnyOf):
        return [field for inner in rule.rules for field in iter_role_settings(inner)]
    return []
