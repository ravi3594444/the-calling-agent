"""The tools the agent may call (PRD §8), bound to one business.

The agent holds the conversation. The database holds the truth. No
availability data is ever put in the prompt, because a prompt is written once
per call and the book changes during it.

Every function here follows the same shape: validate the arguments, ask the
engine, and return a sentence the agent can say. Guard clauses first, the real
work last, so the successful path reads top to bottom.

WHAT A TOOL RETURNS
A `Spoken` -- a string the agent reads aloud, carrying a `.data` payload the
dashboard and the call log use. It is a str subclass so the relay, which has
only ever handled strings, needs no change.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from . import availability, bookings, dates, formatting, holds, menu
from .agent_spec import AgentDefinition
from .businesses import Business
from .db import readonly
from .records import Booking

log = logging.getLogger(__name__)


#: What a crashed tool tells the agent.
#:
#: Never the exception text. A psycopg error carries the failing SQL, the
#: column names and the tenant's own uuid, and whatever a tool returns is put
#: in front of the model -- which then paraphrases it to the caller. One bad
#: hold id printed a stack trace into a live conversation. The detail belongs
#: in the log, where it is just as useful and nobody is listening.
TOOL_FAILED = (
    "That did not go through on my side. Apologise, and either try once more "
    "or offer to take a number and call back. Do not invent a result."
)


class Spoken(str):
    """What the agent says, plus what the UI needs. Still a plain string."""

    data: dict[str, Any]

    def __new__(cls, text: str, **data: Any):
        spoken = super().__new__(cls, text)
        spoken.data = data
        return spoken


# --- the tools ---------------------------------------------------------------


def now(business: Business, _args: dict[str, Any]) -> Spoken:
    """The clock, as a tool rather than baked into the prompt.

    A prompt is written once when the call connects. A call can outlive
    midnight, and a business can be in a different timezone from the server,
    so a date baked into the prompt is wrong for exactly the calls where it
    matters most.
    """
    moment = datetime.now(UTC)
    local = formatting.local(business, moment)
    return Spoken(
        f"It is {formatting.time_str(business, moment)} on "
        f"{formatting.date_long(business, moment)}.",
        iso=moment.isoformat(),
        local_iso=local.isoformat(),
        weekday=local.strftime("%A"),
        date=local.date().isoformat(),
        timezone=business.timezone,
    )


def resolve_date(business: Business, args: dict[str, Any]) -> Spoken:
    """"next Monday" -> a real date in the venue's own calendar."""
    phrase = str(args.get("phrase", "")).strip()
    if not phrase:
        return Spoken(
            "I need something to work out, like 'next Monday'.",
            error=True, ok=False, reason="phrase_missing",
        )

    today = formatting.local(business, datetime.now(UTC)).date()
    languages = tuple(business.config["voice"]["languages"])
    resolved = dates.resolve_date(phrase, today=today, languages=languages)
    if resolved is None:
        return Spoken(
            f"I could not work out what date {phrase!r} means.",
            error=True, ok=False, reason="date_unclear",
        )

    return Spoken(
        f"{phrase} is {formatting.date_long(business, resolved)}.",
        date=resolved.isoformat(),
        weekday=resolved.strftime("%A"),
    )


def lookup_customer(business: Business, args: dict[str, Any]) -> Spoken:
    """Who is calling, and their NEXT booking -- never a list of them.

    One booking, even when there are several (PRD §8). A phone number is not
    proof of identity: it gets reassigned, shared, and overheard on
    speakerphone, and reading somebody's diary to whoever holds their old
    number is the kind of leak that ends a contract.
    """
    phone = _phone(business, args.get("phone"))
    if not phone:
        return Spoken("I do not have a number to look up.", known=False)

    with readonly() as conn:
        from .db import fetch_one

        guest = fetch_one(
            conn,
            "SELECT name, visit_count, no_show_count, do_not_call FROM customers"
            " WHERE business_id = :b AND phone_e164 = :p",
            b=str(business.id),
            p=phone,
        )
    if guest is None:
        return Spoken("I have not spoken to this number before.", known=False)

    upcoming = bookings.upcoming_for_phone(business, phone, limit=1)
    parts = [f"This is {guest.name}." if guest.name else "I know this number."]
    if guest.visit_count:
        parts.append(f"They have been in {guest.visit_count} times.")
    if upcoming:
        booking = upcoming[0]
        parts.append(
            f"They already have {business.units(booking.party_size)} booked for "
            f"{formatting.when(business, booking.start_time)}, reference "
            f"{formatting.spell(booking.reference)}."
        )

    return Spoken(
        " ".join(parts),
        known=True,
        name=guest.name,
        visits=guest.visit_count,
        no_shows=guest.no_show_count,
        next_booking=upcoming[0].as_dict(business) if upcoming else None,
    )


