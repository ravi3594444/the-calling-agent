"""Signing in.

An account is a way to OBTAIN a dashboard link, not a second kind of
credential: login mints an ordinary token and sets the cookie that
`current_business` already reads. So the tests that matter are that the thing
it mints really works, that it cannot be obtained without the password, and
that signing out kills the token rather than just the cookie.
"""

from __future__ import annotations

import itertools
import secrets

import pytest
from fastapi.testclient import TestClient

from calling_agent import owners
from calling_agent.api import auth
from calling_agent.config import settings
from calling_agent.main import app
from tests.conftest import make_business

_run = secrets.token_hex(3)
_seq = itertools.count(1)

PASSWORD = "a-good-long-password"


def an_email() -> str:
    """Unique per run: emails are unique across the product and the test
    database is shared and never truncated."""
    return f"signin{_run}{next(_seq)}@example.com"


@pytest.fixture
def account():
    """A venue with an owner who can sign in."""
    business = make_business()
    email = an_email()
    owners.create(business.id, email=email, password=PASSWORD)
    return business, email


def sign_in(client, email, password):
    return client.post("/login", data={"email": email, "password": password})


# --- passwords ----------------------------------------------------------------


def test_a_password_is_slow_and_salted_rather_than_hashed_like_a_token():
    """tokens.hash_secret is one SHA-256, which is right for 32 random bytes
    and wrong for a word somebody chose. Two hashes of the same password must
    differ, or a dump tells you which owners share one."""
    first = owners.hash_password(PASSWORD)
    assert first.startswith("scrypt$")
    assert first != owners.hash_password(PASSWORD), "unsalted"
    assert owners.password_matches(PASSWORD, first)
    assert not owners.password_matches(PASSWORD + "x", first)


@pytest.mark.parametrize("stored", ["", "not-a-hash", "scrypt$bad", "sha256$1$2$3$aa$bb"])
def test_a_hash_nobody_can_parse_fails_the_login_rather_than_the_page(stored):
    """A row somebody edited by hand should refuse the password, not 500."""
    assert owners.password_matches(PASSWORD, stored) is False


def test_a_short_password_is_refused_before_an_account_exists():
    business = make_business()
    with pytest.raises(owners.OwnerError):
        owners.create(business.id, email=an_email(), password="short")


def test_one_email_cannot_hold_two_accounts_whatever_case_it_is_typed_in():
    """The unique index is on lower(email), so login has exactly one answer to
    "which venue is this"."""
    email = an_email()
    owners.create(make_business().id, email=email, password=PASSWORD)
    with pytest.raises(owners.OwnerError):
        owners.create(make_business().id, email=email.upper(), password=PASSWORD)


def test_an_address_is_matched_however_it_was_typed(account):
    _, email = account
    assert owners.authenticate(f"  {email.upper()}  ", PASSWORD) is not None


# --- signing in ----------------------------------------------------------------


def test_signing_in_hands_back_a_working_dashboard(account):
    """The point of the whole feature: a browser that has never seen this venue
    and holds no link ends up looking at its dashboard."""
    business, email = account
    client = TestClient(app, follow_redirects=False)

    assert client.get("/api/bootstrap").status_code == 401, "nothing yet"

    signed_in = sign_in(client, email, PASSWORD)
    assert signed_in.status_code == 303
    assert signed_in.headers["location"] == "/dashboard"
    assert auth.COOKIE in signed_in.cookies or auth.COOKIE in client.cookies

    bootstrap = client.get("/api/bootstrap")
    assert bootstrap.status_code == 200
    assert bootstrap.json()["business"]["id"] == str(business.id)


def test_the_wrong_password_says_nothing_about_whether_the_account_exists(account):
    """One message for both, or the form becomes a way to ask which venues are
    registered here."""
    _, email = account
    client = TestClient(app, follow_redirects=False)

    wrong = sign_in(client, email, "not-the-password")
    missing = sign_in(client, "nobody" + email, PASSWORD)

    for refused in (wrong, missing):
        assert refused.status_code == 200, "the page again, for a person"
        assert "That email or password is wrong." in refused.text
    assert client.get("/api/bootstrap").status_code == 401, "no cookie was set"


def test_a_submission_missing_a_field_is_the_form_again_not_a_422(account):
    client = TestClient(app, follow_redirects=False)
    for data in ({}, {"email": "someone@example.com"}, {"password": PASSWORD}):
        refused = client.post("/login", data=data)
        assert refused.status_code == 200
        assert "Sign in" in refused.text


def test_the_session_expires_even_though_a_saved_link_does_not(account, monkeypatch):
    """A link is something they chose to keep; a session is a browser they
    happened to use. The token minted here carries an expiry, which the one
    handed out at signup deliberately does not."""
    from calling_agent.db import fetch_one, transaction

    business, email = account
    monkeypatch.setattr(settings, "session_days", 30)
    client = TestClient(app, follow_redirects=False)
    sign_in(client, email, PASSWORD)

    with transaction() as conn:
        row = fetch_one(
            conn,
            "SELECT expires_at, label FROM dashboard_tokens WHERE business_id = :b"
            " ORDER BY created_at DESC LIMIT 1",
            b=str(business.id),
        )
    assert row.label == "signed in"
    assert row.expires_at is not None


def test_signing_out_kills_the_token_not_just_the_cookie(account):
    """Clearing the cookie alone leaves a live credential in whatever copied
    it, which is the opposite of what pressing sign out means."""
    _, email = account
    client = TestClient(app, follow_redirects=False)
    sign_in(client, email, PASSWORD)
    stolen = client.cookies[auth.COOKIE]
    assert client.get("/api/bootstrap").status_code == 200

    assert client.post("/logout").status_code == 303
    assert client.get("/api/bootstrap").status_code == 401

    # The copy somebody else took is dead too.
    other = TestClient(app)
    other.headers.update({"X-Tableline-Token": stolen})
    assert other.get("/api/bootstrap").status_code == 401


def test_the_login_page_waves_through_somebody_already_signed_in(account):
    _, email = account
    client = TestClient(app, follow_redirects=False)
    assert client.get("/login").status_code == 200

    sign_in(client, email, PASSWORD)
    landed = client.get("/login")
    assert landed.status_code == 303
    assert landed.headers["location"] == "/dashboard"


def test_the_login_page_only_offers_signup_when_signup_is_on(monkeypatch):
    monkeypatch.setattr(settings, "signup_enabled", False)
    assert 'href="/start"' not in TestClient(app).get("/login").text
    monkeypatch.setattr(settings, "signup_enabled", True)
    assert 'href="/start"' in TestClient(app).get("/login").text


# --- recovery -------------------------------------------------------------------


def test_a_reset_lets_the_new_password_in_and_keeps_the_old_one_out(account):
    """The recovery path, until something can send mail."""
    _, email = account
    owners.set_password(email, "a-different-long-password")

    client = TestClient(app, follow_redirects=False)
    assert sign_in(client, email, PASSWORD).status_code == 200, "the old one is dead"
    assert sign_in(client, email, "a-different-long-password").status_code == 303


def test_resetting_an_address_nobody_has_says_so(account):
    with pytest.raises(owners.OwnerError):
        owners.set_password("nobody-here@example.com", "a-good-long-password")
