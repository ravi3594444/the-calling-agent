"""The ten-minute form (PRD §15).

This route is the one place in the product that creates a tenant with nothing
authenticated in front of it, so the tests that matter most are the ones about
when it refuses to exist at all.
"""

from __future__ import annotations

import itertools
import secrets

import pytest
from fastapi.testclient import TestClient

from calling_agent import businesses
from calling_agent.api import signup
from calling_agent.config import settings
from calling_agent.main import app
from tests.conftest import test_phone_number as a_number

#: A complete, valid submission. Tests vary one field at a time from this.
GOOD = {
    "description": "small plates and natural wine",
    "agent_name": "Inês",
    "vertical": "restaurant",
    "country": "PT",
    "opens": "18:00",
    "closes": "23:30",
    "seats": "34",
    "days": ["2", "3", "4", "5", "6"],
}


@pytest.fixture
def open_signup(monkeypatch):
    """Signup on, no invite code, and a rate limiter that starts empty."""
    monkeypatch.setattr(settings, "signup_enabled", True)
    monkeypatch.setattr(settings, "signup_code", "")
    monkeypatch.setattr(settings, "signup_max_per_hour", 50)
    monkeypatch.setattr(signup, "_recent", {})
    return TestClient(app, follow_redirects=False)


#: The test database is shared by the whole session and never truncated, so a
#: fixed venue name means run two asserts against run one's row -- and keeps
#: passing after the handler breaks. Every name that reaches the database here
#: is unique to this run.
_run = secrets.token_hex(3)
_names = itertools.count(1)


def unique_name(label: str = "Venue") -> str:
    return f"{label} {_run}{next(_names)}"


def submit(client, **changes):
    form = dict(GOOD) | changes
    form.setdefault("name", unique_name())
    return client.post("/start", data=form)


def venue_from(response):
    """The business that submission actually created.

    Resolved through the link it handed back rather than a guessed slug, so
    the test can only ever be looking at the row it just made.
    """
    token = response.headers["location"].split("token=")[1]
    client = TestClient(app)
    client.headers.update({"X-Tableline-Token": token})
    return businesses.by_id(client.get("/api/bootstrap").json()["business"]["id"])


# --- when it must not exist ---------------------------------------------------


def test_signup_is_off_unless_it_was_turned_on(monkeypatch):
    """The default. A deployment that never asked for a public signup page
    must not discover it has one."""
    monkeypatch.setattr(settings, "signup_enabled", False)
    client = TestClient(app)
    assert client.get("/start").status_code == 404
    assert client.post("/start", data=GOOD).status_code == 404


def test_a_disabled_signup_looks_absent_even_to_a_malformed_post(monkeypatch):
    """404, not 422. Declared Form(...) parameters are validated BEFORE the
    handler body, so an empty POST once answered 422 while GET answered 404 --
    which told anyone probing that the route was there and merely switched off.
    The form is read by hand precisely so the guard runs first."""
    monkeypatch.setattr(settings, "signup_enabled", False)
    client = TestClient(app)
    assert client.post("/start", data={}).status_code == 404
    assert client.post("/start", data={"name": "x"}).status_code == 404


def test_an_invite_code_is_required_when_one_is_set(monkeypatch, open_signup):
    monkeypatch.setattr(settings, "signup_code", "letmein")
    refused = open_signup.post("/start", data=dict(GOOD) | {"name": "Café Ramona", "code": "wrong"})
    assert refused.status_code == 200, "a wrong code is the form again, not an API error"
    assert "That invite code is not right." in refused.text
    assert "Café Ramona" in refused.text, "what they typed must survive the refusal"

    assert submit(open_signup, code="letmein").status_code == 303


def test_enough_signups_from_one_address_are_refused(monkeypatch, open_signup):
    monkeypatch.setattr(settings, "signup_max_per_hour", 2)
    assert submit(open_signup).status_code == 303
    assert submit(open_signup).status_code == 303
    assert submit(open_signup).status_code == 429


def test_a_refused_attempt_does_not_count_against_the_limit(monkeypatch, open_signup):
    """Only completed signups count. A typo in the invite code must not lock
    somebody out of their own second attempt."""
    monkeypatch.setattr(settings, "signup_code", "letmein")
    monkeypatch.setattr(settings, "signup_max_per_hour", 1)
    for _ in range(3):
        assert submit(open_signup, code="nope").status_code == 200
    assert submit(open_signup, code="letmein").status_code == 303


# --- what it produces ---------------------------------------------------------


def test_the_form_lists_the_countries_and_verticals_the_server_knows(open_signup):
    page = open_signup.get("/start")
    assert page.status_code == 200
    assert "Portugal" in page.text and 'value="PT"' in page.text
    assert 'value="restaurant"' in page.text and 'value="dental"' in page.text