def check_availability(business: Business, args: dict[str, Any]) -> Spoken:
    """Is there room? Never guess -- this is the only honest answer."""
    when, problem = _when(business, args)
    if problem:
        return problem
    units, problem = _units(business, args)
    if problem:
        return problem

    verdict = availability.is_available(business, when, units)
    if verdict.ok:
        return Spoken(
            f"Yes, {formatting.when(business, when)} works for {business.units(units)}.",
            ok=True,
            start_time=when.isoformat(),
        )
    return _refusal(business, when, units, verdict)


def hold(business: Business, args: dict[str, Any]) -> Spoken:
    """Claim the capacity NOW, before asking for a name (PRD §8).

    This is the single most important ordering rule in the product. Collecting
    details first and discovering the slot went while the caller spelled their
    surname is the failure this tool exists to prevent.
    """
    when, problem = _when(business, args)
    if problem:
        return problem
    units, problem = _units(business, args)
    if problem:
        return problem

    result = holds.take(business, when, units, call_id=args.get("call_id"))
    if not result.ok:
        verdict = availability.Availability(
            False, result.reason, alternatives=result.alternatives, detail=result.detail
        )
        return _refusal(business, when, units, verdict)

    seconds = int((result.expires_at - datetime.now(UTC)).total_seconds())
    return Spoken(
        f"Held {business.units(units)} for {formatting.when(business, when)}. "
        "I need a name and a number to confirm it.",
        ok=True,
        hold_id=str(result.hold_id),
        expires_at=result.expires_at.isoformat(),
        expires_in_seconds=max(0, seconds),
        start_time=when.isoformat(),
    )


def confirm(business: Business, args: dict[str, Any]) -> Spoken:
    """Turn a live hold into a booking and read the reference back."""
    hold_id = str(args.get("hold_id", "")).strip()
    name = str(args.get("name", "")).strip()
    if not hold_id:
        return Spoken(
            "I need the hold before I can confirm it.",
            error=True, ok=False, reason="hold_missing",
        )
    if not _is_uuid(hold_id):
        # A model that never called hold() will invent something that looks
        # like an id -- "hold_12345" -- and confirm with it. Checked here
        # rather than in the query, because handing that to Postgres raises
        # an InvalidTextRepresentation whose text names the table, the SQL and
        # the tenant's own uuid, and that text goes straight into the
        # conversation. The answer has to tell the agent what to DO instead.
        log.warning("confirm called with a fabricated hold id %r", hold_id[:40])
        return Spoken(
            "I do not have that table held. Let me take the time again before "
            "I confirm it.",
            error=True, ok=False, reason="hold_not_found",
        )
    if not name:
        return Spoken(
            "I need a name for the booking.", error=True, ok=False, reason="name_missing"
        )

    try:
        booking = holds.confirm(
            business,
            hold_id,
            name=name,
            phone=_phone(business, args.get("phone")),
            notes=str(args.get("notes", "")).strip(),
            call_id=args.get("call_id"),
        )
    except holds.HoldExpired:
        return Spoken(
            "That hold just lapsed while we were talking. Let me check that time again.",
            error=True, ok=False, reason="hold_expired", expired=True,
        )
    except holds.HoldNotFound:
        return Spoken(
            "I have lost track of that hold. Let me take the time again.",
            error=True, ok=False, reason="hold_not_found",
        )

    _queue_confirmation(business, booking)
    return Spoken(
        f"Booked: {business.units(booking.party_size)} for {booking.name}, "
        f"{formatting.when(business, booking.start_time)}. "
        f"The reference is {formatting.spell(booking.reference)}.",
        ok=True,
        reference=booking.reference,
        booking_id=str(booking.id),
        start_time=booking.start_time.isoformat(),
        party_size=booking.party_size,
        name=booking.name,
        status=booking.status,
    )


