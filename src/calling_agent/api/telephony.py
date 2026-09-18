"""Twilio webhooks: the seam between a phone number and an agent session.

THE TENANT IS THE DIALLED NUMBER (PRD §17). Twilio posts `To` -- the number the
caller rang -- and that resolves the business before a single word is spoken.
A caller cannot talk their way into another venue's book, because the binding
happened before they said anything and the model never sees this mapping.

CONDITIONAL FORWARDING means the business keeps its own number: their line
forwards to ours when it is busy or unanswered. Twilio then sends the
forwarding number as `To` and the original caller as `From`, which is exactly
the shape this needs.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import text

from .. import agent_tools, businesses, notifications, sms_replies
from ..agent_spec import AgentDefinition
from ..call_log import CallRecorder
from ..config import settings
from ..db import fetch_one, readonly, transaction
from ..session import AgentSession
from ..transport import TwilioTransport

log = logging.getLogger(__name__)

router = APIRouter(tags=["telephony"])


@router.post("/twilio/voice")
async def incoming_call(request: Request) -> Response:
    """Answer an inbound call with TwiML that opens the media stream.

    Returns TwiML even when the number is unknown: a caller who hears a short
    apology has been served better than one who hears a carrier error tone,
    and the log line tells us which number needs onboarding.
    """
    form = await _verified_form(request)
    dialled = (form.get("To") or "").strip()
    caller = (form.get("From") or "").strip()
    call_sid = (form.get("CallSid") or "").strip()

    business = _business_for(dialled)
    if business is None:
        log.warning("call to unrouted number %s", dialled)
        return _twiml(
            "<Response><Say>Sorry, this number is not set up to take bookings "
            "yet.</Say><Hangup/></Response>"
        )

    call_id = _open_call_record(business.id, caller, call_sid)
    ticket = issue_stream_ticket(business.id, call_id)
    stream_url = f"{_stream_url(request)}?ticket={ticket}"

    # The websocket has no form body and no signature, so anything naming a
    # business in that URL would be taken on trust. A signed, short-lived
    # ticket says the same thing and cannot be forged or replayed tomorrow.
    return _twiml(
        "<Response>"
        f'<Connect><Stream url="{_xml_escape(stream_url)}" /></Connect>'
        "</Response>"
    )


@router.post("/twilio/sms")
async def incoming_sms(request: Request) -> Response:
    """A guest texted the venue's number back. Usually "C".

    The reply goes back as TwiML so it lands in seconds. An unrouted number
    gets no reply at all: answering a text to a number we do not serve would
    tell whoever is probing which numbers are ours.
    """
    form = await _verified_form(request)
    ours = (form.get("To") or "").strip()
    sender = (form.get("From") or "").strip()
    body = form.get("Body") or ""

    business = _business_for(ours)
    if business is None:
        log.warning("text to unrouted number %s", ours)
        return _twiml("<Response/>")

    reply = sms_replies.handle(business, sender, body, our_number=ours)
    if not reply:
        return _twiml("<Response/>")
    return _twiml(f"<Response><Message>{_xml_escape(reply)}</Message></Response>")


@router.post("/twilio/status")
async def call_status(request: Request) -> Response:
    """Close the call record when Twilio says the call ended."""
    form = await _verified_form(request)
    call_sid = (form.get("CallSid") or "").strip()
    duration = form.get("CallDuration")
    recording_url = form.get("RecordingUrl")

    if not call_sid:
        return Response(status_code=204)

    # Only a call we placed has a message behind it; for an inbound call this
    # finds nothing and does nothing.
    notifications.call_ended(call_sid, (form.get("CallStatus") or "").strip())

    with transaction() as conn:
        conn.execute(
            text(
                "UPDATE calls SET ended_at = COALESCE(ended_at, now()),"
                " duration_s = COALESCE(:duration, duration_s),"
                " recording_url = COALESCE(:recording, recording_url)"
                " WHERE provider_call_id = :sid"
            ),
            {
                "duration": int(duration) if duration else None,
                "recording": recording_url,
                "sid": call_sid,
            },
        )
    return Response(status_code=204)


@router.websocket("/twilio/stream")
async def media_stream(websocket: WebSocket, ticket: str = "") -> None:
    """Bridge one phone call to one agent session.

    The tenant comes from the signed ticket minted when the call was answered,
    so it is decided once, from the dialled number, and cannot be chosen by
    whoever opens this socket.
    """
    claim = read_stream_ticket(ticket)
    if claim is None:
        log.warning("refused a media stream with a missing or stale ticket")
        await websocket.close(code=1008)
        return

    await websocket.accept()
    business_id, call_id, message_id = claim
    agent = (
        _callback_agent(business_id, call_id, message_id)
        if message_id
        else agent_tools.agent_for(business_id=business_id, call_id=call_id)
    )
    await AgentSession(
        TwilioTransport(websocket),
        agent=agent,
        recorder=CallRecorder(business_id=business_id, call_id=call_id),
    ).run()
    await _close_call(call_id)


def _callback_agent(business_id: str, call_id: str, message_id: str) -> AgentDefinition:
    """The agent for a call we placed: the usual one, told why it rang."""
    from dataclasses import replace

    from ..agent_config import build_outbound_prompt, outbound_greeting

    try:
        business = businesses.by_id(business_id)
    except businesses.UnknownBusiness:
        return agent_tools.unconfigured_agent(f"no business {business_id}")

    with readonly() as conn:
        message = fetch_one(
            conn,
            "SELECT m.body, b.name FROM messages m"
            " LEFT JOIN bookings b ON b.id = m.booking_id"
            " WHERE m.id = :i AND m.business_id = :b",
            i=message_id,
            b=business_id,
        )
    if message is None:
        return agent_tools.unconfigured_agent(f"no message {message_id}")

    guest = (message.name or "").strip()
    agent = agent_tools.build_agent(
        business,
        call_id=call_id,
        prompt_builder=lambda: build_outbound_prompt(business, body=message.body, guest_name=guest),
    )
    return replace(agent, greeting=outbound_greeting(business, guest))


@router.post("/twilio/outbound")
async def answered_callback(request: Request, m: str = "") -> Response:
    """Twilio fetches this when a call WE placed is answered.

    Same shape as /twilio/voice, with one more fact: which message this call
    is delivering. Trusting `m` is safe because Twilio signs the request,
    URL included, and _verified_form refuses anything it did not sign.
    """
    form = await _verified_form(request)
    call_sid = (form.get("CallSid") or "").strip()
    answered = (form.get("To") or "").strip()

    with readonly() as conn:
        message = fetch_one(
            conn,
            "SELECT id, business_id FROM messages WHERE id = :i AND channel = 'voice'",
            i=m,
        ) if m else None
    if message is None:
        log.warning("answered callback for unknown message %r", m)
        return _twiml("<Response><Hangup/></Response>")

    call_id = _open_call_record(
        message.business_id, answered, call_sid, direction="outbound"
    )
    ticket = issue_stream_ticket(message.business_id, call_id, str(message.id))
    stream_url = f"{_stream_url(request)}?ticket={ticket}"
    return _twiml(
        "<Response>"
        f'<Connect><Stream url="{_xml_escape(stream_url)}" /></Connect>'
        "</Response>"
    )


# --- stream tickets ----------------------------------------------------------

#: How long a ticket is good for. Twilio opens the stream within a second or
#: two of fetching the TwiML; a minute is generous and still useless to anyone
#: who finds the URL in a log tomorrow.
TICKET_TTL_SECONDS = 120
_TICKET_SALT = "tableline.twilio.stream"


def _serializer() -> URLSafeTimedSerializer:
    # Falls back to the Twilio auth token so a deployment that set up telephony
    # but not TOKEN_PEPPER still signs with something only it knows.
    secret = settings.token_pepper or settings.twilio_auth_token
    if not secret:
        raise HTTPException(500, "set TOKEN_PEPPER before taking phone calls")
    return URLSafeTimedSerializer(secret, salt=_TICKET_SALT)


def issue_stream_ticket(
    business_id: UUID | str, call_id: str, message_id: str | None = None
) -> str:
    claim = {"b": str(business_id), "c": call_id}
    if message_id:
        # Set only on a call WE placed: the stream then knows why it rang.
        claim["m"] = message_id
    return _serializer().dumps(claim)


def read_stream_ticket(ticket: str) -> tuple[str, str, str | None] | None:
    """The business, call and (for a callback) message this stream is for.

    None if it cannot be trusted.
    """
    if not ticket:
        return None
    try:
        claim = _serializer().loads(ticket, max_age=TICKET_TTL_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    except HTTPException:
        raise
    return str(claim.get("b", "")), str(claim.get("c", "")), claim.get("m") or None


# --- helpers -----------------------------------------------------------------


async def _verified_form(request: Request) -> dict[str, str]:
    """Read the form, refusing anything Twilio did not sign.

    Unsigned webhooks are an open door: anyone who learns the URL can open
    calls, fill the call log, and make the agent dial out. Verification is on
    by default and turning it off takes a deliberate environment variable.
    """
    form = {k: str(v) for k, v in (await request.form()).items()}

    if not settings.verify_twilio_signature:
        return form
    if not settings.twilio_auth_token:
        raise HTTPException(500, "TWILIO_AUTH_TOKEN is required to verify webhooks")

    signature = request.headers.get("X-Twilio-Signature", "")
    if not _signature_valid(str(request.url), form, signature):
        log.warning("refused a Twilio webhook with a bad signature")
        raise HTTPException(403, "bad signature")
    return form


def _signature_valid(url: str, form: dict[str, str], signature: str) -> bool:
    """Twilio's own validator, not a hand-rolled HMAC."""
    from twilio.request_validator import RequestValidator

    return RequestValidator(settings.twilio_auth_token).validate(url, form, signature)


