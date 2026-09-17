"""Low-level inserts shared by the two paths that create a booking.

`holds.confirm` (a caller on the phone) and `bookings.request_overflow` (a
waitlist request) both need to mint a reference and find-or-create a guest.
They live here rather than in either module so neither has to import the
other, and so there is exactly one piece of code that decides what a booking
row looks like.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import Connection
from sqlalchemy.exc import IntegrityError

from . import tokens
from .businesses import Business
from .db import fetch_one

log = logging.getLogger(__name__)

#: References collide roughly once in a billion at six characters. Eight tries
#: is far past the point where a collision means a bug rather than bad luck.
REFERENCE_ATTEMPTS = 8


@dataclass(frozen=True)
class Booking:
    id: UUID
    reference: str
    business_id: UUID
    start_time: datetime
    end_time: datetime
    party_size: int
    status: str
    name: str
    phone: str
    notes: str
    manage_token: str | None = None
    customer_id: UUID | None = None


def insert_booking(
    conn: Connection,
    *,
    business: Business,
    customer_id: UUID | None,
    start: datetime,
    end: datetime,
    units: int,
    name: str,
    phone: str,
    notes: str,
    source: str,
    status: str,
    slot_starts: list[datetime],
    manage_token: str | None,
) -> Booking:
    """Insert with a fresh reference, retrying on collision inside the transaction.

    Retry on the unique violation rather than SELECT-then-INSERT: a pre-check
    is a race by construction (PRD §17), and the retry path is both correct and
    almost never taken.

    The retry needs a SAVEPOINT. Without one the failed INSERT aborts the whole
    transaction, and the second attempt would run against a connection that
    refuses everything -- so the collision that the retry exists to survive
    would instead lose the booking.
    """
    expires = end + timedelta(days=1)
    for attempt in range(REFERENCE_ATTEMPTS):
        reference = tokens.new_reference()
        try:
            with conn.begin_nested():
                row = fetch_one(
                    conn,
                    """
                    INSERT INTO bookings (business_id, customer_id, reference, start_time,
                        end_time, party_size, status, source, name, phone, notes,
                        manage_token_hash, manage_token_expires_at, slot_starts)
                    VALUES (:b, :cust, :ref, :start, :end, :units, :status, :source,
                        :name, :phone, :notes, :token, :token_exp, :starts)
                    RETURNING id
                    """,
                    b=str(business.id),
                    cust=str(customer_id) if customer_id else None,
                    ref=reference,
                    start=start,
                    end=end,
                    units=units,
                    status=status,
                    source=source,
                    name=(name or "").strip(),
                    phone=(phone or "").strip(),
                    notes=(notes or "").strip(),
                    token=tokens.hash_secret(manage_token) if manage_token else None,
                    token_exp=expires if manage_token else None,
                    starts=sorted(slot_starts),
                )
                assert row is not None
        except IntegrityError:
            if attempt == REFERENCE_ATTEMPTS - 1:
                raise
            log.warning("booking reference %s collided; retrying", reference)
            continue
        return Booking(
            id=row.id,
            reference=reference,
            business_id=business.id,
            start_time=start,
            end_time=end,
            party_size=units,
            status=status,
            name=(name or "").strip(),
            phone=(phone or "").strip(),
            notes=(notes or "").strip(),
            manage_token=manage_token,
            customer_id=customer_id,
        )
    raise RuntimeError("could not mint a unique booking reference")


def upsert_customer(
    conn: Connection, business_id: UUID, phone: str, name: str
) -> UUID | None:
    """Find or create the guest. No phone means no guest record, not an error.

    A withheld number is ordinary on a phone line, and refusing the booking
    over it would turn a caller into a walk-in.
    """
    phone = (phone or "").strip()
    if not phone:
        return None
    row = fetch_one(
        conn,
        "INSERT INTO customers (business_id, phone_e164, name) VALUES (:b, :p, :n)"
        " ON CONFLICT (business_id, phone_e164) DO UPDATE SET"
        # Never overwrite a known name with an empty one: a caller who does not
        # give a name this time is still the person who gave one last time.
        "   name = CASE WHEN EXCLUDED.name = '' THEN customers.name ELSE EXCLUDED.name END,"
        "   updated_at = now()"
        " RETURNING id",
        b=str(business_id),
        p=phone,
        n=(name or "").strip(),
    )
    return row.id if row else None
