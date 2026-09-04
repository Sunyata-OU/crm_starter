"""Authentication.

The chain itself is built per application and lives on ``app.state``. What is
here is a process-wide handle to it, for the few places that need to ask about
authentication but never see a request: a resource action, a CLI command, a
background sweep.

The same arrangement as :data:`app.notify.notifier` and
:data:`app.resources.rbac.store`, and for the same reason -- passing the chain
down through every declaration would put a dependency on authentication into
code that has nothing to do with it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from app.auth.chain import AuthChain


class CurrentAuth:
    """A holder for the running application's auth chain."""

    def __init__(self) -> None:
        self.chain: AuthChain | None = None

    def bind(self, chain: AuthChain) -> None:
        self.chain = chain

    def password_provider(self, name: str = ""):
        """The provider holding passwords, optionally by name.

        Returns None when none does, which is the correct answer for a
        deployment behind SSO -- and the reason every caller has to handle it.
        """
        if self.chain is None:
            return None
        if name:
            provider = self.chain.get(name)
            return provider if provider and provider.capabilities.manages_passwords else None
        providers = self.chain.password_providers
        return providers[0] if providers else None


#: Bound during startup, once the chain is assembled.
current = CurrentAuth()
