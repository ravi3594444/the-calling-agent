"""Owner accounts: the way back in when the link is gone.

WHAT THIS IS NOT
It is not a replacement for the dashboard link, and it does not put a login in
front of service. PRD §14 is still right: a host seating guests on a shared
tablet should not be typing a password, so the saved link keeps working
exactly as before and nothing downstream of `current_business` changes.

What an account fixes is the one thing a bearer link cannot: the token lives
in one browser's localStorage and only its hash is stored, so clearing site
data, or picking up a different device, used to mean the venue was locked out
and somebody had to mint a new link from a shell. Signing in mints an ordinary
dashboard token and sets it as a cookie. The account is a way to obtain a
link, not a second kind of credential.

PASSWORDS ARE NOT TOKENS
`tokens.hash_secret` is a single peppered SHA-256, which is correct for 32
random bytes and wrong for a password somebody chose: a fast hash over a
guessable input is a wordlist away from an open dashboard. These use scrypt,
salted per row, with the same pepper on top so that a database dump alone
still opens nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text as _text

from .config import settings
from .db import fetch_one, transaction

#: scrypt cost. n=2**14 with r=8 is a few tens of milliseconds and ~16MB per
#: hash: slow enough to ruin an offline wordlist, fast enough that a login does
#: not feel broken, and cheap enough that a burst of them cannot exhaust the
#: box. Stored per hash, so raising it later leaves old rows verifiable.
_N, _R, _P = 2**14, 8, 1
_DK_LEN = 32


class OwnerError(ValueError):
    """An account the product refuses to create."""


@dataclass(frozen=True)
class Owner:
    id: UUID
    business_id: UUID
    email: str


# --- passwords ---------------------------------------------------------------


def hash_password(password: str) -> str:
    """scrypt, salted per row and peppered from the environment.

    Self-describing (`scrypt$n$r$p$salt$key`) so the cost can be raised without
    invalidating every existing password: verification reads the parameters the
    hash was made with rather than today's.
    """
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(
        (password + settings.token_pepper).encode("utf-8"),
        salt=salt,
        n=_N,
        r=_R,
        p=_P,
        dklen=_DK_LEN,
        maxmem=64 * 1024 * 1024,
    )
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${key.hex()}"


def password_matches(password: str, stored: str) -> bool:
    """Constant-time check against whatever parameters that hash was made with.

    A malformed or unknown hash is False, never an exception: a row somebody
    edited by hand should fail the login, not 500 the login page.
    """
    try:
        scheme, n, r, p, salt_hex, key_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(
            (password + settings.token_pepper).encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(key_hex)),
            maxmem=64 * 1024 * 1024,
        )
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(candidate.hex(), key_hex)


#: Short enough that people will actually use it, long enough that scrypt is
#: doing work rather than covering for a four-character password.
MIN_PASSWORD = 10


def check_password_is_usable(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise OwnerError(f"A password needs at least {MIN_PASSWORD} characters.")


def normalise_email(email: str) -> str:
    """Trimmed and lowercased. The unique index is on lower(email), so storing
    what they typed and matching case-insensitively would let "Rosa@" and
    "rosa@" disagree about which one exists."""
    return email.strip().lower()


def looks_like_an_email(email: str) -> bool:
    """Deliberately shallow. Nothing is sent to this address yet, so the only
    mistakes worth catching are the ones a person can see they made."""
    local, _, domain = email.partition("@")
    return bool(local) and "." in domain and " " not in email and domain[-1] != "."


# --- accounts ----------------------------------------------------------------


def email_taken(email: str) -> bool:
    """Asked BEFORE a venue is created, so a taken address costs nothing.

    `create` checks again under the unique index, because between these two
    questions somebody else can answer them differently.
    """
    with transaction() as conn:
        return (
            fetch_one(
                conn, "SELECT id FROM owners WHERE lower(email) = :e", e=normalise_email(email)
            )
            is not None
        )


def create(business_id: UUID | str, *, email: str, password: str) -> Owner:
    """Give a business an owner who can sign in. Validated before a row exists."""
    address = normalise_email(email)
    if not looks_like_an_email(address):
        raise OwnerError("That does not look like an email address.")
    check_password_is_usable(password)

    with transaction() as conn:
        if fetch_one(conn, "SELECT id FROM owners WHERE lower(email) = :e", e=address):
            raise OwnerError("There is already an account with that email.")
        row = fetch_one(
            conn,
            "INSERT INTO owners (business_id, email, password_hash)"
            " VALUES (:b, :e, :h) RETURNING id",
            b=str(business_id),
            e=address,
            h=hash_password(password),
        )
        assert row is not None
    return Owner(id=row.id, business_id=UUID(str(business_id)), email=address)


def authenticate(email: str, password: str) -> Owner | None:
    """The account for these credentials, or None.

    None for every kind of failure, and the caller says only "email or password
    is wrong": distinguishing them turns the login form into a way to ask which
    venues exist.

    The password is verified even when no account matches, against a hash of
    nothing in particular, so a wrong address and a wrong password take the
    same time to refuse. Otherwise the fast path answers "no such account".
    """
    address = normalise_email(email)
    with transaction() as conn:
        row = fetch_one(
            conn,
            "SELECT id, business_id, email, password_hash FROM owners"
            " WHERE lower(email) = :e",
            e=address,
        )
    if row is None:
        password_matches(password, _DECOY)
        return None
    if not password_matches(password, row.password_hash):
        return None

    with transaction() as conn:
        conn.execute(
            _text("UPDATE owners SET last_login_at = now() WHERE id = :i"), {"i": str(row.id)}
        )
    return Owner(id=row.id, business_id=row.business_id, email=row.email)


#: Hashed once at import so a login against an unknown address does the same
#: scrypt work as one against a real account.
_DECOY = hash_password(secrets.token_urlsafe(32))


def set_password(email: str, password: str) -> Owner:
    """Reset from the CLI -- the recovery path until something can send mail."""
    address = normalise_email(email)
    check_password_is_usable(password)
    with transaction() as conn:
        row = fetch_one(
            conn,
            "SELECT id, business_id FROM owners WHERE lower(email) = :e",
            e=address,
        )
        if row is None:
            raise OwnerError(f"No account for {address!r}.")
        conn.execute(
            _text("UPDATE owners SET password_hash = :h WHERE id = :i"),
            {"h": hash_password(password), "i": str(row.id)},
        )
    return Owner(id=row.id, business_id=row.business_id, email=address)


def for_business(business_id: UUID | str) -> list[dict[str, Any]]:
    with transaction() as conn:
        rows = conn.execute(
            _text(
                "SELECT email, created_at, last_login_at FROM owners"
                " WHERE business_id = :b ORDER BY created_at"
            ),
            {"b": str(business_id)},
        ).fetchall()
    return [
        {
            "email": row.email,
            "created_at": row.created_at,
            "last_login_at": row.last_login_at,
        }
        for row in rows
    ]


def _now() -> datetime:
    return datetime.now(UTC)
