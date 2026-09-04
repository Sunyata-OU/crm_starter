"""Exception hierarchy, and how each maps onto an HTTP status.

Providers raise these instead of backend-specific exceptions so the web layer
can render one consistent error page regardless of which backend failed.
"""

from __future__ import annotations

from typing import Any


class CRMError(Exception):
    """Base class for everything this application raises deliberately."""

    status_code: int = 500
    #: Shown to the end user. Subclasses that may carry backend detail keep this
    #: generic so internals are not leaked into the response.
    public_message: str = "Something went wrong."

    def __init__(self, message: str = "", **context: Any) -> None:
        super().__init__(message or self.public_message)
        self.message = message or self.public_message
        self.context = context

    def __str__(self) -> str:
        return self.message


class ConfigError(CRMError):
    """Wiring is wrong: a missing connection, an unknown provider type."""

    status_code = 500
    public_message = "The application is misconfigured."


class RegistryError(ConfigError):
    """A resource, field type, view or module was requested but not registered."""


class ProviderError(CRMError):
    """A backend failed. Wraps the original exception in ``context['cause']``."""

    status_code = 502
    public_message = "A backend service failed to respond."


class UnsupportedOperation(ProviderError):
    """The provider genuinely cannot do this, and the shim cannot emulate it."""

    status_code = 501
    public_message = "That operation is not supported by this data source."


class TooManyRows(ProviderError):
    """The shim would have had to pull more rows than its guard allows.

    Raised loudly rather than truncating, so a silently-wrong result set can
    never reach the screen.
    """

    status_code = 507
    public_message = (
        "This data source cannot narrow the results itself, and too many rows "
        "would need loading. Add a narrower filter."
    )


class NotFound(CRMError):
    status_code = 404
    public_message = "Not found."


class ValidationFailed(CRMError):
    """Submitted data failed validation. ``errors`` maps field name to message."""

    status_code = 422
    public_message = "Please correct the highlighted fields."

    def __init__(
        self, errors: dict[str, str] | None = None, message: str = "", **context: Any
    ) -> None:
        super().__init__(message, **context)
        self.errors = errors or {}


class AuthenticationRequired(CRMError):
    """No usable identity. The web layer turns this into a redirect to login."""

    status_code = 401
    public_message = "Please sign in to continue."


class PermissionDenied(CRMError):
    status_code = 403
    public_message = "You do not have permission to do that."


class MustChangePassword(CRMError):
    """The caller is signed in on a credential they have to replace first.

    Not an authentication failure: they are who they say they are. It is a
    refusal to do anything else until a temporary password -- one an
    administrator generated, and therefore one somebody else has seen -- has
    been replaced by one only they know.
    """

    status_code = 403
    public_message = "Choose a new password before continuing."


class ConflictError(CRMError):
    """Concurrent modification, or a uniqueness constraint."""

    status_code = 409
    public_message = "That change conflicts with the current state of the record."
