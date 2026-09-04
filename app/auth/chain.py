"""Trying several authentication providers in order.

The chain is what lets one deployment serve a browser session, an SSO redirect,
a gateway header and a bearer token at once. Order matters and is configured:
non-interactive providers go first, because a machine caller presenting a token
should never be redirected to a login page.
"""

from __future__ import annotations

from collections.abc import Sequence

from starlette.requests import Request

from app.auth.base import BaseAuthProvider
from app.auth.session import SessionStore
from app.core.results import ANONYMOUS, Identity


class SessionAuth(BaseAuthProvider):
    """Reads whoever the interactive providers previously signed in.

    A thin wrapper over the cookie: the provider that established the identity
    is recorded on it, so this one does not care which was used.
    """

    name = "session"
    interactive = False

    def __init__(self, sessions: SessionStore) -> None:
        self.sessions = sessions

    async def authenticate(self, request: Request) -> Identity | None:
        return self.sessions.load_identity(request)


class AuthChain:
    """Asks each provider in turn for the identity behind a request."""

    def __init__(self, providers: Sequence[BaseAuthProvider]) -> None:
        self.providers = list(providers)
        self._by_name = {p.name: p for p in self.providers}

    def __iter__(self):
        return iter(self.providers)

    def __len__(self) -> int:
        return len(self.providers)

    def get(self, name: str) -> BaseAuthProvider | None:
        return self._by_name.get(name)

    @property
    def interactive(self) -> tuple[BaseAuthProvider, ...]:
        """Providers offering a sign-in flow, for the login page."""
        return tuple(p for p in self.providers if p.interactive)

    @property
    def password_providers(self) -> tuple[BaseAuthProvider, ...]:
        """Providers that hold passwords of their own."""
        return tuple(p for p in self.providers if p.capabilities.manages_passwords)

    def password_manager(self, identity: Identity) -> BaseAuthProvider | None:
        """The provider that owns this identity's password, if any owns it.

        Matching on the identity's own provider matters: a deployment can run
        password sign-in *and* SSO at once, and the person who arrived through
        SSO has no password here even though the application clearly has a
        password form for other people. Asking "which provider signed this
        person in, and does it hold passwords" is the only answer that stays
        right in a mixed deployment.

        The session provider is a special case -- it reports itself as the
        source, while the identity remembers which interactive provider
        actually established it -- so the lookup goes by ``identity.provider``.
        """
        if not identity.is_authenticated:
            return None
        provider = self._by_name.get(identity.provider)
        if provider is not None and provider.capabilities.manages_passwords:
            return provider
        return None

    def self_service_reset(self) -> BaseAuthProvider | None:
        """The provider handling "forgot password", if one is configured."""
        for provider in self.providers:
            if provider.capabilities.self_service_reset:
                return provider
        return None

    async def authenticate(self, request: Request) -> Identity:
        """The identity behind ``request``, or the anonymous identity.

        A provider returning ``None`` means "nothing of mine here"; the chain
        moves on. A provider *raising* means credentials were presented and
        rejected, which stops the chain -- otherwise a bad token would quietly
        become an anonymous request and be served whatever is public.
        """
        for provider in self.providers:
            identity = await provider.authenticate(request)
            if identity is not None:
                return identity
        return ANONYMOUS

    async def health(self) -> list[tuple[str, bool, str]]:
        results = []
        for provider in self.providers:
            try:
                healthy, detail = await provider.health()
            except Exception as exc:
                healthy, detail = False, f"{type(exc).__name__}: {exc}"
            results.append((provider.name, healthy, detail))
        return results

    def __repr__(self) -> str:
        return f"<AuthChain {' -> '.join(p.name for p in self.providers)}>"
