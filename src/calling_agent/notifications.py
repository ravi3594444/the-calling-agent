"""Telling people things: SMS now, a queued call later (PRD §12, §13).

Two rules shape this module.

NOTHING IS SENT FROM A REQUEST HANDLER. Every message is written to `messages`
as `queued` inside the same transaction as the thing that caused it, and a
worker sends it. A booking that succeeded and a text that failed must not be
the same failure -- and a tool endpoint has 200ms (PRD §8), which is not
enough to wait on a carrier.

THE PROVIDER IS A PLUG. `SMS_PROVIDER=log` records the message and sends
nothing, which is what a demo and a test want. `twilio` sends. Adding a third
is a function in the registry below, not a change anywhere else in the
codebase -- this agent is meant to drop into somebody else's system, and
theirs may not be Twilio.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from . import business_config as bc
from . import businesses, formatting
from .businesses import Business
from .config import settings
from .db import fetch_all, fetch_one, transaction

log = logging.getLogger(__name__)


# --- what a message is -------------------------------------------------------


@dataclass(frozen=True)
class Outgoing:
    id: UUID
    business_id: UUID
    to: str
    body: str
    channel: str
    kind: str


def manage_url(business: Business, token: str | None) -> str:
    """The guest's link. Empty when nothing was minted, never a broken URL."""
    base = (settings.dashboard_base_url or business.config["identity"]["public_url"]).rstrip("/")
    if not base or not token:
        return ""
    return f"{base}/manage/{token}"


def template_values(
    business: Business,
    *,
    booking: Any = None,
    alternative: datetime | None = None,
    manage_token: str | None = None,
) -> dict[str, Any]:
    """Everything a message template may name (see business_config.TEMPLATE_FIELDS)."""
    identity = business.config["identity"]
    values: dict[str, Any] = {
        "display_name": identity["display_name"] or business.name,
        "agent_name": identity["agent_name"],
        "address": identity["address"],
        "currency_symbol": business.config["locale"]["currency_symbol"],
        "unit_label": business.unit_plural,
        "phone": business.phone_number or "",
    }
    if booking is not None:
        start = _as_datetime(booking.start_time)
        values.update(
            {
                "reference": booking.reference,
                "name": booking.name,
                "party_size": booking.party_size,
                "date": formatting.date_short(business, start),
                "date_long": formatting.date_long(business, start),
                "time": formatting.time_str(business, start),
                "manage_url": manage_url(business, manage_token),
            }
        )
    if alternative is not None:
        values["alternative"] = formatting.when(business, alternative)
    return values


def render(business: Business, kind: str, values: dict[str, Any]) -> str:
    template = business.config["messaging"]["templates"].get(kind)
    if not template:
        return ""
    return bc.render_template(template, values)


# --- queueing ----------------------------------------------------------------


def queue(
    conn: Connection,
    business: Business,
    *,
    kind: str,
    to: str,
    body: str,
    booking_id: UUID | str | None = None,
    customer_id: UUID | str | None = None,
    channel: str = "sms",
) -> UUID | None:
    """Write one message, inside the caller's transaction. Never sends.

    Returns None when there is nothing to send -- no number, no body, or the
    venue turned this message off. A withheld caller id is ordinary, and a
    booking must not fail because we could not text about it.
    """
    to = (to or "").strip()
    if not to or not body.strip():
        return None
    row = fetch_one(
        conn,
        "INSERT INTO messages (business_id, booking_id, customer_id, direction, channel,"
        " to_address, body, status, kind) VALUES (:b, :bk, :c, 'outbound', :ch, :to, :body,"
        " 'queued', :kind) RETURNING id",
        b=str(business.id),
        bk=str(booking_id) if booking_id else None,
        c=str(customer_id) if customer_id else None,
        ch=channel,
        to=to,
        body=body.strip(),
        kind=kind,
    )
    return row.id if row else None


def queue_for_booking(
    conn: Connection,
    business: Business,
    booking: Any,
    kind: str,
    *,
    alternative: datetime | None = None,
    manage_token: str | None = None,
    channel: str = "sms",
) -> UUID | None:
    """Queue the message this booking's config says belongs to this event."""
    messaging = business.config["messaging"]
    if kind == "confirmed" and not messaging["send_confirmation"]:
        return None
    if kind == "reminder" and not messaging["send_reminder"]:
        return None
    if _do_not_call(conn, business.id, booking.phone) and channel == "voice":
        return None
    body = render(
        business,
        kind,
        template_values(
            business, booking=booking, alternative=alternative, manage_token=manage_token
        ),
    )
    return queue(
        conn,
        business,
        kind=kind,
        to=booking.phone,
        body=body,
        booking_id=booking.id,
        customer_id=getattr(booking, "customer_id", None),
        channel=channel,
    )


def queue_outbound_task(
    conn: Connection,
    business: Business,
    *,
    reason: str,
    booking_id: UUID | str | None = None,
    payload: dict[str, Any] | None = None,
    scheduled_for: datetime | None = None,
    dedupe_key: str | None = None,
) -> None:
    """A job for later: a reminder, an overflow timeout, a callback.

    `dedupe_key` makes re-queueing idempotent, which matters because the thing
    that queues a reminder is a booking edit, and a host who taps Save twice
    must not cause two texts at ten in the morning.
    """
    import json

    conn.execute(
        text(
            "INSERT INTO outbound_tasks (business_id, booking_id, reason, payload,"
            " scheduled_for, dedupe_key) VALUES (:b, :bk, :reason, CAST(:payload AS jsonb),"
            " :at, :dedupe) ON CONFLICT (business_id, dedupe_key)"
            " WHERE dedupe_key IS NOT NULL DO UPDATE SET"
            " scheduled_for = EXCLUDED.scheduled_for, payload = EXCLUDED.payload,"
            " status = 'queued', updated_at = now()"
        ),
        {
            "b": str(business.id),
            "bk": str(booking_id) if booking_id else None,
            "reason": reason,
            "payload": json.dumps(payload or {}),
            "at": scheduled_for or datetime.now(UTC),
            "dedupe": dedupe_key,
        },
    )


