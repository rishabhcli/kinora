"""The RBAC engine — roles → permissions → an action grant.

This is the plane's role-based engine. It mirrors the *vocabulary* of the
existing :mod:`app.auth.rbac` (the ``resource:action`` permission strings, the
``*`` / ``resource:*`` wildcard semantics) so a subject's roles mean exactly the
same thing in the plane as they do in the legacy interactive check — that is the
whole point of unifying: one role catalogue, one matching rule.

A :class:`RoleCatalogue` maps role names to the permission strings they grant; a
:class:`RbacEngine` resolves the subject's roles to a permission set and asks
whether the requested action is covered. The action a caller passes the SDK is a
``resource:action`` permission string (``book:read``, ``workspace:share``); the
engine matches it against the held set with the same wildcard rules as the
legacy module, so the two never diverge.

The engine **abstains** (rather than denying) when a subject holds no role that
grants the action — abstention lets another engine (ABAC, a relationship grant,
a policy rule) still allow it under deny-overrides. It only emits an explicit
ALLOW when a role positively grants the action.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from app.platform.authz.engine import SyncEngine
from app.platform.authz.model import AuthorizationRequest, EngineResult

WILDCARD = "*"


def permission_matches(held: Iterable[str], required: str) -> bool:
    """Whether ``held`` grants ``required`` (``*`` and ``resource:*`` aware).

    Identical matching to :func:`app.auth.rbac.has_capability` so a permission
    means the same thing in the plane as in the legacy check. Order, most → least
    powerful: ``*`` grants everything; ``resource:*`` grants every action on the
    resource; otherwise exact match.
    """
    held_set = set(held)
    if WILDCARD in held_set:
        return True
    if required in held_set:
        return True
    resource = required.split(":", 1)[0]
    return f"{resource}:{WILDCARD}" in held_set


class RoleCatalogue:
    """A role-name → granted-permissions map with wildcard expansion.

    Built once (from the legacy ``ROLES`` table, or programmatically) and shared
    by the :class:`RbacEngine`. A role may grant a bare ``*`` (the admin
    wildcard) or specific ``resource:action`` / ``resource:*`` permissions.
    """

    def __init__(self, roles: Mapping[str, Iterable[str]]) -> None:
        self._roles: dict[str, frozenset[str]] = {
            name: frozenset(perms) for name, perms in roles.items()
        }

    def permissions_for(self, role: str) -> frozenset[str]:
        """The permission strings granted by ``role`` (empty if unknown)."""
        return self._roles.get(role, frozenset())

    def expand(self, roles: Iterable[str]) -> frozenset[str]:
        """The union of permissions across ``roles``."""
        out: set[str] = set()
        for role in roles:
            out |= self._roles.get(role, frozenset())
        return frozenset(out)

    @property
    def role_names(self) -> frozenset[str]:
        """Every known role name (for coverage / introspection)."""
        return frozenset(self._roles)

    @classmethod
    def from_auth_catalogue(cls) -> RoleCatalogue:
        """Build a catalogue from the legacy :mod:`app.auth.rbac` ``ROLES`` table.

        Imported lazily so the plane's pure core has no import-time dependency on
        the auth package (keeps the model layer free of cycles).
        """
        from app.auth.rbac import ROLES

        return cls(ROLES)


class RbacEngine(SyncEngine):
    """Allow when one of the subject's roles grants the action; else abstain.

    The subject carries its roles in ``attributes['roles']``; the engine may also
    consult a pre-expanded permission set in ``attributes['permissions']`` (an
    API-key principal carries scopes directly there with no role indirection).
    """

    name = "rbac"

    def __init__(self, catalogue: RoleCatalogue) -> None:
        self._catalogue = catalogue

    def evaluate(self, request: AuthorizationRequest) -> EngineResult:
        action = request.action
        subject = request.subject

        # Direct permissions (e.g. an API key's scopes) bypass role expansion.
        direct = subject.attr("permissions")
        held: set[str] = set()
        if direct:
            held |= {str(p) for p in direct}
        held |= self._catalogue.expand(subject.roles)

        if not held:
            return EngineResult.abstain(self.name, "subject holds no roles/permissions")

        if permission_matches(held, action):
            granting = sorted(self._granting(held, action))
            return EngineResult.allow(
                self.name,
                f"permission(s) {granting} grant '{action}'",
                rule="rbac:permission-match",
            )
        return EngineResult.abstain(
            self.name, f"no held permission grants '{action}'"
        )

    @staticmethod
    def _granting(held: Iterable[str], action: str) -> set[str]:
        """The exact held permissions responsible for granting ``action``."""
        resource = action.split(":", 1)[0]
        candidates = {WILDCARD, action, f"{resource}:{WILDCARD}"}
        return {p for p in held if p in candidates}


__all__ = ["RbacEngine", "RoleCatalogue", "permission_matches", "WILDCARD"]