def lookup_booking(business: Business, args: dict[str, Any]) -> Spoken:
    """Find one booking, by reference or by the number that made it."""
    reference = str(args.get("reference", "")).strip()
    if reference:
        found = bookings.by_reference(business, reference)
        if found is None:
            return Spoken("I cannot find a booking with that reference.", found=False)
        return _describe(business, found)

    phone = _phone(business, args.get("phone"))
    if not phone:
        return Spoken("I need a reference or a phone number to look it up.", found=False)

    upcoming = bookings.upcoming_for_phone(business, phone, limit=1)
    if not upcoming:
        return Spoken("I cannot find a booking for that number.", found=False)
    return _describe(business, upcoming[0])


def cancel_booking(business: Business, args: dict[str, Any]) -> Spoken:
    """Cancel, after confirming one detail only the booker would know.

    PRD §8: destructive actions need a confirming detail. The reference is not
    one -- it is read aloud on the phone and printed in a text, so anyone in
    earshot has it. The name on the booking is the check.
    """
    reference = str(args.get("reference", "")).strip()
    if not reference:
        return Spoken(
            "I need the reference to cancel a booking.",
            error=True, ok=False, reason="reference_missing",
        )

    booking = bookings.by_reference(business, reference)
    if booking is None:
        return Spoken("I cannot find a booking with that reference.", found=False)
    if booking.status == bookings.CANCELLED:
        return Spoken("That one was already cancelled.", found=True, status="cancelled")

    claimed_name = str(args.get("name", "")).strip()
    if not claimed_name:
        return Spoken(
            "Before I cancel that, can I take the name it is under?",
            needs_confirmation=True,
        )
    if not _same_name(claimed_name, booking.name):
        return Spoken(
            "That name does not match what I have for this booking, "
            "so I will not cancel it.",
            error=True, ok=False, reason="name_mismatch", mismatch=True,
        )

    cancelled = bookings.cancel(business, booking.id)
    return Spoken(
        f"Cancelled: {business.units(cancelled.party_size)} for {cancelled.name}, "
        f"{formatting.when(business, cancelled.start_time)}.",
        ok=True,
        reference=cancelled.reference,
        status=cancelled.status,
    )


def request_overflow(business: Business, args: dict[str, Any]) -> Spoken:
    """Join the waitlist, with a deadline the caller is told out loud.

    Nobody waits without knowing they are waiting (PRD §10).
    """
    when, problem = _when(business, args)
    if problem:
        return problem
    units, problem = _units(business, args)
    if problem:
        return problem

    name = str(args.get("name", "")).strip()
    if not name:
        return Spoken(
            "I need a name to put them on the list.",
            error=True, ok=False, reason="name_missing",
        )

    try:
        booking, deadline = bookings.request_overflow(
            business,
            start=when,
            units=units,
            name=name,
            phone=_phone(business, args.get("phone")),
            notes=str(args.get("notes", "")).strip(),
        )
    except bookings.BookingError as exc:
        return Spoken(str(exc), error=True, ok=False, reason="overflow_refused")

    return Spoken(
        f"I have put you down for {formatting.when(business, when)} and we will "
        f"confirm by {formatting.time_str(business, deadline)}. "
        "You will get a text either way.",
        ok=True,
        pending=True,
        reference=booking.reference,
        deadline=deadline.isoformat(),
    )