def _business_for(dialled_number: str):
    if not dialled_number:
        return None
    try:
        return businesses.by_phone(dialled_number)
    except businesses.UnknownBusiness:
        return None


def _open_call_record(
    business_id: UUID | str, caller: str, call_sid: str, *, direction: str = "inbound"
) -> str:
    with transaction() as conn:
        row = fetch_one(
            conn,
            "INSERT INTO calls (business_id, caller_phone, provider, provider_call_id,"
            " outcome, resolved, direction)"
            " VALUES (:b, :caller, 'twilio', :sid, 'in_progress', true, :dir)"
            " RETURNING id",
            b=str(business_id),
            caller=caller,
            sid=call_sid,
            dir=direction,
        )
    return str(row.id)


async def _close_call(call_id: str | None) -> None:
    if not call_id:
        return
    with transaction() as conn:
        conn.execute(
            text(
                "UPDATE calls SET ended_at = now(),"
                " duration_s = COALESCE(duration_s,"
                "   EXTRACT(EPOCH FROM (now() - started_at))::int)"
                " WHERE id = :i AND ended_at IS NULL"
            ),
            {"i": call_id},
        )


def _stream_url(request: Request) -> str:
    """The wss:// URL Twilio should open back to us."""
    host = settings.public_hostname.strip() or request.url.netloc
    return f"wss://{host}/twilio/stream"


def _twiml(body: str) -> Response:
    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?>{body}',
        media_type="application/xml",
    )


def _xml_escape(value: str) -> str:
    from xml.sax.saxutils import quoteattr

    return quoteattr(value)[1:-1]


