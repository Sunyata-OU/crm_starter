"""Acting as the person who is signed in, rather than as a service account.

A CRM whose backing store is somebody else's API has an attribution problem:
every write it makes looks like the same robot. Forwarding the caller's own
OIDC access token fixes that, and costs something -- the token has to be kept
somewhere for the life of the session -- so it is off unless asked for.

These tests pin the three things that are easy to get wrong: the token is not
kept unless the setting says so, a caller without one still works, and an
oversized cookie loses the token rather than the session.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import Response

from app.auth.session import TOKEN_KEY, SessionStore
from app.core.results import Ctx, Identity
from app.providers.rest import RestConnection, RestMapping, RestProvider

SECRET = "test-key-not-for-real-use"


def store() -> SessionStore:
    return SessionStore(SECRET)


def request_with(cookie_value: str) -> Request:
    scope = {
        "type": "http",
        "headers": [(b"cookie", f"crm_session={cookie_value}".encode())],
        "method": "GET",
        "path": "/",
    }
    return Request(scope)


def saved_cookie(response: Response) -> str:
    return response.headers["set-cookie"].split(";")[0].partition("=")[2]


class TestKeepingTheToken:
    def test_a_session_carries_no_token_by_default(self):
        response = Response()
        store().save_identity(response, Identity(subject="u", email="u@x"))
        assert store().access_token(request_with(saved_cookie(response))) == ""

    def test_a_token_passed_in_is_stored_and_read_back(self):
        response = Response()
        store().save_identity(
            response, Identity(subject="u", email="u@x"), **{TOKEN_KEY: "abc.def.ghi"}
        )
        assert store().access_token(request_with(saved_cookie(response))) == "abc.def.ghi"

    def test_an_oversized_cookie_drops_the_token_not_the_session(self, caplog):
        """A browser discards a >4KB cookie silently, and the next request is
        signed out. Losing attribution beats losing the session.

        The token here is random rather than repeated: itsdangerous compresses
        the payload when that helps, so 5 KB of the same character would fit
        comfortably and prove nothing. A real JWT is base64 and barely
        compresses, which is what this stands in for.
        """
        import secrets

        response = Response()
        store().save_identity(
            response, Identity(subject="u", email="u@x"),
            **{TOKEN_KEY: secrets.token_urlsafe(4096)},
        )
        request = request_with(saved_cookie(response))
        assert store().access_token(request) == ""
        # The session itself survived.
        identity = store().load_identity(request)
        assert identity is not None and identity.subject == "u"
        assert any("access token" in r.getMessage() for r in caplog.records)


class TestSendingIt:
    """What the REST provider does with what it is given."""

    def provider(self, *, caller_token: bool, seen: list) -> RestProvider:
        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("authorization"))
            return httpx.Response(200, json={"id": "1"})

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.example",
            headers={"Authorization": "Bearer service-account"},
        )
        connection = RestConnection(client, base_url="https://api.example",
                                    caller_token=caller_token)
        return RestProvider(
            connection, RestMapping(path="/things"), name="rest:things", read_only=False
        )

    def ctx(self, token: str | None) -> Ctx:
        extra = {"ip": "127.0.0.1"}
        if token:
            extra["access_token"] = token
        return Ctx(identity=Identity(subject="u"), request_id="r", extra=extra)

    async def test_the_callers_token_is_sent_when_there_is_one(self):
        seen: list = []
        provider = self.provider(caller_token=True, seen=seen)
        await provider.update("1", {"name": "x"}, self.ctx("caller-token"))
        assert seen == ["Bearer caller-token"]

    async def test_without_a_token_the_connections_own_credentials_are_used(self):
        """The path a background job and the CLI take."""
        seen: list = []
        provider = self.provider(caller_token=True, seen=seen)
        await provider.update("1", {"name": "x"}, self.ctx(None))
        assert seen == ["Bearer service-account"]

    async def test_a_connection_that_did_not_ask_never_forwards_it(self):
        seen: list = []
        provider = self.provider(caller_token=False, seen=seen)
        await provider.update("1", {"name": "x"}, self.ctx("caller-token"))
        assert seen == ["Bearer service-account"]

    async def test_reads_forward_it_too(self):
        seen: list = []
        provider = self.provider(caller_token=True, seen=seen)
        await provider.get("1", self.ctx("caller-token"))
        assert seen == ["Bearer caller-token"]


class TestConfiguringIt:
    async def test_a_fallback_block_is_applied_to_the_shared_client(self):
        from app.core.connections import ConnectionSpec
        from app.providers.rest import open_rest

        spec = ConnectionSpec(
            name="api.x",
            type="rest",
            options={
                "base_url": "https://api.example",
                "auth": {
                    "type": "caller_token",
                    "fallback": {"type": "bearer", "token": "service-account"},
                },
            },
        )
        connection = await open_rest(spec)
        assert connection.caller_token
        assert connection.client.headers["authorization"] == "Bearer service-account"
        await connection.close()

    async def test_an_unknown_type_names_the_known_ones(self):
        from app.core.connections import ConnectionSpec
        from app.core.errors import ConfigError
        from app.providers.rest import open_rest

        spec = ConnectionSpec(
            name="api.x", type="rest",
            options={"base_url": "https://api.example", "auth": {"type": "magic"}},
        )
        with pytest.raises(ConfigError, match="caller_token"):
            await open_rest(spec)