def test_the_kind_of_place_starts_on_restaurant(open_signup):
    """known_verticals() is sorted, so the untouched select showed "Clinic".
    A form whose first answer is already wrong is one people correct by not
    finishing it."""
    assert 'value="restaurant" selected' in open_signup.get("/start").text

    # And a redraw keeps what they actually picked, rather than resetting it.
    refused = submit(open_signup, vertical="dental", days=[])
    assert 'value="dental" selected' in refused.text


def test_a_submission_creates_a_venue_that_can_answer_the_phone(open_signup):
    """The whole point: no terminal. What comes out has to be a tenant the
    agent can take a call for, not just a row."""
    from calling_agent.agent_config import build_prompt_for, greeting_for

    name = unique_name("Café Ramona")
    created = open_signup.post("/start", data=dict(GOOD) | {"name": name, "phone": a_number()})
    assert created.status_code == 303

    business = venue_from(created)
    assert business.name == name
    assert business.slug.startswith("cafe-ramona"), "the accent folds out of the slug"
    assert business.timezone == "Europe/Lisbon", "the country decides the timezone"
    assert business.config["locale"]["currency"] == "EUR"
    assert business.config["locale"]["clock"] == "24"
    assert business.config["capacity"]["enabled"] is True

    week = {rule.weekday for rule in business.rules}
    assert week == {2, 3, 4, 5, 6}, "only the days they ticked"
    assert business.rules[0].total_units == 34

    assert "Inês" in greeting_for(business)
    assert "natural wine" in build_prompt_for(business)


def test_the_link_it_hands_back_opens_that_venue_and_nothing_else(open_signup):
    """A signup that ends anywhere but a working dashboard has not finished."""
    other = businesses.by_slug("default-test-venue")
    name = unique_name("Token Test")
    created = submit(open_signup, name=name, phone=a_number())
    token = created.headers["location"].split("token=")[1]

    client = TestClient(app)
    client.headers.update({"X-Tableline-Token": token})
    bootstrap = client.get("/api/bootstrap")
    assert bootstrap.status_code == 200
    assert bootstrap.json()["business"]["name"] == name
    assert bootstrap.json()["business"]["id"] != str(other.id)

    assert TestClient(app).get("/api/bootstrap?token=not-a-real-token").status_code == 401


def test_leaving_capacity_blank_means_the_agent_takes_everything(open_signup):
    """PRD §15d. A venue that set no capacity gets an agent that takes every
    booking -- not one that blocks on a number nobody chose."""
    created = submit(open_signup, seats="")
    assert created.status_code == 303
    assert venue_from(created).config["capacity"]["enabled"] is False


def test_the_slug_comes_from_the_name_and_survives_a_clash(open_signup):
    assert signup.slugify("Café Ramona") == "cafe-ramona"
    assert signup.slugify("  The Grove!! ") == "the-grove"
    assert signup.slugify("茶") == "venue", "a name that folds away still needs a slug"

    name = unique_name("Twice Over")
    first = submit(open_signup, name=name)
    second = submit(open_signup, name=name)
    assert first.status_code == 303 and second.status_code == 303
    base = signup.slugify(name)
    assert venue_from(first).slug == base
    assert venue_from(second).slug == f"{base}-2", "the same name twice takes a number"


def test_a_number_that_already_answers_somewhere_else_is_named(open_signup):
    """The dialled number binds the tenant at the edge, so two venues sharing
    one is a call that cannot be routed. The unique index catches it; this
    turns it into a sentence."""
    shared = a_number()
    assert submit(open_signup, phone=shared).status_code == 303
    clash = submit(open_signup, phone=shared)
    assert clash.status_code == 200
    assert "already answers for another venue" in clash.text


# --- what it refuses ----------------------------------------------------------


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({"name": "   "}, "needs a name"),
        ({"days": []}, "at least one day"),
        ({"opens": "half six"}, "look like 18:00"),
        ({"opens": "22:00", "closes": "09:00"}, "after opening time"),
        ({"seats": "lots"}, "has to be a number"),
    ],
)
def test_a_bad_field_redraws_the_form_with_the_rest_still_in_it(open_signup, changes, expected):
    """200 and the form again, not a 4xx and an empty page. This is a person
    filling in a form, and one that empties itself is one they abandon."""
    refused = submit(open_signup, **changes)
    assert refused.status_code == 200
    assert expected in refused.text
    assert "small plates and natural wine" in refused.text, "the other answers survive"


def test_nothing_is_created_when_a_submission_is_refused(open_signup):
    name = unique_name("Ghost Venue")
    assert submit(open_signup, name=name, days=[]).status_code == 200
    with pytest.raises(businesses.UnknownBusiness):
        businesses.by_slug(signup.slugify(name))