def cancel_tasks_for(conn: Connection, booking_id: UUID | str, reasons: list[str]) -> None:
    """Drop queued work that a status change made pointless.

    A cancelled booking must not still get its day-before reminder; that text
    is how a guest learns we did not notice they cancelled.
    """
    conn.execute(
        text(
            "UPDATE outbound_tasks SET status = 'cancelled', updated_at = now()"
            " WHERE booking_id = :bk AND status = 'queued' AND reason = ANY(:reasons)"
        ),
        {"bk": str(booking_id), "reasons": reasons},
    )


def _do_not_call(conn: Connection, business_id: UUID, phone: str) -> bool:
    """PRD §13: do_not_call is absolute."""
    if not phone:
        return False
    row = fetch_one(
        conn,
        "SELECT do_not_call FROM customers WHERE business_id = :b AND phone_e164 = :p",
        b=str(business_id),
        p=phone,
    )
    return bool(row and row.do_not_call)


# --- sending -----------------------------------------------------------------

#: name -> (to, body, business) -> provider message id. Register another and
#: SMS_PROVIDER can name it; nothing else in the codebase changes.
PROVIDERS: dict[str, Callable[[str, str, Business], str]] = {}


def provider(name: str) -> Callable[[Callable[[str, str, Business], str]], Any]:
    def register(fn: Callable[[str, str, Business], str]):
        PROVIDERS[name] = fn
        return fn

    return register


@provider("log")
def _log_provider(to: str, body: str, business: Business) -> str:
    """Records and sends nothing. The default, so a fresh checkout cannot text
    a real person by accident on its first run."""
    log.info("[sms:log] %s -> %s: %s", business.slug, to, body)
    return f"log-{datetime.now(UTC).timestamp():.0f}"


@provider("twilio")
def _twilio_provider(to: str, body: str, business: Business) -> str:
    import httpx

    sid = settings.twilio_account_sid
    token = settings.twilio_auth_token
    sender = settings.twilio_from_number or business.phone_number
    if not (sid and token and sender):
        raise RuntimeError(
            "twilio provider needs TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and a from number"
        )
    response = httpx.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        auth=(sid, token),
        data={"To": to, "From": sender, "Body": body},
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("sid", "")


def send_queued(limit: int = 50) -> int:
    """Send what is queued. Called by the worker, never by a request.

    Rows are claimed with SKIP LOCKED so two workers share the queue rather
    than sending the same text twice.
    """
    sender = PROVIDERS.get(settings.sms_provider)
    if sender is None:
        log.error("SMS_PROVIDER=%r is not registered; nothing will send", settings.sms_provider)
        return 0

    sent = 0
    with transaction() as conn:
        rows = fetch_all(
            conn,
            "SELECT m.id, m.business_id, m.to_address, m.body, m.channel, m.kind FROM messages m"
            " WHERE m.status = 'queued' AND m.direction = 'outbound'"
            " ORDER BY m.created_at LIMIT :limit FOR UPDATE SKIP LOCKED",
            limit=limit,
        )
        for row in rows:
            business = businesses.by_id(row.business_id)
            try:
                provider_id = sender(row.to_address, row.body, business)
            except Exception as exc:  # noqa: BLE001 - one bad number must not stop the queue
                log.warning("message %s failed: %s", row.id, exc)
                conn.execute(
                    text(
                        "UPDATE messages SET status = 'failed', error = :e WHERE id = :i"
                    ),
                    {"e": str(exc)[:500], "i": str(row.id)},
                )
                continue
            conn.execute(
                text(
                    "UPDATE messages SET status = 'sent', provider_id = :p, sent_at = now()"
                    " WHERE id = :i"
                ),
                {"p": provider_id, "i": str(row.id)},
            )
            sent += 1
    return sent


# --- scheduling helpers ------------------------------------------------------


def schedule_reminder(conn: Connection, business: Business, booking: Any) -> None:
    """Queue the day-before reminder, if this venue sends them."""
    messaging = business.config["messaging"]
    if not messaging["send_reminder"]:
        return
    start = _as_datetime(booking.start_time)
    at = start - timedelta(hours=int(messaging["reminder_hours_before"]))
    if at <= datetime.now(UTC):
        return
    queue_outbound_task(
        conn,
        business,
        reason="reminder",
        booking_id=booking.id,
        scheduled_for=at,
        dedupe_key=f"reminder:{booking.id}",
    )


def within_calling_window(business: Business, moment: datetime | None = None) -> bool:
    """PRD §13: a configurable local window, default 09:00-20:00."""
    outbound = business.config["outbound"]
    at = formatting.local(business, moment or datetime.now(UTC)).time()
    start = _parse_time(outbound["window_start"])
    end = _parse_time(outbound["window_end"])
    return start <= at < end


def _parse_time(value: str):
    from datetime import time as _time

    hour, minute = (int(p) for p in value.split(":")[:2])
    return _time(hour, minute)


def _as_datetime(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