def business_info(business: Business, _args: dict[str, Any]) -> Spoken:
    """Address, hours and what this place is. Read from config, never hardcoded."""
    identity = business.config["identity"]
    lines = [f"{identity['display_name'] or business.name}"]
    if identity["description"]:
        lines[0] += f" — {identity['description']}"
    lines[0] += "."
    if identity["address"]:
        lines.append(f"We are at {identity['address']}.")

    today = formatting.local(business, datetime.now(UTC)).date()
    week = []
    for offset in range(7):
        day = today + timedelta(days=offset)
        services = availability.services_on(business, day)
        if not services:
            week.append(f"{day.strftime('%A')}: closed")
            continue
        spans = ", ".join(
            f"{formatting.time_str(business, s.starts_at)} to "
            f"{formatting.time_str(business, s.ends_at)}"
            for s in services
        )
        week.append(f"{day.strftime('%A')}: {spans}")
    lines.append("This week: " + "; ".join(week) + ".")
    return Spoken(" ".join(lines), hours=week)


# --- argument handling -------------------------------------------------------


def _when(business: Business, args: dict[str, Any]) -> tuple[datetime | None, Spoken | None]:
    """Read a date and time into an instant, or say what is wrong with them."""
    day = dates.parse_iso_date(str(args.get("date", "")))
    if day is None:
        phrase = str(args.get("date", ""))
        today = formatting.local(business, datetime.now(UTC)).date()
        day = dates.resolve_date(phrase, today=today,
                                 languages=tuple(business.config["voice"]["languages"]))
    if day is None:
        return None, Spoken(
            "I did not catch the date. What day were you thinking?",
            error=True, ok=False, reason="date_unclear",
        )

    raw_time = str(args.get("time", ""))
    at = dates.parse_iso_time(raw_time) or dates.resolve_time(raw_time)
    if at is None:
        # A bare "7" means seven in the evening at a restaurant that opens at
        # noon, and seven in the morning at a garage. Opening hours settle it,
        # which is exactly how a person behind the counter would read it.
        at = _hour_within_opening(business, day, dates.bare_hour(raw_time))
    if at is None:
        return None, Spoken(
            "I did not catch the time. What time suits you?",
            error=True, ok=False, reason="time_unclear",
        )

    return datetime.combine(day, at, tzinfo=business.tz).astimezone(UTC), None


def _hour_within_opening(business: Business, day: date, hour: int | None) -> time | None:
    """Read a bare hour as whichever of its two meanings the venue is open for.

    "7" is 07:00 or 19:00. If the venue is open for exactly one of them, that
    is the one the caller meant. If it is open for both, or neither, this
    returns None and the agent asks -- guessing wrong books somebody twelve
    hours out, which nobody notices until the day.
    """
    if hour is None:
        return None

    candidates = [time(hour)]
    if hour < 12:
        candidates.append(time(hour + 12))

    services = availability.services_on(business, day)
    if not services:
        return None

    open_now = [
        candidate
        for candidate in candidates
        if any(
            service.starts_at
            <= datetime.combine(day, candidate, tzinfo=business.tz).astimezone(UTC)
            < service.ends_at
            for service in services
        )
    ]
    return open_now[0] if len(open_now) == 1 else None


def _units(business: Business, args: dict[str, Any]) -> tuple[int, Spoken | None]:
    raw = args.get("units", args.get("party_size", args.get("people")))
    try:
        units = int(raw)
    except (TypeError, ValueError):
        return 0, _units_unclear(business)

    largest = business.config["policy"]["max_party_size"]
    if units < 1:
        return 0, _units_unclear(business)
    if units > largest:
        return 0, Spoken(
            f"The largest we can take is {business.units(largest)}. "
            "I can take a message if it is bigger than that.",
            error=True,
            ok=False,
            reason=availability.PARTY_TOO_LARGE,
            too_large=True,
            max=largest,
        )
    return units, None


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _units_unclear(business: Business) -> Spoken:
    return Spoken(
        f"How many {business.unit_plural}?",
        error=True, ok=False, reason="units_unclear",
    )


def _phone(business: Business, raw: Any) -> str:
    return dates.normalise_phone(str(raw or ""), region=business.config["locale"]["country"])


