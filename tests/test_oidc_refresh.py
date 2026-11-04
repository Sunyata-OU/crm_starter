"""Renewing an OIDC session with its refresh token.

Pins the behaviours that matter: nothing happens unless the setting is on, a
session near expiry is renewed before the route sees it, a refusal at the
issuer signs the person out, and an issuer outage does not.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from app.auth.base import AuthError
from app.auth.oidc import OIDCAuth
from app.auth.session import EXPIRES_KEY, REFRESH_KEY, TOKEN_KEY, SessionStore
from app.core.errors import ConfigError
from app.core.results import Identity
from app.web.middleware import SessionRefreshMiddleware

SECRET = "test-key-not-for-real-use"


class FakeOIDC(OIDCAuth):
    def __init__(self, outcome):
        super().__init__(issuer="https://idp", client_id="c", secret_key=SECRET)
        self.outcome = outcome
        self.calls: list[str] = []

    async def refresh(self, refresh_token):
        self.calls.append(refresh_token)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def build(outcome, *, enabled=True, keep_access=True):
    sessions = SessionStore(SECRET)
    provider = FakeOIDC(outcome)
    crm = SimpleNamespace(
        settings=SimpleNamespace(
            oidc_refresh_tokens=enabled,
            oidc_refresh_leeway=60,
            oidc_keep_access_token=keep_access,
        ),
        sessions=sessions,
        auth=SimpleNamespace(get=lambda name: provider if name == "oidc" else None),
    )

    async def whoami(request: Request) -> Response:
        data = sessions.read(request)
        return JSONResponse({"sub": data.get("sub"), "at": data.get(TOKEN_KEY)})

    app = Starlette(routes=[Route("/", whoami)])
    app.state.crm = crm
    app.add_middleware(SessionRefreshMiddleware)
    return app, sessions, provider


def login_cookie(sessions: SessionStore, *, expires_in: int) -> str:
    response = Response()
    sessions.save_identity(
        response,
        Identity(subject="u", email="u@x"),
        **{
            TOKEN_KEY: "old-at",
            REFRESH_KEY: "old-rt",
            EXPIRES_KEY: int(time.time()) + expires_in,
        },
    )
    return response.headers["set-cookie"].split(";")[0].partition("=")[2]


def client_with(app, cookie: str) -> TestClient:
    client = TestClient(app)
    client.cookies.set("crm_session", cookie)
    return client


def test_disabled_does_nothing():
    app, sessions, provider = build({}, enabled=False)
    r = client_with(app, login_cookie(sessions, expires_in=1)).get("/")
    assert r.json()["at"] == "old-at" and provider.calls == []


def test_fresh_token_is_left_alone():
    app, sessions, provider = build({})
    client_with(app, login_cookie(sessions, expires_in=3600)).get("/")
    assert provider.calls == []


def test_expiring_session_is_renewed_before_the_route_runs():
    app, sessions, provider = build(
        {"access_token": "new-at", "refresh_token": "new-rt", "expires_in": 300}
    )
    r = client_with(app, login_cookie(sessions, expires_in=10)).get("/")
    assert provider.calls == ["old-rt"]
    assert r.json() == {"sub": "u", "at": "new-at"}  # the route saw the new token
    assert "crm_session=" in r.headers["set-cookie"]  # and the browser gets it
    renewed = r.headers["set-cookie"].split(";")[0].partition("=")[2]
    assert sessions.serializer.loads(renewed)[REFRESH_KEY] == "new-rt"


def test_issuer_without_rotation_keeps_the_old_refresh_token():
    app, sessions, _ = build({"access_token": "new-at", "expires_in": 300})
    r = client_with(app, login_cookie(sessions, expires_in=10)).get("/")
    renewed = r.headers["set-cookie"].split(";")[0].partition("=")[2]
    assert sessions.serializer.loads(renewed)[REFRESH_KEY] == "old-rt"


def test_refused_refresh_signs_the_person_out():
    app, sessions, _ = build(AuthError("ended", provider="oidc"))
    r = client_with(app, login_cookie(sessions, expires_in=10)).get("/")
    assert r.json()["sub"] is None
    assert 'crm_session=""' in r.headers["set-cookie"] or "Max-Age=0" in r.headers["set-cookie"]


def test_issuer_outage_keeps_the_session():
    app, sessions, _ = build(ConfigError("down"))
    r = client_with(app, login_cookie(sessions, expires_in=10)).get("/")
    assert r.json() == {"sub": "u", "at": "old-at"}


def test_oversized_cookie_sheds_the_refresh_token_too():
    import secrets

    store = SessionStore(SECRET)
    response = Response()
    store.save_identity(
        response,
        Identity(subject="u", email="u@x"),
        **{TOKEN_KEY: secrets.token_urlsafe(4096), REFRESH_KEY: "rt", EXPIRES_KEY: 1},
    )
    value = response.headers["set-cookie"].split(";")[0].partition("=")[2]
    data = store.serializer.loads(value)
    assert REFRESH_KEY not in data and data["sub"] == "u"
