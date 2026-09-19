"""Holds: claiming capacity before the details exist (PRD §7).

The order matters more than the code. The agent takes the hold the MOMENT a
caller states a time, and only then asks for a name and a number. Collecting
first and discovering the slot went while they spelled their surname is the
primary failure mode of every phone booking system, and it is the one this
module exists to prevent.

Holds live in Postgres beside the counters they move. Splitting them into
Redis would put the claim and the capacity in two stores that fail
independently, and the first symptom of that is a double booking.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from . import availability, businesses, records, slots, tokens
from .businesses import Business
from .db import fetch_one, transaction
from .records import Booking

log = logging.getLogger(__name__)


def _our_call_id(call_id: UUID | str | None) -> str | None:
    """Our own `calls.id`, or nothing. Never someone else's identifier.

    `holds.call_id` is a uuid column naming a row in our `calls` table. A
    provider's opaque tool-call id once arrived here under the same name and
    Postgres rejected it, which cost a real caller their table -- the hold is
    the booking, and it died over a field that is only ever used to say which
    conversation produced it.

    So the value is checked here rather than trusted from the caller, and a
    value that is not ours is dropped with a warning instead of raised. Losing
    the link between a call and its booking is a worse dashboard; losing the
    hold is a worse restaurant.
    """
    if call_id is None:
        return None
    try:
        return str(UUID(str(call_id)))
    except ValueError:
        log.warning("ignoring a call_id that is not one of ours: %r", call_id)
        return None


class HoldError(Exception):
    """Base for every refusal a caller of this module must handle."""


class HoldNotFound(HoldError):
    """No such hold for that business. Never leaks whether it exists elsewhere."""


class HoldExpired(HoldError):
    """The claim lapsed. Its capacity is already back on sale."""


@dataclass(frozen=True)
class HoldResult:
    ok: bool
    hold_id: UUID | None = None
    expires_at: datetime | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    units: int = 0
    reason: str = availability.OK
    alternatives: tuple[datetime, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)


# --- taking a hold -----------------------------------------------------------


def take(
    business: Business | UUID | str,
    start: datetime,
    units: int,
    *,
    ttl_seconds: int | None = None,
    call_id: UUID | str | None = None,
    allow_overflow: bool = False,
) -> HoldResult:
    """Claim `units` at `start` for a few minutes, or say why not.

    The availability question is asked TWICE: once outside the lock to decide
    whether to bother, and once inside it holding every slot row the turn
    covers. Only the second answer is worth anything -- the first is a guess
    that was true when it was asked.

    `allow_overflow` is the owner deliberately going past the walk-in buffer
    when accepting a waitlist request (PRD §11). It is never set by the agent.
    """
    business = _business(business)
    start = start.astimezone(UTC)
    ttl = int(ttl_seconds or business.config["policy"]["hold_ttl_seconds"])

    verdict = availability.is_available(business, start, units)
    if not verdict.ok and not allow_overflow:
        return HoldResult(
            ok=False,
            reason=verdict.reason,
            alternatives=verdict.alternatives,
            start_time=start,
            units=units,
            detail=verdict.detail,
        )

    service = availability.service_for(business, start)
    if service is None:
        # Capacity is off or unconfigured: there is nothing to contend for, so
        # the hold is a bookkeeping row and the counters stay untouched.
        turn = int(business.config["capacity"]["turn_minutes"])
        end = start + timedelta(minutes=turn)
        with transaction() as conn:
            hold_id = _insert_hold(conn, business.id, start, [], units, end, ttl, call_id)
        return HoldResult(
            ok=True, hold_id=hold_id, expires_at=_expiry(ttl), start_time=start,
            end_time=end, units=units,
        )

    turn_starts = availability.turn_slots(service, start)
    end = start + timedelta(minutes=service.turn_minutes)
    room = availability.sellable(service, business)

    hold_id: UUID | None = None
    free = 0
    with transaction() as conn:
        rows = slots.lock(
            conn,
            business.id,
            turn_starts,
            capacity_total=service.total_units,
            slot_minutes=service.slot_minutes,
        )
        free = slots.headroom(rows, room)
        if free >= units or allow_overflow:
            slots.add(conn, business.id, turn_starts, units)
            hold_id = _insert_hold(
                conn, business.id, start, turn_starts, units, end, ttl, call_id
            )
        # Losing the race is not an error and nothing needs undoing: the only
        # writes so far are zero-filled slot rows, which are true either way.
        # The transaction closes HERE, before alternatives are searched, so the
        # loser's consolation query is not run while holding the winner's rows.

    if hold_id is None:
        return HoldResult(
            ok=False,
            reason=availability.FULL,
            alternatives=availability.alternatives_for(business, start, units),
            start_time=start,
            units=units,
            detail={"free": max(0, free), "capacity": room},
        )

    return HoldResult(
        ok=True,
        hold_id=hold_id,
        expires_at=_expiry(ttl),
        start_time=start,
        end_time=end,
        units=units,
    )


def _insert_hold(
    conn: Connection,
    business_id: UUID,
    start: datetime,
    turn_starts: list[datetime],
    units: int,
    end: datetime,
    ttl: int,
    call_id: UUID | str | None,
) -> UUID:
    row = fetch_one(
        conn,
        "INSERT INTO holds (business_id, slot_start, slot_starts, units, end_time,"
        " expires_at, call_id)"
        " VALUES (:b, :s, :starts, :u, :end, now() + make_interval(secs => :ttl), :call)"
        " RETURNING id",
        b=str(business_id),
        s=start,
        starts=sorted(turn_starts),
        u=units,
        end=end,
        ttl=ttl,
        call=_our_call_id(call_id),
    )
    assert row is not None
    return row.id


def _expiry(ttl: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=ttl)


# --- confirming --------------------------------------------------------------


def confirm(
    business: Business | UUID | str,
    hold_id: UUID | str,
    *,
    name: str,
    phone: str = "",
    notes: str = "",
    source: str = "voice",
    status: str = "confirmed",
    call_id: UUID | str | None = None,
) -> Booking:
    """Turn a live hold into a booking, re-validating that it is still live.

    NEVER on the strength of an earlier check_availability (PRD §7). The hold
    row is locked, its expiry re-read from the database clock, and only then
    does a booking exist. Between the caller saying yes and this line running,
    a sweep may have taken the capacity back, and telling them to come anyway
    is the one outcome worse than saying no.

    Idempotent: a retried tool call returns the booking the first one made.
    The agent's transport can and does deliver the same call twice.
    """
    business = _business(business)
    with transaction() as conn:
        held = fetch_one(
            conn,
            "SELECT id, business_id, slot_start, slot_starts, units, end_time, expires_at,"
            " released_at, converted_booking_id FROM holds"
            " WHERE id = :h AND business_id = :b FOR UPDATE",
            h=str(hold_id),
            b=str(business.id),
        )
        if held is None:
            raise HoldNotFound(str(hold_id))

        if held.converted_booking_id:
            existing = _read_booking(conn, business.id, held.converted_booking_id)
            if existing is not None:
                return existing

        if held.released_at is not None:
            raise HoldExpired(str(hold_id))
        now = conn.execute(text("SELECT now()")).scalar_one()
        if held.expires_at <= now:
            raise HoldExpired(str(hold_id))

        customer_id = records.upsert_customer(conn, business.id, phone, name)
        manage_token = tokens.new_secret()
        booking = records.insert_booking(
            conn,
            business=business,
            customer_id=customer_id,
            start=held.slot_start,
            end=held.end_time,
            units=held.units,
            name=name,
            phone=phone,
            notes=notes,
            source=source,
            status=status,
            slot_starts=list(held.slot_starts or []),
            manage_token=manage_token,
        )
        conn.execute(
            text("UPDATE holds SET converted_booking_id = :bk WHERE id = :h"),
            {"bk": str(booking.id), "h": str(held.id)},
        )
        if (ours := _our_call_id(call_id)) is not None:
            conn.execute(
                text("UPDATE calls SET booking_id = :bk WHERE id = :c AND business_id = :b"),
                {"bk": str(booking.id), "c": ours, "b": str(business.id)},
            )
    return booking


def release(business: Business | UUID | str, hold_id: UUID | str) -> bool:
    """Give a hold's capacity back early. Safe to call twice."""
    business = _business(business)
    with transaction() as conn:
        held = fetch_one(
            conn,
            "SELECT id, slot_starts, units, released_at, converted_booking_id FROM holds"
            " WHERE id = :h AND business_id = :b FOR UPDATE",
            h=str(hold_id),
            b=str(business.id),
        )
        if held is None:
            raise HoldNotFound(str(hold_id))
        if held.released_at is not None or held.converted_booking_id is not None:
            return False
        slots.give_back(conn, business.id, list(held.slot_starts or []), held.units)
        conn.execute(
            text("UPDATE holds SET released_at = now() WHERE id = :h"), {"h": str(held.id)}
        )
    return True


