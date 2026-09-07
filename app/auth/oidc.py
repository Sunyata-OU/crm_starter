"""Single sign-on over OpenID Connect.

Uses the authorization-code flow with PKCE against any compliant issuer --
Google, Entra, Keycloak, Auth0 -- configured by discovery URL rather than by
hand-written endpoints.

The transient values of a login (state, nonce, PKCE verifier) go into a short
signed cookie rather than server memory, so the flow survives a restart and
works across multiple workers without shared state.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import Mapping, Sequence
from typing import Any

import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.requests import Request
from starlette.responses import Response

from app.auth.base import AuthError, BaseAuthProvider
from app.core.errors import ConfigError
from app.core.results import Identity

FLOW_COOKIE = "crm_oidc_flow"
FLOW_MAX_AGE = 600


def _claim(claims: Mapping[str, Any], path: str) -> Any:
    """Read a claim addressed by a dotted path.

    Not every issuer puts roles at the top level. Keycloak -- named in this
    module's own docstring -- nests realm roles under ``realm_access.roles``
    and client roles under ``resource_access.<client>.roles``, so a flat
    ``claims.get(path)`` returns nothing and every user silently falls back to
    ``default_roles``: an administrator signs in successfully and arrives with
    the rights of a stranger, with no error anywhere to explain it.

    A path with no dots is looked up as a plain key, so a claim whose name
    genuinely contains a dot keeps working.
    """
    if path in claims:
        return claims[path]

    current: Any = claims
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return None
        current = current[segment]
    return current


class OIDCAuth(BaseAuthProvider):
    """Redirect-based sign-in against an OIDC issuer."""

    name = "oidc"
    interactive = True

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str = "",
        secret_key: str,
        scopes: str = "openid email profile",
        redirect_path: str = "/auth/callback",
        roles_claim: str = "roles",
        role_map: dict[str, str] | None = None,
        default_roles: Sequence[str] = ("user",),
        label: str = "Sign in with SSO",
        timeout: float = 10.0,
    ) -> None:
        if not issuer or not client_id:
            raise ConfigError("the OIDC provider needs an issuer and a client id")
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = scopes
        self.redirect_path = redirect_path
        self.roles_claim = roles_claim
        self.role_map = role_map or {}
        self.default_roles = frozenset(default_roles)
        self.label = label
        self.timeout = timeout
        self._serializer = URLSafeTimedSerializer(secret_key, salt="crm.oidc.flow")
        self._metadata: dict[str, Any] | None = None

    # -- discovery ----------------------------------------------------------

    async def metadata(self) -> dict[str, Any]:
        """The issuer's published endpoints, fetched once and cached."""
        if self._metadata is not None:
            return self._metadata
        url = f"{self.issuer}/.well-known/openid-configuration"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise ConfigError(f"could not read OIDC discovery from {url}: {exc}") from exc
        self._metadata = response.json()
        return self._metadata

    # -- starting the flow --------------------------------------------------

    async def login_url(self, request: Request, next_url: str = "") -> str:
        meta = await self.metadata()
        verifier = secrets.token_urlsafe(64)
        challenge = _pkce_challenge(verifier)
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)

        request.state.oidc_flow = self._serializer.dumps(
            {"state": state, "nonce": nonce, "verifier": verifier, "next": next_url}
        )

        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": str(request.url_for("auth_callback")),
            "scope": self.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return str(httpx.URL(meta["authorization_endpoint"], params=params))

    def attach_flow(self, request: Request, response: Response) -> None:
        """Persist the flow secrets set by :meth:`login_url` onto the response."""
        payload = getattr(request.state, "oidc_flow", None)
        if payload:
            response.set_cookie(
                FLOW_COOKIE, payload, max_age=FLOW_MAX_AGE, httponly=True,
                samesite="lax", path="/",
            )

    # -- completing the flow ------------------------------------------------

    async def callback(self, request: Request) -> Identity:
        if error := request.query_params.get("error"):
            raise AuthError(
                f"The identity provider refused the sign-in ({error}).", provider=self.name
            )

        flow = self._read_flow(request)
        if request.query_params.get("state") != flow.get("state"):
            # A mismatched state means the response did not come from the
            # request we started: a cross-site request forgery attempt.
            raise AuthError("The sign-in response did not match the request.", provider=self.name)

        code = request.query_params.get("code")
        if not code:
            raise AuthError("The identity provider returned no authorization code.", provider=self.name)

        meta = await self.metadata()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": str(request.url_for("auth_callback")),
            "client_id": self.client_id,
            "code_verifier": flow["verifier"],
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                token_response = await client.post(meta["token_endpoint"], data=data)
                token_response.raise_for_status()
            except httpx.HTTPError as exc:
                raise AuthError(f"Could not exchange the sign-in code: {exc}", provider=self.name) from exc
            tokens = token_response.json()

            claims = _decode_id_token(tokens.get("id_token", ""))
            if not claims and (userinfo := meta.get("userinfo_endpoint")):
                # Some issuers return an opaque id_token; fall back to userinfo.
                info = await client.get(
                    userinfo, headers={"Authorization": f"Bearer {tokens['access_token']}"}
                )
                info.raise_for_status()
                claims = info.json()

        claims = self._with_roles_from_access_token(claims, tokens.get("access_token", ""))

        if flow.get("nonce") and claims.get("nonce") not in (None, flow["nonce"]):
            raise AuthError("The sign-in response was replayed.", provider=self.name)

        return self.to_identity(claims)

    def _read_flow(self, request: Request) -> dict[str, Any]:
        raw = request.cookies.get(FLOW_COOKIE)
        if not raw:
            raise AuthError(
                "The sign-in took too long or the browser blocked a cookie. Try again.",
                provider=self.name,
            )
        try:
            return self._serializer.loads(raw, max_age=FLOW_MAX_AGE)
        except BadSignature:
            raise AuthError("The sign-in request could not be verified.", provider=self.name) from None

    def next_url(self, request: Request) -> str:
        try:
            return self._read_flow(request).get("next", "") or "/"
        except AuthError:
            return "/"

    def clear_flow(self, response: Response) -> None:
        response.delete_cookie(FLOW_COOKIE, path="/")

    # -- claims -------------------------------------------------------------

    def to_identity(self, claims: dict[str, Any]) -> Identity:
        subject = str(claims.get("sub") or claims.get("email") or "")
        if not subject:
            raise AuthError("The identity provider returned no subject.", provider=self.name)
        return Identity(
            subject=subject,
            email=str(claims.get("email", "")),
            display_name=str(claims.get("name") or claims.get("preferred_username") or ""),
            roles=self.extract_roles(claims),
            claims=claims,
            provider=self.name,
            # A standard OIDC claim, so SSO users get their zone for free.
            timezone=str(claims.get("zoneinfo") or "UTC"),
            locale=str(claims.get("locale") or "en"),
        )

    def _with_roles_from_access_token(
        self, claims: dict[str, Any], access_token: str
    ) -> dict[str, Any]:
        """Borrow the roles claim from the access token when the id token lacks it.

        Keycloak puts roles in the access token and, by default, nowhere else:
        the built-in role mappers ship with `access.token.claim` on and
        `id.token.claim` off. An API never notices, because a bearer token is
        all an API is given. A browser client reading the id token finds no
        roles at all and signs everybody in with none -- which presents as
        "SSO works but nobody can see anything", the failure this exists to
        prevent, and which the default configuration of the most likely issuer
        walks straight into.

        Only the configured roles claim is taken, and only when the id token
        does not carry it, so an issuer that does the standard thing is
        unaffected. Reading it unverified is no weaker than reading the id
        token the same way: both came from the token endpoint over TLS in
        response to a code we generated, and neither was supplied by a client.
        """
        if _claim(claims, self.roles_claim) is not None:
            return claims
        head, _, _ = self.roles_claim.partition(".")
        borrowed = _decode_id_token(access_token).get(head)
        if borrowed is None:
            return claims
        return {**claims, head: borrowed}

    def extract_roles(self, claims: dict[str, Any]) -> frozenset[str]:
        """Map issuer groups onto application roles.

        Without a ``role_map`` the claim values are used directly; with one,
        only mapped values grant a role, which keeps an unexpected group in the
        directory from silently granting access here.
        """
        raw = _claim(claims, self.roles_claim)
        if raw is None:
            return self.default_roles
        values = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
        values = [str(v).strip() for v in values if str(v).strip()]
        if not self.role_map:
            return frozenset(values) or self.default_roles
        mapped = {self.role_map[v] for v in values if v in self.role_map}
        return frozenset(mapped) or self.default_roles

    async def health(self) -> tuple[bool, str]:
        try:
            meta = await self.metadata()
            return True, f"discovery ok: {meta.get('issuer', self.issuer)}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _decode_id_token(token: str) -> dict[str, Any]:
    """Read the claims out of a JWT without verifying its signature.

    Safe here only because the token came directly from the issuer's token
    endpoint over TLS, in response to a code we generated. It is never used on
    a token supplied by a client.
    """
    if not token or token.count(".") != 2:
        return {}
    import json

    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return {}


class DevAuth(BaseAuthProvider):
    """Signs everyone in as a fixed developer account.

    For local work only. The application refuses to start with this enabled in
    a production environment.
    """

    name = "dev"
    interactive = False

    def __init__(self, *, subject: str = "dev", roles: Sequence[str] = ("admin",)) -> None:
        self.subject = subject
        self.roles = frozenset(roles)

    async def authenticate(self, request: Request) -> Identity:
        return Identity(
            subject=self.subject,
            email=f"{self.subject}@localhost",
            display_name="Developer",
            roles=self.roles,
            provider=self.name,
        )
