"""What a guest's text back to us means, and what to say in return (PRD §12).

Every confirmation and reminder ends "reply C to cancel". This is the other
half of that sentence. It is small on purpose: a text is one word from a
person holding a phone, not a conversation, so there are exactly three
answers -- cancel, the alternative, and "here is what you can say".

WHO MAY CANCEL
The number the text came from. Not a reference typed into the body: PRD §12
says the six-character reference is read aloud and printed, so it is not a
credential and never authorises anything. The phone number the booking was
made with is -- the same standard the agent applies on a call when it asks
for the name on the booking.

`handle()` logs the inbound text first, whatever it says, so the call log has
the guest's side of the exchange even when the word was one we do not know.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from . import bookings, dates, formatting
from .businesses import Business
from .db import transaction

log = logging.getLogger(__name__)

CANCEL_WORDS = {"C", "CANCEL"}
YES_WORDS = {"Y", "YES"}


def handle(business: Business, sender: str, body: str, *, our_number: str = "") -> str | None:
    """Act on one inbound text and return the reply, or None for no reply."""
    # Twilio sends E.164 already; normalising anyway means a number stored
    # from a call ("98765 43210", read to the agent) still matches the one
    # that texts us back, because both went through the same door.
    sender = dates.normalise_phone(sender or "", region=business.config["locale"]["country"])
    body = (body or "").strip()
    _record(business, sender, our_number, body)
    if not sender or not body:
        return None

    word = body.split()[0].upper().strip(".!")
    venue = business.config["identity"]["display_name"] or business.name

    if word in CANCEL_WORDS:
        return _cancel_next(business, sender, venue)
    if word in YES_WORDS:
        # The decline text says "call us to take it". Until an alternative is
        # held against the reply, saying otherwise here would be a promise.
        return f"{venue}: to take that time, please call us and we will hold it for you."
    return f"{venue}: reply C to cancel your next booking."


def _cancel_next(business: Business, sender: str, venue: str) -> str:
    upcoming = bookings.upcoming_for_phone(business, sender, limit=1)
    if not upcoming:
        return f"{venue}: we can't find an upcoming booking for this number."

    booking = upcoming[0]
    if booking.status not in (bookings.CONFIRMED, bookings.PENDING):
        return f"{venue}: your booking {booking.reference} is already {booking.status}."

    # This reply IS the cancellation notice, so the queued one is skipped:
    # two texts saying the same thing thirty seconds apart reads as a fault.
    bookings.cancel(business, booking.id, actor="sms", notify=False)
    log.info("booking %s cancelled by text for %s", booking.reference, business.slug)
    return (
        f"{venue}: cancelled, {formatting.when(business, booking.start_time)}, "
        f"reference {booking.reference}. Sorry to miss you."
    )


def _record(business: Business, sender: str, ours: str, body: str) -> None:
    """The guest's side of the exchange, kept whatever it said."""
    try:
        with transaction() as conn:
            conn.execute(
                text(
                    "INSERT INTO messages (business_id, direction, channel, from_address,"
                    " to_address, body, status, kind)"
                    " VALUES (:b, 'inbound', 'sms', :frm, :to, :body, 'received', 'reply')"
                ),
                {"b": str(business.id), "frm": sender, "to": ours, "body": body[:1000]},
            )
    except Exception:  # noqa: BLE001 - a lost log line must not lose the reply
        log.exception("could not record an inbound text for %s", business.slug)
