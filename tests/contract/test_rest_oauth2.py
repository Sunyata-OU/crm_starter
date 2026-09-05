"""OAuth2 client-credentials auth for a REST connection.

Every other auth type is a header computed once when the connection opens.
This one holds a token that expires, so the tests are mostly about lifecycle:
when it is fetched, when it is reused, when it is renewed, and what happens
when several requests want it at once on a cold connection.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.core.errors import ConfigError, ProviderError
from app.providers.rest import OAuth2ClientCredentials

TOKEN_URL = "https://issuer.test/oauth/token"


class Issuer:
    """A token endpoint that counts what it is asked for."""

    def __init__(self, expires_in: int = 3600, status: int = 200, body: dict | None = None):
        self.requests: list[httpx.Request] = []
        self.expires_in = expires_in
        self.status = status
        self.body = body
        self.serial = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status >= 400:
            return httpx.Response(self.status, json={"error": "invalid_client"})
        if self.body is not None:
            return httpx.Response(200, json=self.body)
        self.serial += 1
        return httpx.Response(
            200, json={"access_token": f"tok-{self.serial}", "expires_in": self.expires_in}
        )

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def form(self, index: int = 0) -> dict[str, str]:
        from urllib.parse import parse_qsl

        return dict(parse_qsl(self.requests[index].content.decode()))


def make(issuer: Issuer, **kw) -> OAuth2ClientCredentials:
    return OAuth2ClientCredentials(
        token_url=TOKEN_URL, client_id="cid", client_secret="secret",
        transport=issuer.transport, **kw,
    )


class TestFetchingAToken:
    @pytest.mark.asyncio
    async def test_the_bearer_is_the_issued_token(self):
        issuer = Issuer()
        assert await make(issuer)._bearer() == "tok-1"

    @pytest.mark.asyncio
    async def test_the_grant_type_is_client_credentials(self):
        issuer = Issuer()
        await make(issuer)._bearer()
        assert issuer.form()["grant_type"] == "client_credentials"

    @pytest.mark.asyncio
    async def test_credentials_go_in_the_basic_header_by_default(self):
        issuer = Issuer()
        await make(issuer)._bearer()
        assert issuer.requests[0].headers["authorization"].startswith("Basic ")
        assert "client_secret" not in issuer.form()

    @pytest.mark.asyncio
    async def test_credentials_can_go_in_the_body_instead(self):
        issuer = Issuer()
        await make(issuer, client_auth="body")._bearer()
        assert issuer.form()["client_secret"] == "secret"
        assert "authorization" not in issuer.requests[0].headers

    @pytest.mark.asyncio
    async def test_a_scope_is_sent_when_given(self):
        issuer = Issuer()
        await make(issuer, scope="read:things")._bearer()
        assert issuer.form()["scope"] == "read:things"

    @pytest.mark.asyncio
    async def test_extra_parameters_ride_along(self):
        # Auth0 wants an `audience`; nothing else does. Rather than special-case
        # one issuer, anything unrecognised is passed through.
        issuer = Issuer()
        await make(issuer, extra={"audience": "https://api.test"})._bearer()
        assert issuer.form()["audience"] == "https://api.test"


class TestReuseAndRenewal:
    @pytest.mark.asyncio
    async def test_a_live_token_is_not_refetched(self):
        issuer = Issuer(expires_in=3600)
        auth = make(issuer)
        assert await auth._bearer() == await auth._bearer() == "tok-1"
        assert len(issuer.requests) == 1

    @pytest.mark.asyncio
    async def test_a_token_is_renewed_before_it_expires(self):
        # Renewed early by `leeway`, so it is never presented in the instant it
        # lapses and a little clock skew is absorbed.
        issuer = Issuer(expires_in=30)
        auth = make(issuer, leeway=30.0)
        assert await auth._bearer() == "tok-1"
        assert await auth._bearer() == "tok-2", "a token at its leeway is already stale"

    @pytest.mark.asyncio
    async def test_a_cold_burst_fetches_one_token_between_them(self):
        # Without the lock each concurrent request would open its own token
        # request, which some issuers rate-limit and all of them bill for.
        issuer = Issuer()
        auth = make(issuer)
        tokens = await asyncio.gather(*(auth._bearer() for _ in range(8)))
        assert set(tokens) == {"tok-1"}
        assert len(issuer.requests) == 1

    @pytest.mark.asyncio
    async def test_a_missing_expiry_is_treated_as_an_hour(self):
        issuer = Issuer(body={"access_token": "tok-x"})
        auth = make(issuer)
        await auth._bearer()
        assert auth._expires_at > 0 and auth._valid


class TestWhenTheIssuerRefuses:
    @pytest.mark.asyncio
    async def test_an_error_response_says_what_the_issuer_said(self):
        auth = make(Issuer(status=401))
        with pytest.raises(ProviderError, match="invalid_client"):
            await auth._bearer()

    @pytest.mark.asyncio
    async def test_a_response_without_a_token_is_an_error(self):
        auth = make(Issuer(body={"token_type": "bearer"}))
        with pytest.raises(ProviderError, match="no access_token"):
            await auth._bearer()

    @pytest.mark.asyncio
    async def test_a_non_json_body_is_an_error(self):
        def handler(request):
            return httpx.Response(200, text="<html>nope</html>")

        auth = OAuth2ClientCredentials(
            token_url=TOKEN_URL, client_id="cid", transport=httpx.MockTransport(handler)
        )
        with pytest.raises(ProviderError, match="did not return JSON"):
            await auth._bearer()


class TestConfiguration:
    def test_a_token_url_is_required(self):
        with pytest.raises(ConfigError):
            OAuth2ClientCredentials(token_url="", client_id="cid")

    def test_a_client_id_is_required(self):
        with pytest.raises(ConfigError):
            OAuth2ClientCredentials(token_url=TOKEN_URL, client_id="")

    def test_an_unknown_client_auth_style_is_refused(self):
        with pytest.raises(ConfigError, match="basic"):
            OAuth2ClientCredentials(token_url=TOKEN_URL, client_id="c", client_auth="wat")


class TestOnARequest:
    """The auth flow as httpx drives it, rather than the token cache alone."""

    def api(self, issuer: Issuer, responses: list[int]):
        """An API that answers with `responses` in order, recording bearers."""
        seen: list[str] = []
        remaining = list(responses)

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == TOKEN_URL:
                return issuer.handler(request)
            seen.append(request.headers.get("authorization", ""))
            return httpx.Response(remaining.pop(0) if remaining else 200, json={})

        return handler, seen

    @pytest.mark.asyncio
    async def test_the_bearer_is_attached_to_the_request(self):
        issuer = Issuer()
        handler, seen = self.api(issuer, [200])
        auth = OAuth2ClientCredentials(
            token_url=TOKEN_URL, client_id="cid", transport=httpx.MockTransport(handler)
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=auth) as c:
            await c.get("https://api.test/things")
        assert seen == ["Bearer tok-1"]

    @pytest.mark.asyncio
    async def test_a_401_renews_the_token_and_retries_once(self):
        # A token refused before it looked expired -- revoked, or the issuer
        # disagrees about the clock.
        issuer = Issuer()
        handler, seen = self.api(issuer, [401, 200])
        auth = OAuth2ClientCredentials(
            token_url=TOKEN_URL, client_id="cid", transport=httpx.MockTransport(handler)
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=auth) as c:
            response = await c.get("https://api.test/things")
        assert response.status_code == 200
        assert seen == ["Bearer tok-1", "Bearer tok-2"]

    @pytest.mark.asyncio
    async def test_a_persistent_401_is_not_retried_forever(self):
        # A genuinely unauthorised client would otherwise loop against the
        # token endpoint.
        issuer = Issuer()
        handler, seen = self.api(issuer, [401, 401, 401])
        auth = OAuth2ClientCredentials(
            token_url=TOKEN_URL, client_id="cid", transport=httpx.MockTransport(handler)
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=auth) as c:
            response = await c.get("https://api.test/things")
        assert response.status_code == 401
        assert len(seen) == 2, "one retry, not a loop"


class TestConnectionWiring:
    @pytest.mark.asyncio
    async def test_an_oauth2_connection_gets_the_auth_flow(self):
        from app.core.connections import ConnectionSpec
        from app.providers.rest import open_rest

        spec = ConnectionSpec(
            name="api", type="rest",
            options={
                "base_url": "https://api.test",
                "auth": {
                    "type": "oauth2", "token_url": TOKEN_URL,
                    "client_id": "cid", "client_secret": "s", "scope": "read",
                },
            },
        )
        conn = await open_rest(spec)
        try:
            assert isinstance(conn.client.auth, OAuth2ClientCredentials)
            assert conn.client.auth.scope == "read"
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_a_misspelled_auth_type_is_refused_at_open_time(self):
        # Falling through would build an unauthenticated client and surface as
        # a 401 from the API, which reads like bad credentials rather than a
        # typo in the config.
        from app.core.connections import ConnectionSpec
        from app.providers.rest import open_rest

        spec = ConnectionSpec(
            name="api", type="rest",
            options={"base_url": "https://api.test", "auth": {"type": "oauth"}},
        )
        with pytest.raises(ConfigError, match="unknown auth type"):
            await open_rest(spec)

    @pytest.mark.asyncio
    async def test_a_connection_with_no_auth_still_opens(self):
        from app.core.connections import ConnectionSpec
        from app.providers.rest import open_rest

        spec = ConnectionSpec(name="api", type="rest", options={"base_url": "https://api.test"})
        conn = await open_rest(spec)
        try:
            assert conn.client.auth is None
        finally:
            await conn.close()