def _same_name(claimed: str, stored: str) -> bool:
    """Forgiving on purpose: a caller says "Sharma", the booking says "Ravi Sharma"."""
    claimed_parts = {p for p in claimed.lower().split() if p}
    stored_parts = {p for p in stored.lower().split() if p}
    if not claimed_parts or not stored_parts:
        return False
    return bool(claimed_parts & stored_parts)


def _refusal(
    business: Business, when: datetime, units: int, verdict: availability.Availability
) -> Spoken:
    """One place that turns a machine reason into something worth hearing.

    A caller told only "no" phones the next restaurant. A caller offered
    quarter past eight usually takes it, which is the entire product.
    """
    detail = verdict.detail or {}
    alternatives = [formatting.time_str(business, a) for a in verdict.alternatives]
    offer = (
        f" I could do {_join(alternatives)} instead." if alternatives else ""
    )

    reasons = {
        availability.CLOSED: f"We are closed on {detail.get('weekday', 'that day')}.",
        availability.HOLIDAY: f"We are closed for {detail.get('holiday', 'a holiday')}.",
        availability.PAST: "That is in the past.",
        availability.LAST_SEATING: "That is past our last booking of the night.",
        availability.FULL: f"{formatting.when(business, when)} is full.",
        availability.OFF_GRID: (
            f"We take bookings every {detail.get('slot_minutes', 30)} minutes, "
            "on the hour or the half hour."
        ),
        availability.PARTY_TOO_LARGE: (
            f"The largest we can take is {business.units(detail.get('max', units))}."
        ),
        availability.TOO_SOON: "That is too close to now for me to take over the phone.",
        availability.OUTSIDE_WINDOW: (
            "We are not taking bookings that far ahead yet. "
            "I can take a number and ring you when we open that date."
        ),
    }
    sentence = reasons.get(verdict.reason, "I cannot take that one.")

    if not alternatives and verdict.reason == availability.FULL:
        elsewhere = availability.next_available(business, units, limit=1)
        if elsewhere:
            offer = f" The next I have is {formatting.when(business, elsewhere[0])}."

    # `requested` and `now` ride along on EVERY refusal, not just a past one.
    # A refusal the model cannot diagnose is a refusal it repeats: told only
    # "that is in the past", it argued with the clock and tried the same wrong
    # year three times. Told which instant it asked for and which instant it
    # is, it can see its own mistake and fix it in one turn.
    return Spoken(
        sentence + offer,
        ok=False,
        reason=verdict.reason,
        requested=when.isoformat(),
        now=datetime.now(UTC).isoformat(),
        alternatives=[a.isoformat() for a in verdict.alternatives],
    )


