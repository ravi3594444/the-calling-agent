"""Secrets that travel in URLs: the dashboard link and the manage link.

PRD §12: the manage link is 32+ random characters, stored hashed and expiring
after the booking date. The 6-character reference is NOT a credential and
authorises nothing -- it is read aloud on the phone, printed in a text and
repeated in a noisy room, and anything it could authorise is something a
stranger in earshot could do.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import string

from .config import settings

#: No O/0 or I/1: every one of these gets read aloud down a phone line.
REFERENCE_ALPHABET = "".join(
    c for c in string.ascii_uppercase + string.digits if c not in "O0I1"
)
REFERENCE_LENGTH = 6


def new_reference() -> str:
    """A 6-character human-readable code. Uniqueness is the database's job.

    `secrets`, not `random`: references appear in messages and support
    conversations, and a predictable sequence would let anyone enumerate a
    venue's bookings by reading one text.
    """
    return "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(REFERENCE_LENGTH))


def new_secret(length: int = 32) -> str:
    """A URL-safe secret. `length` is bytes of entropy, not characters."""
    return secrets.token_urlsafe(length)


def hash_secret(secret: str) -> str:
    """Peppered SHA-256. A database dump alone opens nobody's dashboard.

    The pepper lives in the environment, not the database, so the two have to
    leak together. Empty is allowed and degrades to a plain hash: a deployment
    that has not set one is still not storing link secrets in the clear.
    """
    return hashlib.sha256((settings.token_pepper + secret).encode("utf-8")).hexdigest()


def secrets_match(candidate_hash: str, stored_hash: str) -> bool:
    return hmac.compare_digest(candidate_hash, stored_hash)
