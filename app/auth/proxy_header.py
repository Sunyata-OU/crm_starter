"""Trusting an upstream gateway's identity headers.

Common for internal deployments sitting behind oauth2-proxy, Cloudflare Access
or an authenticating nginx. The gateway has already established who the user
is; this provider reads its verdict.

The danger is obvious: if the application is reachable without going through the
gateway, anyone can set the header and become anyone. So the provider refuses to
do anything unless a list of trusted networks is configured, and checks the peer
against it on every request.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence

from starlette.requests import Request

from app.auth.base import BaseAuthProvider
from app.core.results import Identity


class ProxyHeaderAuth(BaseAuthProvider):
    """Accepts identity headers, but only from a trusted peer."""

    name = "proxy_header"
    interactive = False

    def __init__(
        self,
        *,
        trusted_ips: Sequence[str] = (),
        user_header: str = "X-Forwarded-User",
        email_header: str = "X-Forwarded-Email",
        name_header: str = "X-Forwarded-Preferred-Username",
        roles_header: str = "X-Forwarded-Groups",
        default_roles: Sequence[str] = ("user",),
    ) -> None:
        self.networks = _parse_networks(trusted_ips)
        self.user_header = user_header
        self.email_header = email_header
        self.name_header = name_header
        self.roles_header = roles_header
        self.default_roles = frozenset(default_roles)

    @property
    def enabled(self) -> bool:
        """Disabled entirely when no trusted network is configured.

        Failing closed matters more than convenience here: an empty allowlist
        with headers honoured would be a total authentication bypass.
        """
        return bool(self.networks)

    def trusts(self, request: Request) -> bool:
        client = request.client
        if client is None:
            return False
        try:
            peer = ipaddress.ip_address(client.host)
        except ValueError:
            return False
        return any(peer in network for network in self.networks)

    async def authenticate(self, request: Request) -> Identity | None:
        if not self.enabled or not self.trusts(request):
            return None
        subject = request.headers.get(self.user_header, "").strip()
        email = request.headers.get(self.email_header, "").strip()
        if not subject and not email:
            return None

        roles = [
            part.strip()
            for part in request.headers.get(self.roles_header, "").split(",")
            if part.strip()
        ]
        return Identity(
            subject=subject or email,
            email=email,
            display_name=request.headers.get(self.name_header, "").strip() or email or subject,
            roles=frozenset(roles) or self.default_roles,
            provider=self.name,
        )

    async def health(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "no trusted networks configured; provider is disabled"
        return True, f"trusting {', '.join(str(n) for n in self.networks)}"


def _parse_networks(values: Sequence[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Accept both bare addresses and CIDR ranges."""
    networks = []
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        try:
            networks.append(ipaddress.ip_network(text, strict=False))
        except ValueError:
            continue
    return tuple(networks)
