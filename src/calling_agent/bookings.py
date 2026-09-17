"""The booking lifecycle (PRD §9) and the overflow queue (PRD §11).

    pending --accept--> confirmed --> arrived
       |                    |      \\-> no_show
       \\--decline--> declined
                            \\--cancel--> cancelled

Two rules decide everything here:

PENDING NEVER TOUCHES THE COUNTER. A waitlist request is a question, not a
table. Only `confirmed` holds capacity, which is why accepting one has to go
back through the lock and can still fail.

STATUS IS A STATE MACHINE, NOT A SET OF BOOLEANS. `cancelled` and `no_show`
and `arrived` are not flags that can all be true at once, and the transition
-- not the new value -- is what fires messages and moves counters. An `UPDATE
bookings SET status` anywhere outside this module is a bug.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from . import availability, holds, notifications, records, slots, tokens
from .businesses import Business
from .db import fetch_all, fetch_one, readonly, transaction

log = logging.getLogger(__name__)

PENDING = "pending"
CONFIRMED = "confirmed"
ARRIVED = "arrived"
NO_SHOW = "no_show"
CANCELLED = "cancelled"
DECLINED = "declined"

#: Every move the product allows, stated once. Anything else is refused by
#: name, so a client bug reads as "cannot go from cancelled to arrived"
#: instead of silently rewriting history.
TRANSITIONS: dict[str, set[str]] = {
    PENDING: {CONFIRMED, DECLINED, CANCELLED},
    CONFIRMED: {ARRIVED, NO_SHOW, CANCELLED},
    ARRIVED: {CONFIRMED, NO_SHOW},          # a host correcting a mistap
    NO_SHOW: {CONFIRMED, ARRIVED},          # "Undo" on the dashboard
    CANCELLED: set(),
    DECLINED: set(),
}

#: Statuses that occupy capacity. The counter is moved when a booking enters
#: or leaves this set, and never otherwise.
HOLDS_CAPACITY = {CONFIRMED, ARRIVED}


class BookingError(Exception):
    """A refusal the caller must show somebody."""


class BookingNotFound(BookingError):
    pass


class IllegalTransition(BookingError):
    pass


class NoCapacity(BookingError):
    """Accepting would need units that are not there."""


@dataclass(frozen=True)
class BookingRow:
    id: UUID
    business_id: UUID
    reference: str
    start_time: datetime
    end_time: datetime
    party_size: int
    status: str
    name: str
    phone: str
    notes: str
    source: str
    slot_starts: list[datetime]
    customer_id: UUID | None
    created_at: datetime
    visits: int = 0
    no_shows: int = 0

    def as_dict(self, business: Business) -> dict[str, Any]:
        from . import formatting

        return {
            "id": str(self.id),
            "reference": self.reference,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "time": formatting.time_str(business, self.start_time),
            "date": self.start_time.astimezone(business.tz).date().isoformat(),
            "party_size": self.party_size,
            "status": self.status,
            "name": self.name,
            "phone": self.phone,
            "notes": self.notes,
            "source": self.source,
            "visits": self.visits,
            "no_shows": self.no_shows,
            "created_at": self.created_at.isoformat(),
        }


_SELECT = """
SELECT b.id, b.business_id, b.reference, b.start_time, b.end_time, b.party_size,
       b.status, b.name, b.phone, b.notes, b.source, b.slot_starts, b.customer_id,
       b.created_at, COALESCE(c.visit_count, 0) AS visits,
       COALESCE(c.no_show_count, 0) AS no_shows
  FROM bookings b LEFT JOIN customers c ON c.id = b.customer_id