# --- the sweeper -------------------------------------------------------------


def sweep_expired(limit: int = 500) -> int:
    """Return the capacity of every lapsed hold. Runs every minute.

    Without this the room fills with ghosts: capacity committed by callers who
    hung up mid-sentence, never released, and the agent turning real people
    away from an empty restaurant.

    `FOR UPDATE SKIP LOCKED` so two workers sweeping at once share the work
    instead of queueing behind each other -- and never double-release, because
    a row one worker holds is invisible to the other.
    """
    released = 0
    with transaction() as conn:
        rows = conn.execute(
            text(
                "SELECT id, business_id, slot_starts, units FROM holds"
                " WHERE released_at IS NULL AND converted_booking_id IS NULL"
                "   AND expires_at <= now()"
                " ORDER BY expires_at LIMIT :limit FOR UPDATE SKIP LOCKED"
            ),
            {"limit": limit},
        ).fetchall()
        for row in rows:
            slots.give_back(conn, row.business_id, list(row.slot_starts or []), row.units)
            conn.execute(
                text("UPDATE holds SET released_at = now() WHERE id = :h"), {"h": str(row.id)}
            )
            released += 1
    if released:
        log.info("swept %d expired hold(s)", released)
    return released


def expire_now(hold_id: UUID | str) -> None:
    """Age a hold past its expiry. For tests and for support, never for callers."""
    with transaction() as conn:
        conn.execute(
            text("UPDATE holds SET expires_at = now() - interval '1 second' WHERE id = :h"),
            {"h": str(hold_id)},
        )


def _read_booking(conn: Connection, business_id: UUID, booking_id: UUID) -> Booking | None:
    row = fetch_one(
        conn,
        "SELECT id, reference, start_time, end_time, party_size, status, name, phone,"
        " notes, customer_id FROM bookings WHERE id = :i AND business_id = :b",
        i=str(booking_id),
        b=str(business_id),
    )
    if row is None:
        return None
    return Booking(
        id=row.id,
        reference=row.reference,
        business_id=business_id,
        start_time=row.start_time,
        end_time=row.end_time,
        party_size=row.party_size,
        status=row.status,
        name=row.name,
        phone=row.phone,
        notes=row.notes,
        customer_id=row.customer_id,
    )


def _business(value: Business | UUID | str) -> Business:
    return value if isinstance(value, Business) else businesses.by_id(value)
