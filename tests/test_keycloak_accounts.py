"""The Keycloak accounts module: the roster, impersonation, and its role.

What matters: the endpoint the roster reads (a realm role's members and a
client role's are at different paths), the client-uuid lookup that makes the
latter reachable, that impersonation always has the operator's own token and a
log row, and that the role changes are made with that token too, so Keycloak --
not this application -- is what refuses a non-admin.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

import modules.keycloak_accounts as kc
from app.core.errors import ConfigError
from app.core.registry import Registry, StaffDirectory
from app.core.results import Ctx, Identity, Record
from app.fields.types import DateTimeField, EmailField, TextField
from app.providers.memory import MemoryProvider
from app.providers.rest import RestConnection
from app.resources.resource import Resource
from app.settings import Settings

CFG = kc.KeycloakConfig(url="https://kc.test", realm="master", roles=("admin", "manager"))
CLIENT_CFG = kc.KeycloakConfig(
    url="https://kc.test", realm="master", roles=("admin", "manager"), client="app"
)


def _jwt(claims: dict) -> str:
    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


ADMIN_TOKEN = _jwt({"realm_access": {"roles": ["admin"]}})
PLAIN_TOKEN = _jwt({"realm_access": {"roles": ["manager"]}})
TARGET = Record({"id": "u-1", "username": "kim", "email": "kim@example.com"})


def _ctx(token: str | None = ADMIN_TOKEN, subject: str = "operator") -> Ctx:
    return Ctx(
        identity=Identity(
            subject=subject, email="op@example.com", display_name="Op", roles=frozenset({"admin"})
        ),
        extra={"access_token": token} if token else {},
    )


# -- the roster ------------------------------------------------------------------


class TestMembersEndpoint:
    def test_with_no_client_it_reads_the_realm_role_endpoint(self):
        target = kc._members_of(CFG, "admin")
        assert target == "api.keycloak#/admin/realms/master/roles/admin/users"

    def test_with_a_client_it_reads_the_client_role_endpoint(self):
        target = kc._members_of(CLIENT_CFG, "admin")
        assert "/clients/{client_uuid}/roles/admin/users" in target


class TestClientUuidResolver:
    def _connection(self, handler) -> RestConnection:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://kc.test"
        )
        return RestConnection(client)

    PATH = "/admin/realms/master/clients/{client_uuid}/roles/admin/users"

    async def test_resolves_the_uuid_from_the_clientid(self):
        calls = []

        def handler(request):
            calls.append(request)
            assert request.url.params["clientId"] == "app"
            return httpx.Response(200, json=[{"id": "abc-123"}])

        path = await kc._client_uuid_resolver(CLIENT_CFG)(self._connection(handler), self.PATH)
        assert path == "/admin/realms/master/clients/abc-123/roles/admin/users"
        assert len(calls) == 1

    async def test_the_lookup_is_cached_across_roles(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=[{"id": "abc-123"}])

        resolve = kc._client_uuid_resolver(CLIENT_CFG)
        connection = self._connection(handler)
        await resolve(connection, self.PATH)
        await resolve(connection, self.PATH.replace("admin", "manager"))
        assert len(calls) == 1

    async def test_an_explicit_uuid_skips_the_lookup(self):
        def handler(request):
            raise AssertionError("should not call Keycloak when the uuid is preset")

        cfg = kc.KeycloakConfig(client="app", client_uuid="preset")
        path = await kc._client_uuid_resolver(cfg)(self._connection(handler), self.PATH)
        assert "/clients/preset/" in path

    async def test_a_client_that_does_not_exist_fails_loudly(self):
        with pytest.raises(ConfigError, match="no Keycloak client"):
            await kc._client_uuid_resolver(CLIENT_CFG)(
                self._connection(lambda r: httpx.Response(200, json=[])), self.PATH
            )

    async def test_a_path_without_the_placeholder_is_untouched(self):
        def handler(request):
            raise AssertionError("no placeholder means nothing to resolve")

        path = "/admin/realms/master/users/{pk}"
        assert await kc._client_uuid_resolver(CLIENT_CFG)(self._connection(handler), path) == path


class TestRegistering:
    def _register(self, **settings) -> Registry:
        from tests.conftest import build_registry

        registry = build_registry()
        registry.settings = Settings(
            keycloak_url="https://kc.test", **settings
        )
        kc.register(registry)
        return registry

    def test_it_adds_the_screens_and_actions(self):
        registry = self._register(keycloak_roles="admin,manager")
        accounts = registry.resource("keycloak_users")
        assert set(accounts.actions) >= {
            "impersonate", "grant_impersonation", "revoke_impersonation"
        }
        assert registry.has_resource("impersonation_log")

    def test_the_roster_becomes_the_staff_directory(self):
        registry = self._register(keycloak_roles="admin,manager")
        assert registry.staff_directory == StaffDirectory(
            "keycloak_users", roles=("admin", "manager")
        )

    def test_a_single_role_needs_no_union(self):
        registry = self._register(keycloak_roles="admin")
        assert registry.resource("keycloak_users").field("role").name == "role"

    def test_settings_become_a_config(self):
        cfg = kc.KeycloakConfig.from_settings(
            Settings(
                keycloak_url="https://kc.test/", keycloak_realm="staff",
                keycloak_roles="a,b", keycloak_client="app", keycloak_frontend_url="https://app.test/",
            )
        )
        assert (cfg.url, cfg.realm, cfg.roles, cfg.client, cfg.frontend_url) == (
            "https://kc.test", "staff", ("a", "b"), "app", "https://app.test"
        )
        assert cfg.management_client == "realm-management"


# -- impersonating ---------------------------------------------------------------


@pytest.fixture
def screen():
    """A registry with the log resource, backed in memory, and a stand-in resource."""
    from tests.conftest import build_registry

    registry = build_registry()
    log = MemoryProvider([])
    registry.add_resource(
        Resource(
            "impersonation_log", provider=log, audited=False, timeline=False,
            fields=[
                TextField("id", in_form=False), EmailField("impersonator_email"),
                TextField("impersonator_name"), TextField("target_keycloak_id"),
                TextField("target_username"), EmailField("target_email"),
                DateTimeField("created_at"),
            ],
        )
    )
    people = registry.add_resource(
        Resource("people", provider=MemoryProvider([]), audited=False, timeline=False,
                 fields=[TextField("id", in_form=False), TextField("name")])
    )
    return registry, log, people


async def _run(action, records, ctx, resource):
    return await action.handler(records, ctx, resource)


class TestImpersonate:
    async def _action(self, screen, resolve=kc._account_target):
        registry, log, people = screen
        await registry.bind()
        return kc.impersonate_action(resolve, CFG), registry.resource("people"), log

    async def test_it_logs_and_hands_over_a_redirect_with_the_operators_token(self, screen):
        action, resource, log = await self._action(screen)
        result = await _run(action, [TARGET], _ctx(), resource)
        assert result.template == "impersonation/redirect.html"
        assert result.data["access_token"] == ADMIN_TOKEN
        assert result.data["impersonate_url"] == (
            "https://kc.test/admin/realms/master/users/u-1/impersonation"
        )
        (row,) = list(log._rows.values())
        assert row["impersonator_email"] == "op@example.com"
        assert (row["target_keycloak_id"], row["target_username"]) == ("u-1", "kim")

    async def test_without_a_token_nothing_is_logged(self, screen):
        action, resource, log = await self._action(screen)
        result = await _run(action, [TARGET], _ctx(None), resource)
        assert result.level == "error" and "KEEP_ACCESS_TOKEN" in result.message
        assert not log._rows

    async def test_without_a_keycloak_url_it_says_so(self, screen):
        registry, *_ = screen
        await registry.bind()
        action = kc.impersonate_action(kc._account_target, kc.KeycloakConfig())
        result = await _run(action, [TARGET], _ctx(), registry.resource("people"))
        assert result.level == "error" and "CRM_KEYCLOAK_URL" in result.message

    async def test_one_at_a_time(self, screen):
        action, resource, log = await self._action(screen)
        result = await _run(action, [TARGET, TARGET], _ctx(), resource)
        assert result.level == "error" and not log._rows

    async def test_a_custom_resolver_decides_who_the_record_is(self, screen):
        async def resolve(record, ctx, resource):
            return f"kc-{record['id']}", record["name"], None

        action, resource, log = await self._action(screen, resolve)
        result = await _run(action, [Record({"id": 7, "name": "Ada"})], _ctx(), resource)
        assert result.data["impersonate_url"].endswith("/users/kc-7/impersonation")
        assert next(iter(log._rows.values()))["target_username"] == "Ada"

    async def test_a_record_with_no_account_is_refused(self, screen):
        async def resolve(record, ctx, resource):
            return None

        action, resource, log = await self._action(screen, resolve)
        result = await _run(action, [TARGET], _ctx(), resource)
        assert result.level == "error" and not log._rows

    def test_only_admins_are_offered_it(self):
        assert kc.impersonate_action(kc._account_target, CFG).roles == frozenset(
            kc.IMPERSONATE_ROLES
        )


# -- granting and revoking the role ------------------------------------------------


@pytest.fixture
def keycloak(monkeypatch):
    calls: list[httpx.Request] = []
    state = {"mapping_status": 204}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path.endswith("/clients"):
            assert request.url.params["clientId"] == "master-realm"
            return httpx.Response(200, json=[{"id": "c-uuid"}])
        if path.endswith("/roles/impersonation"):
            return httpx.Response(200, json={"id": "r-1", "name": "impersonation"})
        return httpx.Response(state["mapping_status"])

    monkeypatch.setattr(kc, "_transport", httpx.MockTransport(handler))
    return calls, state


class TestGrant:
    async def test_posts_the_role_mapping_with_the_operators_token(self, keycloak):
        calls, _ = keycloak
        result = await kc._change_impersonation_role(CFG, [TARGET], _ctx(), grant=True)
        assert result.level == "success"
        write = calls[-1]
        assert write.method == "POST"
        assert write.url.path == "/admin/realms/master/users/u-1/role-mappings/clients/c-uuid"
        assert json.loads(write.content) == [{"id": "r-1", "name": "impersonation"}]
        assert write.headers["Authorization"] == f"Bearer {ADMIN_TOKEN}"

    async def test_revoke_deletes_the_mapping(self, keycloak):
        calls, _ = keycloak
        result = await kc._change_impersonation_role(CFG, [TARGET], _ctx(), grant=False)
        assert result.level == "success" and calls[-1].method == "DELETE"

    async def test_client_admin_roles_count_as_keycloak_admin(self, keycloak):
        token = _jwt({"resource_access": {"master-realm": {"roles": ["manage-users"]}}})
        result = await kc._change_impersonation_role(CFG, [TARGET], _ctx(token), grant=True)
        assert result.level == "success"


class TestRefusals:
    async def test_without_keycloak_admin_nothing_is_called(self, keycloak):
        calls, _ = keycloak
        result = await kc._change_impersonation_role(
            CFG, [TARGET], _ctx(PLAIN_TOKEN), grant=True
        )
        assert result.level == "error" and "admin access" in result.message
        assert calls == []

    async def test_without_a_token_it_says_why(self, keycloak):
        result = await kc._change_impersonation_role(CFG, [TARGET], _ctx(None), grant=True)
        assert result.level == "error" and "KEEP_ACCESS_TOKEN" in result.message

    async def test_keycloak_403_is_reported_not_swallowed(self, keycloak):
        _, state = keycloak
        state["mapping_status"] = 403
        result = await kc._change_impersonation_role(CFG, [TARGET], _ctx(), grant=True)
        assert result.level == "error" and "refused" in result.message

    async def test_cannot_revoke_your_own_role(self, keycloak):
        calls, _ = keycloak
        result = await kc._change_impersonation_role(
            CFG, [TARGET], _ctx(subject="u-1"), grant=False
        )
        assert result.level == "error" and calls == []

    async def test_one_account_at_a_time(self, keycloak):
        result = await kc._change_impersonation_role(CFG, [TARGET, TARGET], _ctx(), grant=True)
        assert result.level == "error"

    async def test_no_keycloak_url_means_unavailable(self, keycloak):
        result = await kc._change_impersonation_role(
            kc.KeycloakConfig(), [TARGET], _ctx(), grant=True
        )
        assert result.level == "error" and "CRM_KEYCLOAK_URL" in result.message
