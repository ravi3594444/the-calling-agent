"""Restaurant reservation logic and the tools the agent calls.

State lives in memory. That is fine for a demo and wrong for production: on a
serverless host each function instance has its own copy, so a booking taken by
one instance is invisible to the next. Swapping BOOKINGS for a real database is
the one change needed to make this real -- every function below goes through
it, so nothing else has to move.
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from .config import settings
from .menu import IMPLEMENTATIONS as MENU_IMPLEMENTATIONS
from .menu import TOOLS as MENU_TOOLS

# --- House rules -------------------------------------------------------------

# (open, close) per weekday, Monday = 0. None means closed that day.
OPENING_HOURS: dict[int, tuple[time, time] | None] = {
    0: (time(12, 0), time(22, 0)),
    1: (time(12, 0), time(22, 0)),
    2: (time(12, 0), time(22, 0)),
    3: (time(12, 0), time(22, 0)),
    4: (time(12, 0), time(23, 0)),
    5: (time(11, 0), time(23, 0)),
    6: (time(11, 0), time(21, 0)),
}

SLOT_MINUTES = 30
# Covers per 30-minute slot. The kitchen, not the dining room, is the limit.
SEATS_PER_SLOT = 12
MAX_DAYS_AHEAD = 60


@dataclass
class Booking:
    reference: str
    name: str
    party_size: int
    at: datetime
    phone: str = ""
    notes: str = ""
    cancelled: bool = False


@dataclass
class BookingStore:
    """In-memory reservation book. Replace with a database for real use."""

    bookings: dict[str, Booking] = field(default_factory=dict)

    def seats_taken(self, at: datetime) -> int:
        return sum(
            b.party_size
            for b in self.bookings.values()
            if not b.cancelled and b.at == at
        )

    def add(self, booking: Booking) -> None:
        self.bookings[booking.reference] = booking

    def get(self, reference: str) -> Booking | None:
        return self.bookings.get(reference.strip().upper())


BOOKINGS = BookingStore()


# --- Helpers -----------------------------------------------------------------


def _reference() -> str:
    """Short, unambiguous code. No 0/O or 1/I -- these get read aloud."""
    alphabet = "".join(c for c in string.ascii_uppercase + string.digits if c not in "O0I1")
    while True:
        ref = "".join(random.choices(alphabet, k=5))
        if ref not in BOOKINGS.bookings:
            return ref


def _parse_when(day: str, at: str) -> tuple[datetime | None, str | None]:
    """Parse an ISO date and 24-hour time into an aware datetime.

    The agent is told to resolve "tomorrow" itself, so only absolute values
    arrive here; anything else is a prompt failure worth reporting clearly.
    """
    try:
        parsed_day = date.fromisoformat(day.strip())
    except ValueError:
        return None, f"'{day}' is not a date I can read. Use YYYY-MM-DD."
    try:
        hour, minute = (int(part) for part in at.strip().split(":")[:2])
        parsed_time = time(hour, minute)
    except (ValueError, TypeError):
        return None, f"'{at}' is not a time I can read. Use HH:MM on a 24-hour clock."

    when = datetime.combine(parsed_day, parsed_time, tzinfo=UTC)

    now = datetime.now(UTC)
    if when < now:
        return None, "That is in the past."
    if (parsed_day - now.date()).days > MAX_DAYS_AHEAD:
        return None, f"We only take bookings up to {MAX_DAYS_AHEAD} days ahead."
    if parsed_time.minute % SLOT_MINUTES:
        return None, f"Bookings start every {SLOT_MINUTES} minutes, on the hour or half hour."

    hours = OPENING_HOURS.get(parsed_day.weekday())
    if hours is None:
        return None, f"We are closed on {parsed_day.strftime('%A')}s."
    opens, closes = hours
    # The last booking must finish before closing.
    last = (
        datetime.combine(parsed_day, closes, tzinfo=UTC) - timedelta(minutes=SLOT_MINUTES)
    ).time()
    if not (opens <= parsed_time <= last):
        return None, (
            f"On {parsed_day.strftime('%A')}s we serve from "
            f"{opens.strftime('%H:%M')} to {closes.strftime('%H:%M')}."
        )
    return when, None


def _spoken(when: datetime) -> str:
    """A form the agent can read aloud without mangling it."""
    return when.strftime("%A %d %B at %H:%M")


# --- Tool implementations ----------------------------------------------------


def check_availability(args: dict[str, Any]) -> str:
    day = str(args.get("date", ""))
    at = str(args.get("time", ""))
    party = int(args.get("party_size") or 0)

    if not 1 <= party <= settings.max_party_size:
        return f"Parties are between one and {settings.max_party_size} people."

    when, problem = _parse_when(day, at)
    if problem:
        return problem
    assert when is not None

    free = SEATS_PER_SLOT - BOOKINGS.seats_taken(when)
    if free >= party:
        return f"Yes, {_spoken(when)} is available for {party}."

    # Offer the nearest alternatives rather than a flat no.
    alternatives = []
    for step in (1, -1, 2, -2, 3, -3, 4, -4):
        other = when + timedelta(minutes=SLOT_MINUTES * step)
        _, bad = _parse_when(other.date().isoformat(), other.strftime("%H:%M"))
        if bad:
            continue
        if SEATS_PER_SLOT - BOOKINGS.seats_taken(other) >= party:
            alternatives.append(other.strftime("%H:%M"))
        if len(alternatives) == 3:
            break

    if not alternatives:
        return f"{_spoken(when)} is fully booked for {party}, and so is the rest of that day."
    return (
        f"{_spoken(when)} is full for {party}. "
        f"I could do {', '.join(sorted(alternatives))} the same day."
    )


def book_table(args: dict[str, Any]) -> str:
    name = str(args.get("name", "")).strip()
    day = str(args.get("date", ""))
    at = str(args.get("time", ""))
    party = int(args.get("party_size") or 0)
    phone = str(args.get("phone", "")).strip()
    notes = str(args.get("notes", "")).strip()

    if not name:
        return "I need a name for the booking."
    if not 1 <= party <= settings.max_party_size:
        return f"Parties are between one and {settings.max_party_size} people."

    when, problem = _parse_when(day, at)
    if problem:
        return problem
    assert when is not None

    if SEATS_PER_SLOT - BOOKINGS.seats_taken(when) < party:
        return f"{_spoken(when)} just filled up. Ask me for another time."

    booking = Booking(
        reference=_reference(),
        name=name,
        party_size=party,
        at=when,
        phone=phone,
        notes=notes,
    )
    BOOKINGS.add(booking)
    confirmation = (
        f"Booked: {party} for {name}, {_spoken(when)}. "
        f"The reference is {' '.join(booking.reference)}."
    )
    if notes:
        confirmation += f" Noted: {notes}."
    return confirmation


def lookup_booking(args: dict[str, Any]) -> str:
    booking = BOOKINGS.get(str(args.get("reference", "")))
    if booking is None:
        return "I cannot find a booking with that reference."
    if booking.cancelled:
        return f"That booking for {booking.name} was cancelled."
    return (
        f"{booking.party_size} for {booking.name}, {_spoken(booking.at)}."
        + (f" Noted: {booking.notes}." if booking.notes else "")
    )


def cancel_booking(args: dict[str, Any]) -> str:
    booking = BOOKINGS.get(str(args.get("reference", "")))
    if booking is None:
        return "I cannot find a booking with that reference."
    if booking.cancelled:
        return "That one was already cancelled."
    booking.cancelled = True
    return f"Cancelled: {booking.party_size} for {booking.name}, {_spoken(booking.at)}."


def restaurant_info(_args: dict[str, Any]) -> str:
    lines = [f"{settings.restaurant_name} serves {settings.restaurant_cuisine}."]
    if settings.restaurant_address:
        lines.append(f"We are at {settings.restaurant_address}.")
    grouped: list[str] = []
    for weekday in range(7):
        label = date(2024, 1, 1 + weekday).strftime("%A")
        hours = OPENING_HOURS[weekday]
        grouped.append(
            f"{label}: closed"
            if hours is None
            else f"{label}: {hours[0].strftime('%H:%M')} to {hours[1].strftime('%H:%M')}"
        )
    lines.append("Opening hours are " + "; ".join(grouped) + ".")
    return " ".join(lines)


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "check_availability",
        "description": (
            "Check whether a table is free. Always call this before booking. "
            "If the slot is full it suggests nearby times."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Date as YYYY-MM-DD."},
                "time": {"type": "string", "description": "24-hour time as HH:MM."},
                "party_size": {"type": "integer", "description": "Number of people."},
            },
            "required": ["date", "time", "party_size"],
        },
    },
    {
        "type": "function",
        "name": "book_table",
        "description": (
            "Confirm a reservation. Only call this once the caller has given a "
            "name, party size, date and time, and has agreed to them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name for the booking."},
                "date": {"type": "string", "description": "Date as YYYY-MM-DD."},
                "time": {"type": "string", "description": "24-hour time as HH:MM."},
                "party_size": {"type": "integer", "description": "Number of people."},
                "phone": {"type": "string", "description": "Contact number, if given."},
                "notes": {
                    "type": "string",
                    "description": "Allergies, occasion, seating or access requests.",
                },
            },
            "required": ["name", "date", "time", "party_size"],
        },
    },
    {
        "type": "function",
        "name": "lookup_booking",
        "description": "Read back an existing reservation from its reference code.",
        "parameters": {
            "type": "object",
            "properties": {
                "reference": {"type": "string", "description": "Five-character code."}
            },
            "required": ["reference"],
        },
    },
    {
        "type": "function",
        "name": "cancel_booking",
        "description": "Cancel a reservation by its reference code.",
        "parameters": {
            "type": "object",
            "properties": {
                "reference": {"type": "string", "description": "Five-character code."}
            },
            "required": ["reference"],
        },
    },
    {
        "type": "function",
        "name": "restaurant_info",
        "description": "Opening hours, address and cuisine. Use for questions about the venue.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]

TOOLS += MENU_TOOLS

IMPLEMENTATIONS = {
    "check_availability": check_availability,
    "book_table": book_table,
    "lookup_booking": lookup_booking,
    "cancel_booking": cancel_booking,
    "restaurant_info": restaurant_info,
    **MENU_IMPLEMENTATIONS,
}
