"""The authentication providers and the chain that tries them.

The distinction that matters throughout: returning ``None`` means "no
credentials of my kind here, ask the next provider"; raising means "credentials
were presented and are wrong". Collapsing the two would let a bad token fall
through to anonymous access.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request
from starlette.responses import Response

from app.auth.api_token import ApiTokenAuth, generate_token, hash_token
from app.auth.base import AuthError, BaseAuthProvider
from app.auth.chain import AuthChain, SessionAuth
from app.auth.local import LocalPasswordAuth, hash_password, verify_password
from app.auth.oidc import DevAuth, OIDCAuth
from app.auth.proxy_header import ProxyHeaderAuth
from app.auth.session import SessionStore
from app.core.results import ANONYMOUS, Identity
from app.providers.memory import MemoryProvider

SECRET = "test-secret-key-for-signing"


def make_request(
    *, headers: dict | None = None, client: tuple[str, int] | None = ("127.0.0.1", 1234),
    cookies: dict | None = None, path: str = "/",
) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if cookies:
        jar = "; ".join(f"{k}={v}" for k, v in cookies.items())
        raw_headers.append((b"cookie", jar.encode()))
    return Request({
        "type": "http", "method": "GET", "path": path, "headers": raw_headers,
        "client": client, "query_string": b"", "scheme": "http",
        "server": ("test", 80), "root_path": "",
    })


# -- passwords -------------------------------------------------------------


class TestPasswordHashing:
    def test_a_password_verifies_against_its_hash(self):
        assert verify_password(hash_password("correct horse"), "correct horse")

    def test_a_wrong_password_does_not(self):
        assert not verify_password(hash_password("correct horse"), "wrong")

    def test_the_hash_is_salted(self):
        assert hash_password("same") != hash_password("same")

    def test_a_corrupt_hash_fails_rather_than_raising(self):
        assert not verify_password("not-a-hash", "anything")


class TestLocalPasswordAuth:
    @pytest.fixture
    def users(self):
        return MemoryProvider([
            {"id": 1, "email": "ada@example.com", "name": "Ada",
             "password_hash": hash_password("secret123"),
             "roles": ["admin"], "is_active": True},
            {"id": 2, "email": "gone@example.com", "name": "Gone",
             "password_hash": hash_password("secret123"),
             "roles": ["user"], "is_active": False},
        ])

    @pytest.fixture
    def auth(self, users):
        return LocalPasswordAuth(users)

    async def test_correct_credentials_return_an_identity(self, auth):
        identity = await auth.login(
            make_request(), {"username": "ada@example.com", "password": "secret123"}
        )
        assert identity.email == "ada@example.com"
        assert identity.roles == frozenset({"admin"})

    async def test_the_username_is_case_insensitive(self, auth):
        identity = await auth.login(
            make_request(), {"username": "ADA@example.com", "password": "secret123"}
        )
        assert identity is not None

    async def test_a_wrong_password_is_rejected(self, auth):
        with pytest.raises(AuthError):
            await auth.login(make_request(), {"username": "ada@example.com", "password": "nope"})

    async def test_an_unknown_user_is_rejected(self, auth):
        with pytest.raises(AuthError):
            await auth.login(make_request(), {"username": "nobody@example.com", "password": "x"})

    async def test_both_failures_give_the_same_message(self, auth):
        """Saying which half was wrong enumerates who has an account."""
        with pytest.raises(AuthError) as wrong_password:
            await auth.login(make_request(), {"username": "ada@example.com", "password": "nope"})
        with pytest.raises(AuthError) as no_such_user:
            await auth.login(make_request(), {"username": "nobody@example.com", "password": "nope"})
        assert wrong_password.value.message == no_such_user.value.message

    async def test_a_deactivated_account_cannot_sign_in(self, auth):
        with pytest.raises(AuthError):
            await auth.login(make_request(), {"username": "gone@example.com", "password": "secret123"})

    async def test_empty_credentials_are_rejected(self, auth):
        with pytest.raises(AuthError):
            await auth.login(make_request(), {"username": "", "password": ""})

    async def test_roles_stored_as_json_text_are_parsed(self, users):
        import json

        from app.core.results import Ctx

        await users.create(
            {"email": "j@example.com", "name": "J", "password_hash": hash_password("x"),
             "roles": json.dumps(["manager", "user"]), "is_active": True},
            Ctx.system(),
        )
        identity = await LocalPasswordAuth(users).login(
            make_request(), {"username": "j@example.com", "password": "x"}
        )
        assert identity.roles == frozenset({"manager", "user"})


# -- API tokens ------------------------------------------------------------


class TestApiTokenAuth:
    @pytest.fixture
    def token_value(self):
        return generate_token()

    @pytest.fixture
    def auth(self, token_value):
        tokens = MemoryProvider([
            {"id": 1, "name": "ci-runner", "token_hash": hash_token(token_value),
             "roles": ["user"], "is_active": True},
            {"id": 2, "name": "revoked", "token_hash": hash_token("crm_revoked"),
             "roles": ["admin"], "is_active": False},
        ])
        return ApiTokenAuth(tokens)

    async def test_a_valid_token_identifies_the_caller(self, auth, token_value):
        identity = await auth.authenticate(
            make_request(headers={"Authorization": f"Bearer {token_value}"})
        )
        assert identity.subject == "ci-runner"
        assert identity.roles == frozenset({"user"})

    async def test_no_header_defers_to_the_next_provider(self, auth):
        assert await auth.authenticate(make_request()) is None

    async def test_a_different_scheme_defers(self, auth):
        result = await auth.authenticate(make_request(headers={"Authorization": "Basic abc"}))
        assert result is None

    async def test_an_invalid_token_stops_the_chain(self, auth):
        # Crucially not None: a bad token must not fall through to anonymous.
        with pytest.raises(AuthError):
            await auth.authenticate(make_request(headers={"Authorization": "Bearer crm_wrong"}))

    async def test_a_revoked_token_is_rejected(self, auth):
        with pytest.raises(AuthError):
            await auth.authenticate(make_request(headers={"Authorization": "Bearer crm_revoked"}))

    def test_tokens_are_stored_only_as_hashes(self, token_value):
        digest = hash_token(token_value)
        assert token_value not in digest
        assert len(digest) == 64

    def test_generated_tokens_are_unique_and_prefixed(self):
        first, second = generate_token(), generate_token()
        assert first != second
        assert first.startswith("crm_")


# -- proxy headers ---------------------------------------------------------


class TestProxyHeaderAuth:
    def test_without_a_trusted_network_it_is_disabled(self):
        """Failing closed matters: an empty allowlist that honoured headers
        would be a complete authentication bypass."""
        assert not ProxyHeaderAuth().enabled

    async def test_a_disabled_provider_ignores_headers(self):
        auth = ProxyHeaderAuth()
        result = await auth.authenticate(
            make_request(headers={"X-Forwarded-User": "attacker"})
        )
        assert result is None

    async def test_a_trusted_peer_is_believed(self):
        auth = ProxyHeaderAuth(trusted_ips=["127.0.0.1"])
        identity = await auth.authenticate(
            make_request(headers={
                "X-Forwarded-User": "ada",
                "X-Forwarded-Email": "ada@example.com",
                "X-Forwarded-Groups": "admin,staff",
            })
        )
        assert identity.email == "ada@example.com"
        assert identity.roles == frozenset({"admin", "staff"})

    async def test_an_untrusted_peer_is_not(self):
        auth = ProxyHeaderAuth(trusted_ips=["10.0.0.0/8"])
        result = await auth.authenticate(
            make_request(headers={"X-Forwarded-User": "attacker"}, client=("203.0.113.7", 1))
        )
        assert result is None

    async def test_a_cidr_range_is_honoured(self):
        auth = ProxyHeaderAuth(trusted_ips=["10.0.0.0/8"])
        identity = await auth.authenticate(
            make_request(headers={"X-Forwarded-User": "ada"}, client=("10.4.5.6", 1))
        )
        assert identity is not None

    async def test_a_trusted_peer_sending_no_headers_defers(self):
        auth = ProxyHeaderAuth(trusted_ips=["127.0.0.1"])
        assert await auth.authenticate(make_request()) is None

    async def test_default_roles_apply_when_no_groups_are_sent(self):
        auth = ProxyHeaderAuth(trusted_ips=["127.0.0.1"], default_roles=("user",))
        identity = await auth.authenticate(make_request(headers={"X-Forwarded-User": "ada"}))
        assert identity.roles == frozenset({"user"})


# -- sessions --------------------------------------------------------------


class TestSessionStore:
    @pytest.fixture
    def store(self):
        return SessionStore(SECRET)

    def test_an_identity_round_trips(self, store):
        identity = Identity(subject="1", email="ada@example.com", display_name="Ada",
                            roles=frozenset({"admin"}), provider="local")
        carrier = Response()
        store.save_identity(carrier, identity)
        cookie = carrier.headers["set-cookie"].split(";")[0].split("=", 1)[1]

        restored = store.load_identity(make_request(cookies={store.cookie_name: cookie}))
        assert restored.subject == "1"
        assert restored.roles == frozenset({"admin"})

    def test_no_cookie_means_no_identity(self, store):
        assert store.load_identity(make_request()) is None

    def test_a_tampered_cookie_is_ignored(self, store):
        result = store.load_identity(
            make_request(cookies={store.cookie_name: "forged.payload.here"})
        )
        assert result is None, "an unsigned cookie must read as signed out"

    def test_a_cookie_from_another_key_is_ignored(self, store):
        other = SessionStore("a-completely-different-key")
        carrier = Response()
        other.save_identity(carrier, Identity(subject="1"))
        cookie = carrier.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        assert store.load_identity(make_request(cookies={store.cookie_name: cookie})) is None

    def test_the_cookie_is_http_only(self, store):
        carrier = Response()
        store.save_identity(carrier, Identity(subject="1"))
        assert "httponly" in carrier.headers["set-cookie"].lower()

    def test_large_claims_are_not_carried(self, store):
        # A cookie has ~4KB; a full token payload would overflow it.
        identity = Identity(subject="1", claims={"huge": "x" * 5000, "small": "ok"})
        carrier = Response()
        store.save_identity(carrier, identity)
        cookie = carrier.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        restored = store.load_identity(make_request(cookies={store.cookie_name: cookie}))
        assert "huge" not in restored.claims
        assert restored.claims["small"] == "ok"


# -- the chain -------------------------------------------------------------


class Never(BaseAuthProvider):
    name = "never"

    async def authenticate(self, request):
        return None


class Always(BaseAuthProvider):
    name = "always"

    def __init__(self, subject="always"):
        self.subject = subject

    async def authenticate(self, request):
        return Identity(subject=self.subject, provider=self.name)


class Refuses(BaseAuthProvider):
    name = "refuses"

    async def authenticate(self, request):
        raise AuthError("bad credentials", provider=self.name)


class TestAuthChain:
    async def test_the_first_provider_to_answer_wins(self):
        chain = AuthChain([Never(), Always("first"), Always("second")])
        identity = await chain.authenticate(make_request())
        assert identity.subject == "first"

    async def test_an_empty_chain_yields_anonymous(self):
        assert await AuthChain([]).authenticate(make_request()) is ANONYMOUS

    async def test_all_declining_yields_anonymous(self):
        assert await AuthChain([Never(), Never()]).authenticate(make_request()) is ANONYMOUS

    async def test_a_refusal_stops_the_chain(self):
        # Otherwise a wrong password would quietly become an anonymous request.
        chain = AuthChain([Refuses(), Always()])
        with pytest.raises(AuthError):
            await chain.authenticate(make_request())

    def test_interactive_providers_are_identified(self):
        chain = AuthChain([Never(), SessionAuth(SessionStore(SECRET))])
        assert chain.interactive == ()

    def test_a_provider_can_be_looked_up_by_name(self):
        chain = AuthChain([Always()])
        assert chain.get("always") is not None
        assert chain.get("nonexistent") is None

    async def test_health_covers_every_provider(self):
        results = await AuthChain([Never(), Always()]).health()
        assert len(results) == 2


class TestDevAuth:
    async def test_it_signs_everyone_in(self):
        identity = await DevAuth().authenticate(make_request())
        assert identity.is_authenticated
        assert identity.roles == frozenset({"admin"})

    def test_it_is_refused_in_production(self):
        from app.core.errors import ConfigError
        from app.core.registry import Registry
        from app.main import build_auth
        from app.settings import Settings

        settings = Settings(environment="production", dev_auth=True, secret_key="x")
        with pytest.raises(ConfigError, match="production"):
            build_auth(settings, Registry(), SessionStore(SECRET))


class TestOIDCConfiguration:
    def test_an_issuer_is_required(self):
        from app.core.errors import ConfigError

        with pytest.raises(ConfigError):
            OIDCAuth(issuer="", client_id="x", secret_key=SECRET)

    def test_claims_map_to_roles(self):
        auth = OIDCAuth(issuer="https://issuer.test", client_id="x", secret_key=SECRET,
                        roles_claim="groups", role_map={"crm-admins": "admin"})
        assert auth.extract_roles({"groups": ["crm-admins", "other"]}) == frozenset({"admin"})

    def test_an_unmapped_group_grants_nothing_extra(self):
        # With a map configured, only mapped values count -- an unexpected
        # directory group must not silently become a role here.
        auth = OIDCAuth(issuer="https://issuer.test", client_id="x", secret_key=SECRET,
                        roles_claim="groups", role_map={"crm-admins": "admin"},
                        default_roles=("user",))
        assert auth.extract_roles({"groups": ["unknown-group"]}) == frozenset({"user"})

    def test_without_a_map_claims_are_used_directly(self):
        auth = OIDCAuth(issuer="https://issuer.test", client_id="x", secret_key=SECRET,
                        roles_claim="roles")
        assert auth.extract_roles({"roles": ["manager"]}) == frozenset({"manager"})

    def test_a_missing_claim_falls_back_to_the_defaults(self):
        auth = OIDCAuth(issuer="https://issuer.test", client_id="x", secret_key=SECRET,
                        default_roles=("user",))
        assert auth.extract_roles({}) == frozenset({"user"})


class TestOIDCNestedRolesClaims:
    """Not every issuer puts roles at the top level.

    Keycloak -- named in the provider's own docstring alongside Google, Entra
    and Auth0 -- nests realm roles under ``realm_access.roles``. A flat lookup
    found nothing there and fell through to ``default_roles``, so an
    administrator signed in successfully and arrived with a stranger's rights,
    with no error to explain it.
    """

    KEYCLOAK = {
        "realm_access": {"roles": ["offline_access", "crm-admins"]},
        "resource_access": {"crm": {"roles": ["crm-managers"]}},
    }

    def auth(self, **kw):
        return OIDCAuth(issuer="https://issuer.test", client_id="x", secret_key=SECRET, **kw)

    def test_a_nested_realm_role_is_found(self):
        auth = self.auth(roles_claim="realm_access.roles", role_map={"crm-admins": "admin"})
        assert auth.extract_roles(self.KEYCLOAK) == frozenset({"admin"})

    def test_a_client_role_two_levels_down_is_found(self):
        auth = self.auth(roles_claim="resource_access.crm.roles",
                         role_map={"crm-managers": "manager"})
        assert auth.extract_roles(self.KEYCLOAK) == frozenset({"manager"})

    def test_without_a_map_nested_values_are_used_directly(self):
        auth = self.auth(roles_claim="realm_access.roles")
        assert auth.extract_roles(self.KEYCLOAK) == frozenset({"offline_access", "crm-admins"})

    def test_a_top_level_claim_still_wins_over_a_dotted_reading(self):
        # A claim whose name genuinely contains a dot must keep working.
        auth = self.auth(roles_claim="realm_access.roles")
        assert auth.extract_roles({"realm_access.roles": ["direct"]}) == frozenset({"direct"})

    def test_a_path_through_a_missing_branch_falls_back(self):
        auth = self.auth(roles_claim="realm_access.roles", default_roles=("user",))
        assert auth.extract_roles({"sub": "x"}) == frozenset({"user"})

    def test_a_path_through_a_non_mapping_falls_back(self):
        # `realm_access` present but a string: walking into it must not raise.
        auth = self.auth(roles_claim="realm_access.roles", default_roles=("user",))
        assert auth.extract_roles({"realm_access": "nope"}) == frozenset({"user"})

    def test_the_flat_case_is_unchanged(self):
        auth = self.auth(roles_claim="roles")
        assert auth.extract_roles({"roles": ["manager"]}) == frozenset({"manager"})


class TestLoginFlow:
    def test_the_login_page_renders(self, client):
        assert "password" in client.get("/login").text.lower()

    def test_signing_in_and_out_through_the_real_flow(self, client):
        from .conftest import TEST_PASSWORD, csrf_from

        token = csrf_from(client, "/login")
        response = client.post(
            "/login",
            data={"csrf_token": token, "username": "admin@example.com",
                  "password": TEST_PASSWORD, "next": "/"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert client.get("/whoami").json()["email"] == "admin@example.com"

        client.get("/logout", follow_redirects=False)
        assert client.get("/whoami").json()["authenticated"] is False

    def test_wrong_credentials_re_render_the_form_with_401(self, client):
        from .conftest import csrf_from

        token = csrf_from(client, "/login")
        response = client.post(
            "/login",
            data={"csrf_token": token, "username": "admin@example.com", "password": "wrong"},
        )
        assert response.status_code == 401
        assert "do not match" in response.text

    def test_the_submitted_username_is_kept_after_a_failure(self, client):
        from .conftest import csrf_from

        token = csrf_from(client, "/login")
        response = client.post(
            "/login",
            data={"csrf_token": token, "username": "admin@example.com", "password": "wrong"},
        )
        assert 'value="admin@example.com"' in response.text

    def test_an_offsite_next_url_is_refused(self, client):
        """Otherwise the login page becomes an open redirect."""
        from app.web.routes.auth import _safe_next

        assert _safe_next("https://evil.test/steal") == "/"
        assert _safe_next("//evil.test") == "/"
        assert _safe_next("/r/contacts") == "/r/contacts"


class TestRejectedCredentialsReachTheClient:
    """A wrong credential is a 401, not a 500 and not a login redirect."""

    @pytest.fixture
    def app_with_tokens(self, settings):
        from app.fields.types import TextField
        from app.main import create_app
        from app.resources.resource import Resource

        from .conftest import build_registry

        registry = build_registry()
        registry.add_resource(
            Resource(
                "api_tokens",
                provider=MemoryProvider([
                    {"id": 1, "name": "ci", "token_hash": hash_token("crm_good"),
                     "roles": ["user"], "is_active": True},
                ]),
                fields=[TextField("id"), TextField("name"), TextField("token_hash")],
            )
        )
        settings.auth_providers = ["api_token", "session"]
        return create_app(settings=settings, registry=registry)

    def test_a_valid_token_is_accepted(self, app_with_tokens):
        from starlette.testclient import TestClient

        with TestClient(app_with_tokens, raise_server_exceptions=False) as client:
            payload = client.get(
                "/whoami", headers={"Authorization": "Bearer crm_good"}
            ).json()
            assert payload["authenticated"] and payload["provider"] == "api_token"

    def test_an_invalid_token_is_401_not_500(self, app_with_tokens):
        from starlette.testclient import TestClient

        with TestClient(app_with_tokens, raise_server_exceptions=False) as client:
            response = client.get(
                "/r/contacts",
                headers={"Authorization": "Bearer crm_wrong", "Accept": "application/json"},
            )
            assert response.status_code == 401
            assert "error" in response.json()

    def test_an_invalid_token_does_not_fall_through_to_anonymous(self, app_with_tokens):
        from starlette.testclient import TestClient

        with TestClient(app_with_tokens, raise_server_exceptions=False) as client:
            response = client.get("/whoami", headers={"Authorization": "Bearer crm_wrong"})
            assert response.status_code == 401