"""


def _row(r: Any) -> BookingRow:
    return BookingRow(
        id=r.id,
        business_id=r.business_id,
        reference=r.reference,
        start_time=r.start_time,
        end_time=r.end_time,
        party_size=r.party_size,
        status=r.status,
        name=r.name,
        phone=r.phone,
        notes=r.notes,
        source=r.source,
        slot_starts=list(r.slot_starts or []),
        customer_id=r.customer_id,
        created_at=r.created_at,
        visits=getattr(r, "visits", 0) or 0,
        no_shows=getattr(r, "no_shows", 0) or 0,
    )


# --- reading -----------------------------------------------------------------


def by_id(business: Business | UUID | str, booking_id: UUID | str) -> BookingRow:
    business_id = _business_id(business)
    with readonly() as conn:
        row = fetch_one(
            conn, _SELECT + " WHERE b.id = :i AND b.business_id = :b",
            i=str(booking_id), b=str(business_id),
        )
    if row is None:
        raise BookingNotFound(str(booking_id))
    return _row(row)


def by_reference(business: Business | UUID | str, reference: str) -> BookingRow | None:
    """References are spoken, so they arrive with spaces and in any case."""
    cleaned = (reference or "").replace(" ", "").replace("-", "").strip().upper()
    if not cleaned:
        return None
    with readonly() as conn:
        row = fetch_one(
            conn, _SELECT + " WHERE b.business_id = :b AND b.reference = :r",
            b=str(_business_id(business)), r=cleaned,
        )
    return _row(row) if row else None


def upcoming_for_phone(
    business: Business | UUID | str, phone: str, *, limit: int = 1
) -> list[BookingRow]:
    """The caller's next booking.

    PRD §8: mention only the next upcoming one, even if several exist. Never
    read a list of somebody's bookings down the phone -- a number can be
    reassigned, shared, or overheard.
    """
    if not phone:
        return []
    with readonly() as conn:
        rows = fetch_all(
            conn,
            _SELECT + " WHERE b.business_id = :b AND b.phone = :p AND b.start_time > now()"
            "   AND b.status IN ('confirmed', 'pending')"
            " ORDER BY b.start_time LIMIT :limit",
            b=str(_business_id(business)), p=phone, limit=limit,
        )
    return [_row(r) for r in rows]


def between(
    business: Business | UUID | str,
    first: datetime,
    last: datetime,
    *,
    statuses: tuple[str, ...] | None = None,
) -> list[BookingRow]:
    clause = " AND b.status = ANY(:statuses)" if statuses else ""
    params: dict[str, Any] = {
        "b": str(_business_id(business)), "first": first, "last": last
    }
    if statuses:
        params["statuses"] = list(statuses)
    with readonly() as conn:
        rows = fetch_all(
            conn,
            _SELECT + " WHERE b.business_id = :b AND b.start_time >= :first"
            " AND b.start_time < :last" + clause + " ORDER BY b.start_time, b.name",
            **params,
        )
    return [_row(r) for r in rows]


def on_local_date(business: Business, local_date: date) -> list[BookingRow]:
    """A venue's "tonight" is its service, not midnight to midnight.

    A booking taken for 00:30 after a Friday that runs to two in the morning
    belongs to Friday's list. Anchoring on the service windows rather than the
    calendar day is what puts it there.
    """
    services = availability.services_on(business, local_date)
    if services:
        first = min(s.starts_at for s in services)
        last = max(s.ends_at for s in services)
    else:
        first = datetime.combine(local_date, time(0), tzinfo=business.tz).astimezone(UTC)
        last = first + timedelta(days=1)
    return between(business, first, last)


def pending_requests(business: Business | UUID | str) -> list[BookingRow]:
    with readonly() as conn:
        rows = fetch_all(
            conn,
            _SELECT + " WHERE b.business_id = :b AND b.status = 'pending'"
            "   AND b.start_time > now() ORDER BY b.created_at",
            b=str(_business_id(business)),
        )
    return [_row(r) for r in rows]


# --- creating ----------------------------------------------------------------


def create_direct(
    business: Business,
    *,
    start: datetime,
    units: int,
    name: str,
    phone: str = "",
    notes: str = "",
    source: str = "dashboard",
    allow_overflow: bool = False,
) -> BookingRow:
    """Book without a conversation: the dashboard, or another system's API.

    Goes through hold-and-confirm rather than inserting a row, so a booking
    typed by a host contends for capacity on exactly the same terms as one
    taken by the agent. A second path to the counter is a second chance to get
    it wrong.
    """
    held = holds.take(business, start, units, ttl_seconds=60, allow_overflow=allow_overflow)
    if not held.ok:
        raise NoCapacity(held.reason)
    booking = holds.confirm(
        business, held.hold_id, name=name, phone=phone, notes=notes, source=source
    )
    with transaction() as conn:
        notifications.queue_for_booking(
            conn, business, booking, "confirmed", manage_token=booking.manage_token
        )
        notifications.schedule_reminder(conn, business, booking)
    return by_id(business, booking.id)


def request_overflow(
    business: Business,
    *,
    start: datetime,
    units: int,
    name: str,
    phone: str = "",
    notes: str = "",
    source: str = "voice",
) -> tuple[BookingRow, datetime]:
    """Join the waitlist. Writes `pending`; the counter is not touched.

    Returns the booking and the deadline the caller was promised, because the
    agent has to say it out loud: nobody waits without knowing they are
    waiting (PRD §10).
    """
    policy = business.config["policy"]
    if not business.config["features"].get("overflow", True):
        raise BookingError("overflow is switched off for this business")

    deadline = overflow_deadline(business, datetime.now(UTC))
    with transaction() as conn:
        waiting = fetch_one(
            conn,
            "SELECT count(*) AS n FROM bookings WHERE business_id = :b AND status = 'pending'"
            " AND start_time = :s",
            b=str(business.id), s=start,
        )
        if waiting and waiting.n >= int(policy["max_pending_per_slot"]):
            raise BookingError("that time already has as many people waiting as we take")

        customer_id = records.upsert_customer(conn, business.id, phone, name)
        token = tokens.new_secret()
        turn = _turn_minutes(business, start)
        booking = records.insert_booking(
            conn,
            business=business,
            customer_id=customer_id,
            start=start,
            end=start + timedelta(minutes=turn),
            units=units,
            name=name,
            phone=phone,
            notes=notes,
            source=source,
            status=PENDING,
            slot_starts=[],
            manage_token=token,
        )
        notifications.queue_for_booking(conn, business, booking, "pending", manage_token=token)
        notifications.queue_outbound_task(
            conn,
            business,
            reason="overflow_timeout",
            booking_id=booking.id,
            scheduled_for=deadline,
            dedupe_key=f"overflow:{booking.id}",
            payload={"deadline": deadline.isoformat()},
        )
    return by_id(business, booking.id), deadline


def overflow_deadline(business: Business, now: datetime) -> datetime:
    """"Within the hour, or by six" -- whichever comes first (PRD §10).

    The same-day cutoff wins because a request for tonight answered at 9pm is
    not an answer, and a request for next Tuesday does not need one by six.
    """
    policy = business.config["policy"]
    by_timeout = now + timedelta(minutes=int(policy["overflow_timeout_minutes"]))
    cutoff_time = policy["overflow_cutoff_local_time"]
    hour, minute = (int(p) for p in cutoff_time.split(":")[:2])
    local_now = now.astimezone(business.tz)
    cutoff = datetime.combine(local_now.date(), time(hour, minute), tzinfo=business.tz)
    cutoff_utc = cutoff.astimezone(UTC)
    if cutoff_utc <= now:
        return by_timeout
    return min(by_timeout, cutoff_utc)


# --- transitions -------------------------------------------------------------


def transition(
    business: Business,
    booking_id: UUID | str,
    to_status: str,
    *,
    actor: str = "",
    channel: str | None = None,
    alternative: datetime | None = None,
    allow_overflow: bool = False,
) -> BookingRow:
    """Move one booking to one status, with every consequence, atomically.

    Consequences, in the order they must happen:
      1. the move is legal from where it is now,
      2. the counter is adjusted if capacity is entering or leaving,
      3. guest history is updated,
      4. queued work that no longer applies is cancelled,
      5. the message this event owes the guest is queued.

    All in one transaction: a cancelled booking whose seat came back but whose
    apology never queued is worse than either failure alone.
    """
    with transaction() as conn:
        row = fetch_one(
            conn,
            "SELECT id, business_id, reference, start_time, end_time, party_size, status,"
            " name, phone, notes, source, slot_starts, customer_id, created_at"
            "  FROM bookings WHERE id = :i AND business_id = :b FOR UPDATE",
            i=str(booking_id), b=str(business.id),
        )
        if row is None:
            raise BookingNotFound(str(booking_id))
        current = row.status
        if current == to_status:
            return by_id(business, booking_id)
        if to_status not in TRANSITIONS.get(current, set()):
            raise IllegalTransition(f"cannot go from {current} to {to_status}")

        was_holding = current in HOLDS_CAPACITY
        will_hold = to_status in HOLDS_CAPACITY
        slot_starts = list(row.slot_starts or [])

        if will_hold and not was_holding:
            slot_starts = _take_capacity(
                conn, business, row, allow_overflow=allow_overflow
            )
        elif was_holding and not will_hold:
            slots.give_back(conn, business.id, slot_starts, row.party_size)
            slot_starts = []

        conn.execute(
            text(
                "UPDATE bookings SET status = :s, slot_starts = :starts, updated_at = now()"
                " WHERE id = :i"
            ),
            {"s": to_status, "starts": slot_starts, "i": str(row.id)},
        )
        _update_guest_history(conn, row, current, to_status)
        _after_transition(
            conn, business, row, current, to_status,
            channel=channel, alternative=alternative,
        )

    if to_status in (CANCELLED, DECLINED, NO_SHOW):
        # A freed seat is worth a waitlisted caller's evening (PRD §11).
        promote_from_waitlist(business, _as_dt(row.start_time))
    return by_id(business, booking_id)


def _take_capacity(
    conn: Connection, business: Business, row: Any, *, allow_overflow: bool
) -> list[datetime]:
    """Claim the counter for a booking entering a capacity-holding status."""
    if not business.tracks_capacity:
        return []
    service = availability.service_for(business, _as_dt(row.start_time))
    if service is None:
        return []
    starts = availability.turn_slots(service, _as_dt(row.start_time))
    locked = slots.lock(
        conn, business.id, starts,
        capacity_total=service.total_units, slot_minutes=service.slot_minutes,
    )
    room = availability.sellable(service, business)
    if slots.headroom(locked, room) < row.party_size and not allow_overflow:
        raise NoCapacity("there is no longer room for that booking")
    slots.add(conn, business.id, starts, row.party_size)
    return starts


def _update_guest_history(conn: Connection, row: Any, before: str, after: str) -> None:
    """visit_count and no_show_count are derived from transitions, not counted.

    Derived, because a host who taps "did not arrive" and then undoes it must
    leave no trace: a guest wrongly carrying a no-show gets treated worse on
    their next call, by a machine, forever.
    """
    if row.customer_id is None:
        return
    visits = (after == ARRIVED) - (before == ARRIVED)
    no_shows = (after == NO_SHOW) - (before == NO_SHOW)
    if not visits and not no_shows:
        return
    conn.execute(
        text(
            "UPDATE customers SET visit_count = GREATEST(0, visit_count + :v),"
            " no_show_count = GREATEST(0, no_show_count + :n), updated_at = now()"
            " WHERE id = :i"
        ),
        {"v": visits, "n": no_shows, "i": str(row.customer_id)},
    )


def _after_transition(
    conn: Connection,
    business: Business,
    row: Any,
    before: str,
    after: str,
    *,
    channel: str | None,
    alternative: datetime | None,
) -> None:
    booking = _row(row)
    if after in (CANCELLED, DECLINED, NO_SHOW):
        notifications.cancel_tasks_for(conn, row.id, ["reminder", "arrival_nudge"])
    if after == CANCELLED:
        notifications.cancel_tasks_for(conn, row.id, ["overflow_timeout"])

    told = _channel_for(business, after, channel)
    if told == "none":
        return
    if after == CONFIRMED and before == PENDING:
        notifications.queue_for_booking(
            conn, business, booking, "confirmed", channel=_as_channel(told)
        )
        notifications.schedule_reminder(conn, business, booking)
    elif after == DECLINED:
        notifications.queue_for_booking(
            conn, business, booking, "declined",
            alternative=alternative, channel=_as_channel(told),
        )
    elif after == CANCELLED:
        notifications.queue_for_booking(conn, business, booking, "cancelled")


def _channel_for(business: Business, after: str, channel: str | None) -> str:
    """How the guest is told (PRD §15e). An explicit choice beats the default."""
    if channel:
        return channel
    telling = business.config["telling_guest"]
    if after == CONFIRMED:
        return telling["on_accept"]
    if after in (DECLINED, CANCELLED):
        return telling["on_decline"]
    return "text"


def _as_channel(told: str) -> str:
    return "voice" if told == "call" else "sms"


# --- the named moves the dashboard and the agent make ------------------------


def accept(
    business: Business, booking_id: UUID | str, *, channel: str | None = None, actor: str = ""
) -> BookingRow:
    """Owner says yes to a waitlist request. May deliberately exceed the buffer.

    allow_overflow is True on purpose: the buffer is ours, not physics, and an
    owner who accepts a ninth table has decided to seat it.
    """
    return transition(
        business, booking_id, CONFIRMED, channel=channel, actor=actor, allow_overflow=True
    )


def decline(
    business: Business, booking_id: UUID | str, *, channel: str | None = None, actor: str = ""
) -> BookingRow:
    """Owner says no. The guest is offered the nearest alternative automatically."""
    booking = by_id(business, booking_id)
    alternatives = availability.alternatives_for(
        business, booking.start_time, booking.party_size
    )
    nearest = alternatives[0] if alternatives else None
    if nearest is None:
        found = availability.next_available(business, booking.party_size, limit=1)
        nearest = found[0] if found else None
    return transition(
        business, booking_id, DECLINED, channel=channel, actor=actor, alternative=nearest
    )


def cancel(business: Business, booking_id: UUID | str, *, actor: str = "") -> BookingRow:
    return transition(business, booking_id, CANCELLED, actor=actor)


def mark_arrived(business: Business, booking_id: UUID | str, *, actor: str = "") -> BookingRow:
    return transition(business, booking_id, ARRIVED, actor=actor)


def mark_no_show(business: Business, booking_id: UUID | str, *, actor: str = "") -> BookingRow:
    return transition(business, booking_id, NO_SHOW, actor=actor)


def undo(business: Business, booking_id: UUID | str, *, actor: str = "") -> BookingRow:
    return transition(business, booking_id, CONFIRMED, actor=actor, allow_overflow=True)


def move(
    business: Business, booking_id: UUID | str, new_start: datetime, *, actor: str = ""
) -> BookingRow:
    """Reschedule, keeping the reference (PRD §21 q4).

    The old slots are released and the new ones claimed in ONE transaction: a
    move that freed the old time and then failed to take the new one would
    leave a guest with a confirmation for a table nobody is holding.
    """
    new_start = new_start.astimezone(UTC)
    with transaction() as conn:
        row = fetch_one(
            conn,
            "SELECT id, start_time, party_size, status, slot_starts FROM bookings"
            " WHERE id = :i AND business_id = :b FOR UPDATE",
            i=str(booking_id), b=str(business.id),
        )
        if row is None:
            raise BookingNotFound(str(booking_id))
        if row.status not in HOLDS_CAPACITY | {PENDING}:
            raise IllegalTransition(f"a {row.status} booking cannot be moved")

        old_starts = list(row.slot_starts or [])
        if old_starts:
            slots.give_back(conn, business.id, old_starts, row.party_size)

        service = availability.service_for(business, new_start)
        new_starts: list[datetime] = []
        if business.tracks_capacity and row.status in HOLDS_CAPACITY:
            if service is None:
                raise NoCapacity("we are not open then")
            new_starts = availability.turn_slots(service, new_start)
            locked = slots.lock(
                conn, business.id, new_starts,
                capacity_total=service.total_units, slot_minutes=service.slot_minutes,
            )
            if slots.headroom(locked, availability.sellable(service, business)) < row.party_size:
                raise NoCapacity("there is no room at that time")
            slots.add(conn, business.id, new_starts, row.party_size)

        turn = service.turn_minutes if service else _turn_minutes(business, new_start)
        conn.execute(
            text(
                "UPDATE bookings SET start_time = :s, end_time = :e, slot_starts = :starts,"
                " updated_at = now() WHERE id = :i"
            ),
            {
                "s": new_start,
                "e": new_start + timedelta(minutes=turn),
                "starts": new_starts,
                "i": str(row.id),
            },
        )
        moved = by_id_conn(conn, business, row.id)
        notifications.queue_for_booking(conn, business, moved, "confirmed")
        notifications.schedule_reminder(conn, business, moved)
    return by_id(business, booking_id)


def update_notes(
    business: Business, booking_id: UUID | str, notes: str
) -> BookingRow:
    with transaction() as conn:
        result = conn.execute(
            text(
                "UPDATE bookings SET notes = :n, updated_at = now()"
                " WHERE id = :i AND business_id = :b"
            ),
            {"n": notes, "i": str(booking_id), "b": str(business.id)},
        )
        if result.rowcount == 0:
            raise BookingNotFound(str(booking_id))
    return by_id(business, booking_id)


# --- the waitlist ------------------------------------------------------------


def promote_from_waitlist(business: Business, slot_start: datetime) -> BookingRow | None:
    """A cancellation frees a seat: the oldest fitting request takes it.

    Automatic, and the owner is told AFTER the fact (PRD §11). Asking would
    mean the seat sits empty until somebody looks at their phone, which is the
    revenue this product exists to stop losing.
    """
    if not business.config["features"].get("overflow", True):
        return None
    waiting = [b for b in pending_requests(business) if b.start_time == slot_start]
    for candidate in sorted(waiting, key=lambda b: b.created_at):
        verdict = availability.is_available(
            business, candidate.start_time, candidate.party_size, with_alternatives=False
        )
        if not verdict.ok:
            continue
        try:
            promoted = transition(business, candidate.id, CONFIRMED)
        except (NoCapacity, IllegalTransition):
            continue
        log.info(
            "auto-accepted waitlisted booking %s for %s after a cancellation",
            promoted.reference, business.slug,
        )
        return promoted
    return None


def expire_overflow(business: Business, booking_id: UUID | str) -> BookingRow | None:
    """The deadline passed with no answer: auto-decline and offer an alternative.

    PRD §11: never leave the customer silent. Silence from the owner is an
    answer to us; it must never be an answer to the guest.
    """
    try:
        booking = by_id(business, booking_id)
    except BookingNotFound:
        return None
    if booking.status != PENDING:
        return None
    return decline(business, booking_id)


# --- helpers -----------------------------------------------------------------


def by_id_conn(conn: Connection, business: Business, booking_id: UUID | str) -> BookingRow:
    row = fetch_one(
        conn, _SELECT + " WHERE b.id = :i AND b.business_id = :b",
        i=str(booking_id), b=str(business.id),
    )
    if row is None:
        raise BookingNotFound(str(booking_id))
    return _row(row)


def _turn_minutes(business: Business, start: datetime) -> int:
    service = availability.service_for(business, start)
    if service:
        return service.turn_minutes
    return int(business.config["capacity"]["turn_minutes"])


def _as_dt(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _business_id(business: Business | UUID | str) -> UUID | str:
    return business.id if isinstance(business, Business) else business
