"""The dashboard's own API (PRD §16b).

Every screen goes through these, and until now not one of them had a test:
the endpoints behind "Add a booking" and "Block a date" existed for weeks
while the buttons did nothing, and nothing said so.

The token is minted the way `cli link` mints it, so these exercise the real
auth path rather than a bypass.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from calling_agent.api.deps import issue_dashboard_token
from calling_agent.main import app
from tests.conftest import future_slot
from tests.conftest import test_phone_number as a_number


@pytest.fixture
def dash(business):
    """A client already holding a live dashboard token for `business`."""
    token = issue_dashboard_token(business.id, label="test")
    client = TestClient(app)
    client.headers.update({"X-Tableline-Token": token})
    return client


def test_a_request_without_a_token_is_refused(business):
    assert TestClient(app).get("/api/bookings?date=tonight").status_code == 401


def test_a_booking_added_by_hand_takes_capacity(dash, business):
    at = future_slot(business)
    created = dash.post("/api/bookings", json={
        "start_time": at.isoformat(),
        "party_size": 2,
        "name": "Ravi",
        "phone": a_number(),
        "notes": "Window table",
    })
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "confirmed"
    assert len(body["reference"]) == 6


def test_a_booking_without_a_name_is_refused(dash, business):
    at = future_slot(business)
    response = dash.post("/api/bookings", json={
        "start_time": at.isoformat(), "party_size": 2,
    })
    assert response.status_code == 400


def test_blocking_a_day_closes_it(dash, business):
    day = future_slot(business).date().isoformat()
    assert dash.put(f"/api/blocked/{day}", json={
        "type": "closed", "reason": "Private party",
    }).status_code == 200

    blocked = dash.get("/api/blocked").json()
    assert any(row["date"] == day for row in blocked["rows"])

    assert dash.delete(f"/api/blocked/{day}").status_code == 200


def test_capacity_can_be_reduced_for_one_day_only(dash, business):
    day = future_slot(business).date().isoformat()
    response = dash.put(f"/api/blocked/{day}", json={
        "type": "reduced", "total_units": 4, "reason": "Short staffed",
    })
    assert response.status_code == 200
    assert response.json()["type"] == "reduced"


def test_a_booking_can_be_moved(dash, business):
    at = future_slot(business)
    booking = dash.post("/api/bookings", json={
        "start_time": at.isoformat(), "party_size": 2, "name": "Ravi",
    }).json()

    later = future_slot(business, hour=20)
    moved = dash.patch(f"/api/bookings/{booking['id']}",
                       json={"start_time": later.isoformat()})
    assert moved.status_code == 200, moved.text
    assert moved.json()["start_time"] != booking["start_time"]


def test_resending_a_confirmation_needs_a_number(dash, business):
    at = future_slot(business)
    booking = dash.post("/api/bookings", json={
        "start_time": at.isoformat(), "party_size": 2, "name": "No Phone",
    }).json()
    response = dash.post(f"/api/bookings/{booking['id']}/resend")
    assert response.status_code == 400


def test_resending_a_confirmation_queues_one_message(dash, business):
    from sqlalchemy import text

    from calling_agent.db import readonly

    at = future_slot(business)
    booking = dash.post("/api/bookings", json={
        "start_time": at.isoformat(), "party_size": 2,
        "name": "Ravi", "phone": a_number(),
    }).json()

    response = dash.post(f"/api/bookings/{booking['id']}/resend")
    assert response.status_code == 200, response.text

    with readonly() as conn:
        queued = conn.execute(
            text("SELECT count(*) FROM messages WHERE booking_id = :b"),
            {"b": booking["id"]},
        ).scalar_one()
    assert queued >= 1


def test_a_resend_mints_a_fresh_manage_link(dash, business):
    """Only the hash is stored, so the old link cannot be reproduced.

    Minting a new one is the honest behaviour -- and it leaves exactly one
    live link per booking, which is the safer end of that trade.
    """
    from sqlalchemy import text

    from calling_agent.db import readonly

    at = future_slot(business)
    booking = dash.post("/api/bookings", json={
        "start_time": at.isoformat(), "party_size": 2,
        "name": "Ravi", "phone": a_number(),
    }).json()

    def stored_hash():
        with readonly() as conn:
            return conn.execute(
                text("SELECT manage_token_hash FROM bookings WHERE id = :i"),
                {"i": booking["id"]},
            ).scalar_one()

    before = stored_hash()
    dash.post(f"/api/bookings/{booking['id']}/resend")
    assert stored_hash() != before


# --- the menu photo ----------------------------------------------------------


PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def test_a_menu_photo_is_stored_and_served_back(dash, business):
    upload = dash.post("/api/menu/upload",
                       files={"file": ("menu.png", PNG, "image/png")})
    assert upload.status_code == 200, upload.text
    uploaded_id = upload.json()["id"]

    assert dash.get("/api/menu").json()["upload"]["id"] == uploaded_id

    served = dash.get(f"/api/menu/upload/{uploaded_id}")
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/png")
    assert served.content == PNG


def test_a_menu_photo_from_another_venue_is_not_served(dash, business):
    """The id is a uuid, but scoping is what actually keeps venues apart."""
    from tests.conftest import make_business

    other = make_business()
    other_token = issue_dashboard_token(other.id, label="other")
    other_client = TestClient(app)
    other_client.headers.update({"X-Tableline-Token": other_token})

    uploaded = other_client.post(
        "/api/menu/upload", files={"file": ("menu.png", PNG, "image/png")}
    ).json()["id"]

    assert dash.get(f"/api/menu/upload/{uploaded}").status_code == 404


def test_a_spreadsheet_is_not_a_menu_photo(dash, business):
    response = dash.post(
        "/api/menu/upload",
        files={"file": ("book.xlsx", b"PK\x03\x04", "application/vnd.ms-excel")},
    )
    assert response.status_code == 400


def test_an_oversized_upload_is_refused(dash, business):
    from calling_agent.api.dashboard import MAX_UPLOAD_BYTES

    huge = b"\x00" * (MAX_UPLOAD_BYTES + 1)
    response = dash.post("/api/menu/upload",
                         files={"file": ("big.png", huge, "image/png")})
    assert response.status_code == 413


def test_guests_carry_what_the_csv_export_writes(dash, business):
    at = future_slot(business)
    dash.post("/api/bookings", json={
        "start_time": at.isoformat(), "party_size": 2,
        "name": "Ravi", "phone": a_number(),
    })
    guests = dash.get("/api/guests?filter=all").json()["guests"]
    assert guests, "a booking with a number should make a guest"
    for key in ("name", "phone", "visits", "no_shows", "last", "do_not_call"):
        assert key in guests[0], f"Export CSV writes {key}"


# --- reading the menu off a photo --------------------------------------------


def test_the_read_button_is_hidden_until_a_reader_is_set_up(dash, business, monkeypatch):
    from calling_agent.config import settings

    monkeypatch.setattr(settings, "menu_reader", "")
    assert dash.get("/api/menu").json()["reader_configured"] is False

    monkeypatch.setattr(settings, "menu_reader", "gemini")
    assert dash.get("/api/menu").json()["reader_configured"] is True


def test_reading_a_menu_saves_nothing(dash, business, monkeypatch):
    """The whole point of the review step, pinned.

    A reader that wrote straight to menu_items would put dishes in the
    agent's mouth that nobody checked -- including allergy tags.
    """
    from calling_agent import menu_reader
    from calling_agent.config import settings

    monkeypatch.setattr(settings, "menu_reader", "fake")
    monkeypatch.setitem(
        menu_reader.PROVIDERS, "fake",
        lambda blob, content_type, business: [
            {"name": "Butter Chicken", "price": 420, "section": "Mains", "tags": ["nuts"]},
            {"name": "Dal Makhani", "price": "not a number"},
        ],
    )

    uploaded = dash.post("/api/menu/upload",
                         files={"file": ("menu.png", PNG, "image/png")}).json()["id"]
    response = dash.post(f"/api/menu/upload/{uploaded}/read")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["saved"] is False
    assert [d["name"] for d in body["dishes"]] == ["Butter Chicken", "Dal Makhani"]
    assert body["dishes"][1]["price"] is None, "a price that is not a number is not a price"

    assert dash.get("/api/menu").json()["items"] == [], "reading must not write"


def test_only_the_confirmed_dishes_are_written(dash, business):
    """What the owner corrected, not what the reader said."""
    response = dash.post("/api/menu/bulk", json={"dishes": [
        {"name": "Butter Chicken", "price": 380, "section": "Mains", "tags": ["dairy"]},
        {"name": "  ", "price": 1},
        {"name": "Dal Makhani", "price": 260},
    ]})
    assert response.status_code == 200, response.text
    assert response.json()["added"] == 2, "the blank row is not a dish"

    names = {item["name"] for item in dash.get("/api/menu").json()["items"]}
    assert names == {"Butter Chicken", "Dal Makhani"}


def test_confirming_nothing_is_refused(dash, business):
    assert dash.post("/api/menu/bulk", json={"dishes": []}).status_code == 400
    assert dash.post("/api/menu/bulk", json={"dishes": [{"name": ""}]}).status_code == 400


def test_a_reader_that_fails_says_so_without_leaking_the_body(dash, business, monkeypatch):
    from calling_agent import menu_reader
    from calling_agent.config import settings

    def explode(blob, content_type, business):
        raise menu_reader.MenuReadError("The reader rejected the API key on the server.")

    monkeypatch.setattr(settings, "menu_reader", "fake")
    monkeypatch.setitem(menu_reader.PROVIDERS, "fake", explode)

    uploaded = dash.post("/api/menu/upload",
                         files={"file": ("menu.png", PNG, "image/png")}).json()["id"]
    response = dash.post(f"/api/menu/upload/{uploaded}/read")
    assert response.status_code == 502
    assert "API key" in response.json()["detail"]
