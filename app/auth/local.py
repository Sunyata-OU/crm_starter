"""Username and password against a table of users.

The reference implementation of an interactive provider, and the fallback when
no SSO is configured. Passwords are hashed with Argon2id.

This is also the only shipped provider that *owns* a password, which it says so
in its capabilities. Every other provider -- OIDC, a gateway header, an API
token -- authenticates against a credential held somewhere else, and the
account screens ask before offering to change anything. See
:class:`~app.auth.base.AuthCapabilities`.

Lockout state lives in the user row rather than in this object, because there
is one of these per worker and an attacker should not get one budget of guesses
per worker.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from starlette.requests import Request

from app.auth.base import AuthCapabilities, AuthError, BaseAuthProvider
from app.auth.passwords import (
    LockoutPolicy,
    ResetTokens,
    check_password,
)
from app.core.clock import utcnow
from app.core.query import Condition, ListQuery, Op
from app.core.results import Ctx, Identity, Record
from app.providers.base import Provider

log = logging.getLogger("crm.auth")

_hasher = PasswordHasher()

#: Verified against when no user matches, so a wrong username and a wrong
#: password take the same time. Without this, response timing enumerates users.
_DUMMY_HASH = _hasher.hash("timing-equalisation-placeholder")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return True


def needs_rehash(stored_hash: str) -> bool:
    """Whether a stored hash uses outdated parameters and should be upgraded."""
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


class LocalPasswordAuth(BaseAuthProvider):
    """Checks credentials against a user resource.

    Users live behind a provider like everything else, so the account table can
    sit in a different database from the CRM data, or behind an API.
    """

    name = "local"
    interactive = True
    label = "Sign in with a password"
    password_note = "Your password is stored by this application."

    def __init__(
        self,
        users: Provider,
        *,
        identity_field: str = "email",
        password_field: str = "password_hash",
        name_field: str = "name",
        roles_field: str = "roles",
        active_field: str = "is_active",
        timezone_field: str = "timezone",
        min_length: int = 12,
        lockout: LockoutPolicy | None = None,
        reset_tokens: ResetTokens | None = None,
    ) -> None:
        self.users = users
        self.identity_field = identity_field
        self.password_field = password_field
        self.name_field = name_field
        self.roles_field = roles_field
        self.active_field = active_field
        self.timezone_field = timezone_field
        self.min_length = min_length
        self.lockout = lockout or LockoutPolicy()
        #: Absent when no secret key was supplied, which is how a deployment
        #: turns emailed reset links off.
        self.reset_tokens = reset_tokens
        self.capabilities = AuthCapabilities(
            manages_passwords=True,
            change=True,
            admin_reset=True,
            self_service_reset=reset_tokens is not None,
            lockout=self.lockout.enabled,
        )

    async def find_user(self, username: str) -> Record | None:
        page = await self.users.list(
            ListQuery(
                filter=Condition(self.identity_field, Op.EQ, username.strip().lower()),
                page_size=2,
                with_total=False,
            ),
            Ctx.system(),
        )
        items = list(page.items)
        return items[0] if items else None

    async def login(self, request: Request, credentials: dict[str, Any]) -> Identity | None:
        username = str(credentials.get("username", "")).strip()
        password = str(credentials.get("password", ""))
        if not username or not password:
            raise AuthError("Enter both an email address and a password.", provider=self.name)

        user = await self.find_user(username)

        # A locked account is told so, and told when to come back. This does
        # leak that the address exists -- and it is the right trade: someone
        # locked out needs to know why, and an attacker who has already made
        # eight attempts against an address has learned that much anyway.
        if user is not None and self._locked_until(user) is not None:
            locked_until = self._locked_until(user)
            assert locked_until is not None
            raise AuthError(self.lockout.wait_message(locked_until), provider=self.name)

        stored = str(user[self.password_field]) if user else _DUMMY_HASH
        ok = verify_password(stored, password)

        # One message for every failure mode. Saying which half was wrong tells
        # an attacker which addresses have accounts.
        if not user or not ok or not self._is_active(user):
            if user is not None and self.lockout.enabled:
                await self._record_failure(user)
            raise AuthError("That email address and password do not match.", provider=self.name)

        await self._record_success(user, password, stored)
        identity = self.to_identity(user)
        if _flag(user.get("must_change_password")):
            # Carried on the identity rather than enforced here, because
            # refusing the sign-in would leave the user with no way to reach
            # the form that fixes it. The gate lives in the web layer.
            identity = identity.replace(must_change_password=True)
        return identity

    # -- lockout ------------------------------------------------------------

    def _locked_until(self, user: Record):
        from app.core.clock import parse

        raw = user.get("locked_until")
        locked_until = parse(raw) if raw is not None else None
        if locked_until is None or not self.lockout.is_locked(locked_until):
            return None
        return locked_until

    async def _record_failure(self, user: Record) -> None:
        """Count a wrong guess, and lock the account once there are enough.

        Failures are written even though the request is about to be refused:
        the whole point is that the count survives this process.
        """
        from app.core.clock import parse

        raw_last = user.get("last_failed_login")
        failures, locked_until = self.lockout.next_state(
            int(user.get("failed_logins") or 0),
            parse(raw_last) if raw_last is not None else None,
        )
        await self._write(
            user,
            {
                "failed_logins": failures,
                "last_failed_login": utcnow(),
                "locked_until": locked_until,
            },
        )

    async def _record_success(self, user: Record, password: str, stored: str) -> None:
        """Note the sign-in, clear the failure count, and upgrade the hash.

        The rehash is the part worth noticing: Argon2's recommended parameters
        get more expensive as hardware does, and a stored hash from three years
        ago stays weak forever unless something upgrades it. The only moment
        the plaintext is available to do that is a successful sign-in.
        """
        changes: dict[str, Any] = {"last_login": utcnow()}
        if int(user.get("failed_logins") or 0) or user.get("locked_until"):
            changes.update(failed_logins=0, last_failed_login=None, locked_until=None)
        if needs_rehash(stored):
            changes[self.password_field] = hash_password(password)
        await self._write(user, changes)

    async def _write(self, user: Record, changes: dict[str, Any]) -> None:
        """Update the user row, never letting bookkeeping break a sign-in.

        A read-only user store, or one that is briefly unreachable, should not
        stop someone signing in with the right password. The consequence -- no
        lockout counting against such a store -- is stated in the capabilities
        rather than pretended away.
        """
        try:
            await self.users.update(user.pk, changes, Ctx.system())
        except Exception:
            log.warning("could not update sign-in state for %s", user.pk, exc_info=True)

    # -- password management ------------------------------------------------

    async def change_password(self, identity: Identity, current: str, new: str) -> None:
        """Change one's own password, having proved the current one."""
        user = await self._by_subject(identity)
        if user is None:
            raise AuthError("That account no longer exists.", provider=self.name)
        if not verify_password(str(user[self.password_field]), current):
            raise AuthError("Your current password is not correct.", provider=self.name)
        if current == new:
            raise AuthError("The new password is the same as the current one.", provider=self.name)
        self._validate(new, user)
        await self._store_password(user, new)

    async def set_password(self, subject: str, new: str, *, must_change: bool = False) -> None:
        """Set a password without the old one, for a reset.

        ``must_change`` marks a temporary password: the holder is made to
        choose their own at the next sign-in, so an administrator's stopgap
        cannot quietly become someone's permanent credential.
        """
        user = await self._by_pk(subject)
        if user is None:
            raise AuthError("That account no longer exists.", provider=self.name)
        self._validate(new, user)
        await self._store_password(user, new, must_change=must_change)

    def _validate(self, password: str, user: Record) -> None:
        result = check_password(
            password,
            email=str(user.get(self.identity_field, "")),
            name=str(user.get(self.name_field, "") or ""),
            min_length=self.min_length,
        )
        if not result.ok:
            raise AuthError(result.message, provider=self.name)

    async def _store_password(
        self, user: Record, password: str, *, must_change: bool = False
    ) -> None:
        await self.users.update(
            user.pk,
            {
                self.password_field: hash_password(password),
                "password_changed_at": utcnow(),
                "must_change_password": must_change,
                # A new password ends a lockout: whoever set it has proved
                # enough, and leaving the lock in place would strand them.
                "failed_logins": 0,
                "last_failed_login": None,
                "locked_until": None,
            },
            Ctx.system(),
        )

    # -- reset links --------------------------------------------------------

    async def issue_reset_token(self, username: str) -> tuple[str, Record] | None:
        """A single-use reset link for an address, or None if there is no account.

        The caller must not reveal which it got. Whether an address has an
        account is exactly what someone probing a forgotten-password form is
        trying to learn.
        """
        if self.reset_tokens is None:
            return None
        user = await self.find_user(username)
        if user is None or not self._is_active(user):
            return None
        token = self.reset_tokens.issue(str(user.pk), str(user[self.password_field]))
        return token, user

    async def redeem_reset_token(self, token: str, new: str) -> Record | None:
        """Set a new password from a reset link. None if the link is not usable."""
        if self.reset_tokens is None:
            return None
        # Two steps, because the check that makes the link single-use needs
        # the account it names: read the signed payload for the subject, then
        # verify the whole token against that account's *current* hash.
        subject = self._peek_subject(token)
        if subject is None:
            return None
        user = await self._by_pk(subject)
        if user is None:
            return None
        if self.reset_tokens.verify(token, str(user[self.password_field])) is None:
            return None
        self._validate(new, user)
        await self._store_password(user, new)
        return user

    def _peek_subject(self, token: str) -> str | None:
        """The subject in a token whose signature and age are valid.

        Not a security decision on its own -- the fingerprint check in
        :meth:`ResetTokens.verify` is what proves the link is still current --
        but the signature and expiry are already enforced here, so a forged or
        stale token never reaches a database lookup.
        """
        if self.reset_tokens is None:
            return None
        try:
            payload = self.reset_tokens.serializer.loads(
                token, max_age=self.reset_tokens.max_age
            )
        except Exception:
            return None
        return str(payload.get("sub")) if isinstance(payload, dict) else None

    async def _by_subject(self, identity: Identity) -> Record | None:
        by_pk = await self._by_pk(identity.subject)
        if by_pk is not None:
            return by_pk
        return await self.find_user(identity.email) if identity.email else None

    async def _by_pk(self, subject: str) -> Record | None:
        try:
            return await self.users.get(subject, Ctx.system())
        except Exception:
            return None

    def _is_active(self, user: Record) -> bool:
        value = user.get(self.active_field, True)
        return value if isinstance(value, bool) else str(value).lower() not in ("0", "false", "no")

    def to_identity(self, user: Record) -> Identity:
        return Identity(
            subject=str(user.pk),
            email=str(user.get(self.identity_field, "")),
            display_name=str(user.get(self.name_field, "") or ""),
            roles=frozenset(_parse_roles(user.get(self.roles_field))),
            provider=self.name,
            timezone=str(user.get(self.timezone_field) or "UTC"),
        )

    async def health(self) -> tuple[bool, str]:
        try:
            page = await self.users.list(ListQuery(page_size=1), Ctx.system())
            return True, f"user store reachable ({page.total if page.total is not None else '?'} accounts)"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"


def _flag(value: Any) -> bool:
    """A boolean from whatever the backend stored -- 1, "true", True."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _parse_roles(value: Any) -> tuple[str, ...]:
    """Roles may be stored as a list, a JSON array, or a comma-separated string."""
    if not value:
        return ()
    if isinstance(value, (list, tuple, set)):
        return tuple(str(v) for v in value)
    text = str(value).strip()
    if text.startswith("["):
        import json

        try:
            return tuple(str(v) for v in json.loads(text))
        except (ValueError, TypeError):
            pass
    return tuple(part.strip() for part in text.split(",") if part.strip())


def generate_password(length: int = 16) -> str:
    """A readable random password, for seeding and password resets."""
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))
