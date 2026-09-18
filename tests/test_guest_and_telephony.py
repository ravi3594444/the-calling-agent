"""The two surfaces a real phone call touches that nothing else does.

Both were written before a call had ever been placed, so both are exercised
here through the real app rather than by calling the functions directly. That
distinction caught a live bug: `request.form()` needs python-multipart, which
was not a declared dependency, so the Twilio webhook raised at REQUEST time
and nothing that imported the module would have noticed.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from calling_agent import bookings, businesses, holds
from calling_agent.config import settings
from calling_agent.main import app
from tests.conftest import future_slot, make_business, test_phone_number

client = TestClient(app)


def _booked(business, **kwargs):
    at = future_slot(business)
    held = holds.take(business, at, 2)
    return holds.confirm(
        business, held.hold_id, name=kwargs.get("name", "Ravi Sharma"),
        phone=kwargs.get("phone", "+919876543210"),
    )


# --- the guest's manage link -------------------------------------------------


def test_the_link_in_the_confirmation_text_actually_opens(business):
    """The whole reason this file exists: it used to 404."""
    booking = _booked(business)
    assert booking.manage_token, "no manage token was minted"

    page = client.get(f"/manage/{booking.manage_token}")

    assert page.status_code == 200
    assert booking.reference in page.text
    assert "Cancel this booking" in page.text


def test_a_guest_can_cancel_and_the_capacity_comes_back(business):
    booking = _booked(business)

    done = client.post(f"/manage/{booking.manage_token}/cancel", data={"confirm": "yes"})

    assert done.status_code == 200
    assert "cancelled" in done.text.lower()
    assert bookings.by_id(business, booking.id).status == "cancelled"


def test_cancelling_is_not_something_a_link_scanner_can_do(business):
    """Messaging apps fetch every URL they are sent.

    A cancellation behind a GET would be triggered by the text that announced
    the booking, before the guest had even read it.
    """
    booking = _booked(business)

    scanned = client.get(f"/manage/{booking.manage_token}")

    assert scanned.status_code == 200
    assert bookings.by_id(business, booking.id).status == "confirmed"


def test_an_unknown_or_tampered_token_says_the_same_thing(business):
    _booked(business)
    unknown = client.get("/manage/" + "z" * 43)
    assert unknown.status_code == 404
    assert "not valid" in unknown.text


def test_the_six_character_reference_is_not_a_credential(business):
    """It is read aloud on the phone and printed in the same text (PRD §12)."""
    booking = _booked(business)
    assert client.get(f"/manage/{booking.reference}").status_code == 404


def test_one_guest_s_link_never_opens_another_guest_s_booking(business):
    first = _booked(business, name="First", phone="+910000000001")
    second = _booked(business, name="Second", phone="+910000000002")

    page = client.get(f"/manage/{first.manage_token}")

    assert first.reference in page.text
    assert second.reference not in page.text


# --- the Twilio webhook ------------------------------------------------------


def test_the_voice_webhook_answers_with_a_stream(monkeypatch):
    """Would have failed on the first real call: form parsing needs a dependency."""
    monkeypatch.setattr(settings, "verify_twilio_signature", False)
    monkeypatch.setattr(settings, "token_pepper", "test-pepper")
    monkeypatch.setattr(settings, "public_hostname", "example.duckdns.org")

    business = make_business(phone_number=test_phone_number())

    answer = client.post(
        "/twilio/voice",
        data={"To": business.phone_number, "From": "+919876543210", "CallSid": "CA123"},
    )

    assert answer.status_code == 200
    assert "<Connect>" in answer.text
    assert "wss://example.duckdns.org/twilio/stream" in answer.text
    assert "ticket=" in answer.text, "the stream URL carries no signed ticket"


def test_an_unrouted_number_is_answered_politely_not_dropped(monkeypatch):
    """A caller who hears an apology is served better than a carrier error tone."""
    monkeypatch.setattr(settings, "verify_twilio_signature", False)

    answer = client.post(
        "/twilio/voice",
        data={"To": "+915555500000", "From": "+919876543210", "CallSid": "CA124"},
    )

    assert answer.status_code == 200
    assert "<Say>" in answer.text and "<Hangup/>" in answer.text


def test_an_unsigned_webhook_is_refused(monkeypatch):
    """Anyone who learns the URL could otherwise fill the call log."""
    monkeypatch.setattr(settings, "verify_twilio_signature", True)
    monkeypatch.setattr(settings, "twilio_auth_token", "not-the-real-token")

    refused = client.post(
        "/twilio/voice",
        data={"To": test_phone_number(), "From": "+919876543210", "CallSid": "CA125"},
        headers={"X-Twilio-Signature": "obviously-wrong"},
    )

    assert refused.status_code == 403


def test_a_stream_cannot_be_opened_without_a_valid_ticket(monkeypatch):
    """The tenant is decided from the dialled number, never by whoever connects."""
    monkeypatch.setattr(settings, "token_pepper", "test-pepper")

    import pytest
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/twilio/stream?ticket=forged"):
            pass


def test_a_call_answered_is_a_call_logged(monkeypatch):
    """The Calls screen is the renewal screen; a call must leave a row."""
    from sqlalchemy import text

    from calling_agent.db import readonly

    monkeypatch.setattr(settings, "verify_twilio_signature", False)
    monkeypatch.setattr(settings, "token_pepper", "test-pepper")

    business = make_business(phone_number=test_phone_number())
    client.post(
        "/twilio/voice",
        data={"To": business.phone_number, "From": "+919876543210", "CallSid": "CA200"},
    )

    with readonly() as conn:
        row = conn.execute(
            text(
                "SELECT caller_phone, provider, provider_call_id FROM calls"
                " WHERE business_id = :b"
            ),
            {"b": str(business.id)},
        ).fetchone()

    assert row is not None
    assert row.caller_phone == "+919876543210"
    assert row.provider == "twilio"
    assert row.provider_call_id == "CA200"


def test_the_dialled_number_picks_the_tenant(monkeypatch):
    """Two venues, two numbers, one server (PRD §17)."""
    monkeypatch.setattr(settings, "verify_twilio_signature", False)
    monkeypatch.setattr(settings, "token_pepper", "test-pepper")

    one = make_business(phone_number=test_phone_number(), name="Venue One")
    two = make_business(phone_number=test_phone_number(), name="Venue Two")

    from calling_agent.api.telephony import read_stream_ticket

    for venue in (one, two):
        answer = client.post(
            "/twilio/voice",
            data={"To": venue.phone_number, "From": "+919876543210", "CallSid": "CA1"},
        )
        ticket = answer.text.split("ticket=")[1].split("&quot;")[0].split('"')[0]
        business_id, _ = read_stream_ticket(ticket)
        assert business_id == str(venue.id), "the call was routed to the wrong venue"
        assert businesses.by_id(business_id).name == venue.name


# --- telephone mode in the browser -------------------------------------------


def test_the_browser_can_run_at_telephone_fidelity():
    """PRD §16 gates the provider choice on how barge-in feels ON A PHONE.

    Judging that on 24 kHz browser audio answers a question nobody asked, so
    the browser transport can be asked for 8 kHz mu-law -- the same bytes a
    call carries.
    """
    from calling_agent import protocol
    from calling_agent.transport import BrowserTransport

    phone = BrowserTransport(ws=None, encoding="pcmu")
    assert phone.encoding == protocol.ENCODING_PCMU
    assert phone.sample_rate == 8000

    browser = BrowserTransport(ws=None)
    assert browser.encoding == protocol.ENCODING_PCM
    assert browser.sample_rate == 24000


def test_an_unknown_encoding_costs_fidelity_not_the_call():
    from calling_agent import protocol
    from calling_agent.transport import BrowserTransport

    assert BrowserTransport(ws=None, encoding="wav").encoding == protocol.ENCODING_PCM


def test_the_session_payload_pins_input_and_output_to_the_same_encoding(business):
    """Mismatched formats mean the agent talks and the caller hears nothing."""
    from calling_agent import agent_tools, protocol
    from calling_agent.agent_config import build_session_update

    payload = build_session_update(
        protocol.ENCODING_PCMU, agent=agent_tools.build_agent(business)
    )["session"]

    assert payload["input"]["format"]["encoding"] == protocol.ENCODING_PCMU
    assert payload["output"]["format"]["encoding"] == protocol.ENCODING_PCMU
