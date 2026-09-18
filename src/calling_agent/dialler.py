"""Placing a call, so a queued voice message actually rings (PRD §13).

A BOUNDARY over the telephony vendor, the same shape as the SMS providers in
`notifications` and the menu readers in `menu_reader`: one module knows how
to make a phone ring, and nothing else imports the vendor.

An outbound call here is a MESSAGE delivered by voice. The row in `messages`
already holds who to reach and what to tell them; this only dials. When the
guest answers, Twilio fetches TwiML from us, we open the same media stream an
inbound call uses, and the agent is handed the message body as the reason it
rang -- see `agent_config.build_outbound_prompt`.

WHY THE DEFAULT IS "log"
The same reason SMS defaults to it: a fresh checkout with a Twilio key in the
env must not ring a real person on its first tick. VOICE_PROVIDER=twilio is a
deliberate act.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from uuid import UUID, uuid4

from .businesses import Business
from .config import settings

log = logging.getLogger(__name__)

#: Seconds to let it ring. Twilio's default is 60; nobody wants a minute of a
#: restaurant ringing them back, and a missed ring is retried anyway.
RING_SECONDS = 25


class DialError(Exception):
    """The call could not be placed. The message stays in the queue."""


Placer = Callable[[str, Business, str, str], str]
PROVIDERS: dict[str, Placer] = {}


def provider(name: str) -> Callable[[Placer], Placer]:
    def register(fn: Placer) -> Placer:
        PROVIDERS[name] = fn
        return fn

    return register


@provider("log")
def _log(to: str, business: Business, answer_url: str, status_url: str) -> str:
    """Rings nobody. Records that it would have, and returns a fake call id."""
    log.info("[call:log] %s -> %s (answer %s)", business.slug, to, answer_url)
    # Unique, like a real SID: call_ended() finds the message by this value,
    # and a timestamp let two calls in one second answer for each other.
    return f"log-call-{uuid4().hex[:16]}"


@provider("twilio")
def _twilio(to: str, business: Business, answer_url: str, status_url: str) -> str:
    import httpx

    sid = settings.twilio_account_sid
    token = settings.twilio_auth_token
    sender = settings.twilio_from_number or business.phone_number
    if not (sid and token and sender):
        raise DialError("twilio needs TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and a from number")

    response = httpx.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json",
        auth=(sid, token),
        data={
            "To": to,
            "From": sender,
            "Url": answer_url,
            "Method": "POST",
            "StatusCallback": status_url,
            "StatusCallbackMethod": "POST",
            "StatusCallbackEvent": "completed",
            "Timeout": str(RING_SECONDS),
        },
        timeout=15,
    )
    if response.status_code >= 400:
        # The body names the account and the number. Log it; do not raise it
        # into a queue error column that a dashboard might one day show.
        log.error("twilio refused the call: %s %s", response.status_code, response.text[:500])
        raise DialError(f"twilio refused the call (HTTP {response.status_code})")
    return str(response.json().get("sid", ""))


def configured() -> bool:
    return settings.voice_provider in PROVIDERS


def place(business: Business, message_id: UUID | str, to: str) -> str:
    """Ring `to` about message `message_id`. Returns the vendor's call id.

    The answer URL carries the message id in the clear. That is safe for the
    same reason /twilio/voice trusts the number it is handed: Twilio signs
    every request it makes to us, including the URL, and we verify it. A
    forged request naming someone else's message fails that check.
    """
    placer = PROVIDERS.get(settings.voice_provider)
    if placer is None:
        raise DialError(f"VOICE_PROVIDER={settings.voice_provider!r} is not registered")

    host = settings.public_hostname.strip()
    if not host:
        raise DialError("PUBLIC_HOSTNAME must be set for Twilio to reach us when they answer")

    answer_url = f"https://{host}/twilio/outbound?m={message_id}"
    status_url = f"https://{host}/twilio/status"
    call_id = placer(to, business, answer_url, status_url)
    log.info("placed call %s for %s", call_id, business.slug)
    return call_id
