"""The authentication contract.

Every way of establishing who is calling -- a password form, an SSO redirect, a
trusted header, a bearer token -- implements this one interface, so the rest of
the application only ever sees an :class:`~app.core.results.Identity`.

Providers fall into two shapes. Non-interactive ones (tokens, proxy headers)
read the request and answer immediately. Interactive ones (passwords, SSO) also
participate in a login flow, which is what the three optional methods cover.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Protocol, runtime_checkable

from starlette.requests import Request
from starlette.responses import Response

from app.core.results import Identity


@dataclass(frozen=True, slots=True)
class AuthCapabilities:
    """What a provider can do beyond establishing identity.

    The same idea as a data provider's capabilities, for the same reason: the
    UI has to be written once and work whichever provider signed the user in.
    A deployment behind SSO has no password for the application to change, and
    offering a "change password" form there is worse than not offering one --
    it implies the change will have an effect somewhere.

    So the account screens ask the provider rather than assuming. A provider
    that leaves these all false is simply one where the credential lives
    somewhere else, which is the normal case for OIDC, a gateway header, and
    anything backed by a directory.
    """

    #: This provider owns the credential and can verify a current password.
    manages_passwords: bool = False
    #: A signed-in user can change their own password here.
    change: bool = False
    #: An administrator can set someone else's password here.
    admin_reset: bool = False
    #: A forgotten-password link can be issued and redeemed here.
    self_service_reset: bool = False
    #: Repeated failures lock the account rather than being merely refused.
    lockout: bool = False

    def replace(self, **changes: Any) -> AuthCapabilities:
        return replace(self, **changes)


#: The common case: the credential lives elsewhere.
NO_PASSWORDS = AuthCapabilities()


@runtime_checkable
class AuthProvider(Protocol):
    """Establishes the identity behind a request."""

    name: str
    #: Whether this provider has a login flow the UI should offer.
    interactive: bool
    #: What it can do about passwords, if anything.
    capabilities: AuthCapabilities

    async def authenticate(self, request: Request) -> Identity | None:
        """The identity this request carries, or ``None`` to defer to the next
        provider in the chain."""
        ...


class BaseAuthProvider:
    """Defaults for the parts of the flow a provider does not use."""

    name: str = "auth"
    interactive: bool = False
    #: Shown on the sign-in page when several providers are offered.
    label: str = ""
    icon: str = ""
    capabilities: AuthCapabilities = NO_PASSWORDS
    #: Shown where a password form would be, when there is no password to
    #: change here. Answers "then where?", which is the user's next question.
    password_note: str = "Your password is managed by your identity provider."

    async def authenticate(self, request: Request) -> Identity | None:
        return None

    async def login(self, request: Request, credentials: dict[str, Any]) -> Identity | None:
        """Verify submitted credentials. Interactive providers override this."""
        return None

    async def login_url(self, request: Request, next_url: str = "") -> str | None:
        """Where to send the browser to start a redirect-based flow."""
        return None

    async def callback(self, request: Request) -> Identity | None:
        """Complete a redirect-based flow from the provider's response."""
        return None

    async def logout(self, request: Request, response: Response) -> str | None:
        """Clean up, optionally returning a URL to redirect to afterwards."""
        return None

    # -- passwords ----------------------------------------------------------
    #
    # Only meaningful for a provider whose capabilities admit to them. The
    # defaults refuse rather than pretend, so a mis-wired UI fails loudly
    # instead of silently doing nothing.

    async def change_password(
        self, identity: Identity, current: str, new: str
    ) -> None:
        """Change a signed-in user's own password, verifying the current one."""
        raise PasswordsNotManaged(self.password_note, provider=self.name)

    async def set_password(self, subject: str, new: str) -> None:
        """Set a password without knowing the old one.

        For an administrator resetting someone's access, and for redeeming a
        reset link. Both have established the right to do it by other means.
        """
        raise PasswordsNotManaged(self.password_note, provider=self.name)

    async def issue_reset_token(self, username: str) -> tuple[str, Any] | None:
        """A single-use reset link for an address.

        ``None`` both when there is no such account and when this provider
        does not do resets. The caller must not distinguish the two out loud:
        whether an address has an account is exactly what someone probing a
        forgotten-password form wants to learn.
        """
        return None

    async def redeem_reset_token(self, token: str, new: str) -> Any | None:
        """Set a password from a reset link. ``None`` if the link is not usable."""
        return None

    async def health(self) -> tuple[bool, str]:
        return True, "no health check implemented"

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}>"


class PasswordsNotManaged(Exception):
    """This provider does not hold the credential being asked about."""

    def __init__(self, message: str, *, provider: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider


class AuthError(Exception):
    """Credentials were supplied and rejected.

    Distinct from returning ``None``, which means "no credentials here, ask the
    next provider". Raising stops the chain, because a wrong password should not
    silently fall through to anonymous access.
    """

    def __init__(self, message: str = "Sign-in failed.", *, provider: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
