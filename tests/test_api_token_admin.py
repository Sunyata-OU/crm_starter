"""Issuing and rotating a token from the administration screens.

The design point: the value exists exactly once, in the reply to the action
that made it. Only its hash is stored, so a screen that could show a token
later would mean the application had kept one — and the test that matters most
here is the one asserting it did not.
"""

from __future__ import annotations

import pytest

from app.auth.api_token import hash_token
from app.core.clock import utcnow
from app.core.registry import Registry
from app.core.results import Ctx, Identity
from app.providers.memory import MemoryProvider
from modules.core_identity import DEFAULT_TOKEN_DAYS, _api_tokens

ADMIN = Identity(subject="1", email="admin@example.com", display_name="Admin",
                 roles=frozenset({"admin"}))
CTX = Ctx(identity=ADMIN)


@pytest.fixture
def store() -> MemoryProvider:
    return MemoryProvider([])


@pytest.fixture
async def tokens(store):
    """The shipped resource declaration, backed by memory rather than db.main."""
    registry = Registry()
    resource = _api_tokens()
    resource.provider_ref = store
    registry.add_resource(resource)
    await registry.bind()
    yield registry.resource("api_tokens")
    await registry.close()


def rows_of(store: MemoryProvider) -> list[dict]:
    return list(store._rows.values())


def token_from(result) -> str:
    """The value the report modal was given, as the browser would receive it."""
    return result.data["rows"][0][0]


class TestIssuing:
    async def test_a_token_is_created_and_shown_once(self, tokens, store):
        result = await tokens.action("issue_token").run(
            [], CTX, tokens, params={"token_name": "tasky", "token_roles": ["user"],
                                     "expires_in_days": "30"},
        )
        raw = token_from(result)
        assert raw.startswith("crm_")
        assert len(rows_of(store)) == 1

    async def test_only_the_hash_is_kept(self, tokens, store):
        result = await tokens.action("issue_token").run(
            [], CTX, tokens, params={"token_name": "tasky", "expires_in_days": ""},
        )
        raw = token_from(result)
        stored = rows_of(store)[0]
        assert stored["token_hash"] == hash_token(raw)
        # The value itself appears in no column, under any name.
        assert raw not in str(stored)

    async def test_the_days_box_becomes_a_date(self, tokens, store):
        await tokens.action("issue_token").run(
            [], CTX, tokens, params={"token_name": "tasky", "expires_in_days": "30"},
        )
        expires = rows_of(store)[0]["expires_at"]
        assert 29 <= (expires - utcnow()).days <= 30

    async def test_an_empty_box_means_no_expiry(self, tokens, store):
        result = await tokens.action("issue_token").run(
            [], CTX, tokens, params={"token_name": "forever", "expires_in_days": ""},
        )
        assert rows_of(store)[0]["expires_at"] is None
        # And the report says so, rather than leaving it to be discovered.
        assert "never expires" in result.message

    async def test_a_nonsense_expiry_is_refused_before_anything_is_created(
        self, tokens, store
    ):
        result = await tokens.action("issue_token").run(
            [], CTX, tokens, params={"token_name": "tasky", "expires_in_days": "soon"},
        )
        assert result.level == "error"
        assert not rows_of(store)

    async def test_a_token_needs_a_label(self, tokens, store):
        # The label is what somebody reads when deciding whether to revoke it.
        result = await tokens.action("issue_token").run(
            [], CTX, tokens, params={"token_name": "  ", "expires_in_days": "30"},
        )
        assert result.level == "error"
        assert not rows_of(store)

    def test_the_default_lifetime_is_finite(self):
        assert DEFAULT_TOKEN_DAYS > 0


class TestRotating:
    @pytest.fixture
    async def existing(self, tokens, store):
        await tokens.action("issue_token").run(
            [], CTX, tokens, params={"token_name": "tasky", "expires_in_days": "30"},
        )
        return await tokens.provider.get(next(iter(store._rows)), CTX)

    async def test_the_old_value_stops_working(self, tokens, store, existing):
        before = existing["token_hash"]
        result = await tokens.action("rotate_token").run([existing], CTX, tokens)
        after = rows_of(store)[0]["token_hash"]
        assert after != before
        assert after == hash_token(token_from(result))

    async def test_the_row_survives_with_its_history(self, tokens, store, existing):
        await tokens.action("rotate_token").run([existing], CTX, tokens)
        stored = rows_of(store)[0]
        assert stored["name"] == "tasky"
        assert stored["expires_at"] == existing["expires_at"]
        assert stored["rotated_at"] is not None

    async def test_several_at_once_is_refused(self, tokens, store, existing):
        # Each new value has to be read back, and one modal cannot show two.
        result = await tokens.action("rotate_token").run(
            [existing, existing], CTX, tokens
        )
        assert result.level == "warning"
        assert rows_of(store)[0]["token_hash"] == existing["token_hash"]


class TestTheScreen:
    def test_there_is_no_ordinary_create_form(self):
        # A row typed in by hand would have no hash behind it: a credential
        # that authenticates nobody and looks exactly like one that works.
        resource = _api_tokens()
        assert "issue_token" in resource.actions
        assert resource.get_field("token_hash").read_roles == frozenset({"nobody"})

    def test_the_hash_is_never_rendered(self):
        resource = _api_tokens()
        field = resource.get_field("token_hash")
        assert not field.in_list and not field.in_form and not field.in_detail
