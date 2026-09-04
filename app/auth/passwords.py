"""Password rules, reset links and lockout state.

Three separate concerns that share one subject, kept together because they are
easier to reason about side by side than scattered through the auth provider.

The rules follow current guidance rather than the older folklore: **length is
what matters**, character-class requirements mostly produce ``Password1!`` and
a sticky note, and forced rotation makes people pick worse passwords. So there
is a generous minimum length, a check against passwords that are obviously the
user's own name or address, a small blocklist of the passwords attackers try
first -- and no expiry.

Reset links carry their own proof rather than a row in a table. The signature
covers the user's current password hash, so a link stops working the moment the
password changes: used once, and superseded by any other reset. That removes a
table, a cleanup job, and the bug where a forgotten token stays valid for a
year.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.clock import utcnow

#: Long enough to matter, short enough that people will not refuse. Twelve is
#: the number most current guidance settles on for a password that is not
#: backed by a second factor.
MIN_LENGTH = 12
MAX_LENGTH = 128

#: The passwords guessed first. Not a substitute for rate limiting -- it is
#: there so that "the password is Password123" fails at the moment of choosing
#: rather than at the moment of breach.
COMMON = frozenset(
    ["password", "password1", "password123", "passw0rd", "letmein", "welcome", "welcome1", "admin", "admin123", "qwerty", "qwerty123", "123456", "1234567", "12345678", "123456789", "1234567890", "111111", "000000", "iloveyou", "dragon", "monkey", "sunshine", "princess", "football", "baseball", "starwars", "master", "superman", "trustno1", "whatever", "changeme", "abc123", "abcd1234", "test1234", "secret", "summer", "winter", "spring", "autumn", "january", "temporary", "temp1234", "pass1234", "letmein123", "hello123"]
)


@dataclass(frozen=True, slots=True)
class PasswordCheck:
    """The outcome of checking a proposed password."""

    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def message(self) -> str:
        """One sentence, for a form that shows a single error."""
        return " ".join(self.problems)


def check_password(
    password: str, *, email: str = "", name: str = "", min_length: int = MIN_LENGTH
) -> PasswordCheck:
    """Judge a proposed password, returning every problem at once.

    Every problem rather than the first: telling someone their password is too
    short, watching them fix it, and then telling them it is also too common is
    a worse experience than saying both up front.
    """
    problems: list[str] = []
    stripped = password.strip()

    if len(password) < min_length:
        problems.append(f"Use at least {min_length} characters.")
    if len(password) > MAX_LENGTH:
        problems.append(f"Use at most {MAX_LENGTH} characters.")
    if stripped and stripped.lower() in COMMON:
        problems.append("That is one of the most commonly used passwords.")
    if _is_repetitive(stripped):
        problems.append("Use something less repetitive.")

    # A password built from the account it protects is the first thing anyone
    # who knows the address will try. The whole address counts however short
    # it is -- "kim@example.com-secret" is not a secret from anyone who knows
    # the address -- while the local part alone needs a few characters before
    # matching it means anything.
    candidates = [email, name, *(p for p in _name_parts(email, name) if len(p) >= 4)]
    for personal in candidates:
        if personal and personal.lower() in password.lower():
            problems.append("Do not use your name or email address in your password.")
            break

    return PasswordCheck(tuple(problems))


def _name_parts(email: str, name: str) -> list[str]:
    """The pieces of an address or name someone might build a password from.

    The domain is deliberately not one of them. Everyone in an organisation
    shares it, so rejecting it would block a common English word for every
    employee of, say, ``orange.com`` -- a rule people work around rather than
    learn from.
    """
    local_part = email.split("@")[0] if email else ""
    return [p for p in (local_part, *name.split()) if p]


def _is_repetitive(password: str) -> bool:
    """``aaaaaaaaaaaa`` and ``abcabcabcabc`` are long without being strong."""
    if not password:
        return False
    if len(set(password)) <= 2:
        return True
    # A short unit repeated to fill the length requirement.
    for size in range(1, 5):
        unit = password[:size]
        if len(unit) < size:
            break
        if unit * (len(password) // size) == password[: len(password) // size * size] and (
            len(password) // size >= 3
        ):
            return True
    return False


def strength(password: str) -> int:
    """A coarse 0-4 score, for the meter on the form.

    Deliberately crude. Its job is to encourage a longer password, not to
    estimate entropy -- a precise-looking number would imply a guarantee this
    cannot make.
    """
    if not password:
        return 0
    score = 0
    for threshold in (8, 12, 16, 20):
        if len(password) >= threshold:
            score += 1
    variety = sum(
        bool(re.search(pattern, password))
        for pattern in (r"[a-z]", r"[A-Z]", r"\d", r"[^\w\s]")
    )
    if variety >= 3 and score < 4:
        score += 1
    if password.strip().lower() in COMMON:
        return 0
    return min(score, 4)


# -- reset links -------------------------------------------------------------

#: Distinct from the session signer's salt, so a session cookie can never be
#: replayed as a reset token or the reverse.
RESET_SALT = "crm.password.reset"


class ResetTokens:
    """Signs and verifies password-reset links."""

    def __init__(self, secret_key: str, *, max_age: int = 3600) -> None:
        self.serializer = URLSafeTimedSerializer(secret_key, salt=RESET_SALT)
        self.max_age = max_age

    def issue(self, subject: str, password_hash: str) -> str:
        """A token for one account, valid until that account's password changes.

        The current hash goes into the payload, not just the user id: verifying
        compares it against the hash stored now, so changing the password --
        by using an earlier link, or any other way -- invalidates every
        outstanding link at once.
        """
        return self.serializer.dumps({"sub": subject, "pw": _fingerprint(password_hash)})

    def verify(self, token: str, password_hash: str) -> str | None:
        """The subject this token is for, or None if it is not usable.

        None covers every failure -- expired, tampered with, already used --
        because the caller shows the same message for all of them anyway, and
        distinguishing them out loud tells an attacker which guess was closer.
        """
        try:
            payload = self.serializer.loads(token, max_age=self.max_age)
        except (SignatureExpired, BadSignature):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("pw") != _fingerprint(password_hash):
            return None
        subject = payload.get("sub")
        return str(subject) if subject else None


def _fingerprint(password_hash: str) -> str:
    """A short digest of the stored hash, so the token does not carry it."""
    import hashlib

    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


# -- lockout -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LockoutPolicy:
    """How many wrong guesses, and how long the door stays shut.

    Kept in the user row rather than in memory: an in-process counter resets
    every deploy and is per-worker, so with four workers an attacker gets four
    times the attempts. The database is the only place several workers agree.
    """

    max_attempts: int = 8
    #: How long a lock lasts. Long enough to make guessing impractical, short
    #: enough that a real person who mistyped is not calling support.
    lock_minutes: int = 15
    #: Failures older than this no longer count towards a lock, so someone who
    #: mistypes once a month never accumulates their way into a lockout.
    window_minutes: int = 60

    @property
    def enabled(self) -> bool:
        return self.max_attempts > 0

    def is_locked(self, locked_until: datetime | None, now: datetime | None = None) -> bool:
        if locked_until is None:
            return False
        return locked_until > (now or utcnow())

    def next_state(
        self, failures: int, last_failure: datetime | None, now: datetime | None = None
    ) -> tuple[int, datetime | None]:
        """The counter and lock expiry after one more failed attempt."""
        now = now or utcnow()
        if last_failure is not None and now - last_failure > timedelta(
            minutes=self.window_minutes
        ):
            failures = 0
        failures += 1
        if self.enabled and failures >= self.max_attempts:
            return failures, now + timedelta(minutes=self.lock_minutes)
        return failures, None

    def wait_message(self, locked_until: datetime, now: datetime | None = None) -> str:
        minutes = max(1, round((locked_until - (now or utcnow())).total_seconds() / 60))
        unit = "minute" if minutes == 1 else "minutes"
        return (
            f"Too many failed sign-in attempts. Try again in {minutes} {unit}, "
            f"or ask an administrator to reset your password."
        )