def _join(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" or {items[-1]}"


def _describe(business: Business, booking: bookings.BookingRow) -> Spoken:
    text = (
        f"{business.units(booking.party_size)} for {booking.name}, "
        f"{formatting.when(business, booking.start_time)}."
    )
    if booking.notes:
        text += f" Noted: {booking.notes}."
    if booking.status == bookings.PENDING:
        text += " That one is still waiting on the owner."
    return Spoken(text, found=True, **booking.as_dict(business))


def _queue_confirmation(business: Business, booking: Booking) -> None:
    """Text the guest and schedule the reminder. Never blocks the call."""
    from . import notifications
    from .db import transaction

    try:
        with transaction() as conn:
            notifications.queue_for_booking(
                conn, business, booking, "confirmed", manage_token=booking.manage_token
            )
            notifications.schedule_reminder(conn, business, booking)
    except Exception:  # noqa: BLE001 - a failed text must never undo a booking
        log.exception("could not queue the confirmation for %s", booking.reference)


# --- declarations ------------------------------------------------------------

_DATE = {
    "type": "string",
    "description": (
        "ISO-8601 date, YYYY-MM-DD (e.g. 2031-03-09). The four-digit year "
        "comes from the now tool -- never guess it."
    ),
}
_TIME = {"type": "string", "description": "24-hour time, HH:MM (e.g. 19:30)."}


def tool_declarations(business: Business) -> list[dict[str, Any]]:
    """The JSON Schema the Voice Agent API is given, worded for this business.

    Worded per business because "how many covers" and "how many patients" are
    different questions, and a tool description is read by the model on every
    turn. One vertical's words in another vertical's mouth is how an agent
    ends up asking a dentist's caller about their party size.
    """
    units_field = {
        "type": "integer",
        "description": f"Number of {business.unit_plural}.",
    }
    booking_noun = business.config["agent"]["booking_noun"]

    declarations: list[dict[str, Any]] = [
        _tool("now", "The current date and time where the business is. Call this "
                     "before working out any relative day.", {}),
        _tool(
            "resolve_date",
            "Turn a phrase like 'next Monday' or 'this Friday' into a real date.",
            {"phrase": {"type": "string", "description": "What the caller said."}},
            required=["phrase"],
        ),
        _tool(
            "lookup_customer",
            "Who is calling, and their next booking if they have one.",
            {"phone": {"type": "string", "description": "Caller's number."}},
            required=["phone"],
        ),
        _tool(
            "check_availability",
            "Answer an OPEN question about what is free -- 'are you busy on "
            "Saturday?', 'what have you got around eight?'. This reserves "
            "NOTHING. If the caller has named a time they actually want, use "
            "hold instead: it checks and claims in one step.",
            {"date": _DATE, "time": _TIME, "units": units_field},
            required=["date", "time", "units"],
        ),
        _tool(
            "hold",
            f"Claim a {booking_noun} for a few minutes. Call this the MOMENT the "
            "caller names a time they want, BEFORE asking for their name or "
            "number. It checks availability itself and returns alternatives if "
            "the time is full, so calling check_availability first is a wasted "
            "step -- and the time can be taken by someone else in between.",
            {"date": _DATE, "time": _TIME, "units": units_field},
            required=["date", "time", "units"],
        ),
        _tool(
            "confirm",
            "Turn a hold into a real booking. Only call this once the caller has "
            "given a name and agreed to the time.",
            {
                "hold_id": {"type": "string", "description": "From the hold tool."},
                "name": {"type": "string", "description": "Name for the booking."},
                "phone": {"type": "string", "description": "Contact number."},
                "notes": {
                    "type": "string",
                    "description": "Allergies, occasion, access needs, seating requests.",
                },
            },
            required=["hold_id", "name"],
        ),
        _tool(
            "lookup_booking",
            "Read back one existing booking, by reference or phone number.",
            {
                "reference": {"type": "string", "description": "Six-character code."},
                "phone": {"type": "string", "description": "The number that booked."},
            },
        ),
        _tool(
            "cancel_booking",
            "Cancel a booking. Requires the name on the booking as confirmation.",
            {
                "reference": {"type": "string", "description": "Six-character code."},
                "name": {"type": "string", "description": "Name the caller gives."},
            },
            required=["reference"],
        ),
        _tool(
            "business_info",
            "Opening hours, address and what this place is.",
            {},
        ),
    ]

    if business.config["features"].get("overflow", True):
        declarations.append(
            _tool(
                "request_overflow",
                "Put the caller on the waitlist when the time is genuinely full and "
                "no alternative suits. Always tell them the deadline you are given.",
                {
                    "date": _DATE,
                    "time": _TIME,
                    "units": units_field,
                    "name": {"type": "string"},
                    "phone": {"type": "string"},
                    "notes": {"type": "string"},
                },
                required=["date", "time", "units", "name"],
            )
        )

    if business.config["features"].get("menu"):
        declarations += menu.TOOLS

    return declarations


def _tool(
    name: str, description: str, properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
    }


IMPLEMENTATIONS = {
    "now": now,
    "resolve_date": resolve_date,
    "lookup_customer": lookup_customer,
    "check_availability": check_availability,
    "hold": hold,
    "confirm": confirm,
    "lookup_booking": lookup_booking,
    "cancel_booking": cancel_booking,
    "request_overflow": request_overflow,
    "business_info": business_info,
}


def run_tool_for(
    business: Business, name: str, args: dict[str, Any], call_id: str = ""
) -> tuple[str, bool]:
    """Dispatch one tool call. Never raises: the API wants errors flagged.

    An exception here would drop the call. A flagged error lets the agent say
    "let me check that with the kitchen" and keep the caller on the line.
    """
    implementation = IMPLEMENTATIONS.get(name)
    if implementation is not None:
        try:
            return implementation(business, {**args, "call_id": call_id}), False
        except Exception:  # noqa: BLE001 - reported to the agent, not the caller
            log.exception("tool %s failed for %s", name, business.slug)
            return TOOL_FAILED, True

    menu_implementation = menu.IMPLEMENTATIONS.get(name)
    if menu_implementation is not None:
        try:
            return menu_implementation(business, args), False
        except Exception:  # noqa: BLE001
            log.exception("menu tool %s failed", name)
            return TOOL_FAILED, True

    return f"No tool named {name}.", True


def build_agent(business: Business, *, prompt_builder=None) -> AgentDefinition:
    """One AgentDefinition for one business. This is the multi-tenancy seam.

    The relay in session.py takes an AgentDefinition and knows nothing else.
    Binding the business HERE -- once, when the call connects -- is what makes
    a caller unable to talk their way into another venue's book: the identity
    was fixed before they said a word, and the model never sees this mapping.
    """
    from .agent_config import build_prompt_for, greeting_for

    return AgentDefinition(
        name=f"business:{business.slug}",
        display_name=business.config["identity"]["display_name"] or business.name,
        build_prompt=prompt_builder or (lambda: build_prompt_for(business)),
        greeting=greeting_for(business),
        tools=tool_declarations(business),
        run_tool=lambda name, args, call_id="": run_tool_for(business, name, args, call_id),
        voice=business.config["voice"]["voice_id"] or None,
    )


# --- resolving an agent for a call -------------------------------------------


def unconfigured_agent(reason: str) -> AgentDefinition:
    """What answers when no business could be resolved.

    A line that rings out costs the customer a booking; a line that answers and
    says it is not set up yet costs them nothing and tells whoever is testing
    exactly what is wrong. Never a stack trace into a live call.
    """
    message = (
        "Thanks for calling. This line is not finished being set up yet, "
        "so I cannot take a booking. Please try again shortly."
    )
    return AgentDefinition(
        name="unconfigured",
        display_name="Tableline",
        build_prompt=lambda: (
            "You are a phone line that has not been configured yet. Say so "
            "politely, apologise, and do not attempt to take a booking, answer "
            "questions about a business, or invent any detail. Keep it to one "
            f"sentence. The reason, for your own information only: {reason}."
        ),
        greeting=message,
        tools=[],
        run_tool=lambda name, args, call_id="": ("This line is not configured.", True),
    )


def agent_for(
    *,
    dialled_number: str | None = None,
    slug: str | None = None,
    business_id: str | None = None,
) -> AgentDefinition:
    """The agent for one call, resolved from whatever the transport knows.

    This is the whole of multi-tenancy at the edge: a phone transport passes
    the number that was dialled, the browser transport passes a slug, and
    everything below this line is already bound to one business.
    """
    from . import businesses

    try:
        business = businesses.resolve(
            business_id=business_id, slug=slug, dialled_number=dialled_number
        )
    except businesses.UnknownBusiness as exc:
        log.warning("no business for call (%s)", exc)
        return unconfigured_agent(str(exc))
    except Exception as exc:  # noqa: BLE001 - a database blip must not drop the call
        log.exception("could not resolve a business for this call")
        return unconfigured_agent(str(exc))

    if business.status != "active":
        return unconfigured_agent(f"business {business.slug} is {business.status}")
    return build_agent(business)


def default_agent() -> AgentDefinition:
    """The agent for a transport that carries no tenant, from DEFAULT_BUSINESS_SLUG."""
    from .config import settings

    slug = settings.default_business_slug.strip()
    if not slug:
        return unconfigured_agent("DEFAULT_BUSINESS_SLUG is not set")
    return agent_for(slug=slug)
