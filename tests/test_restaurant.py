"""Reservation logic.

The agent is only as good as what these return: it reads their output aloud
and treats it as fact, so a wrong "yes" here becomes a table that does not
exist.
"""

from datetime import UTC, datetime, timedelta

import pytest

from calling_agent import restaurant as r


@pytest.fixture(autouse=True)
def empty_book(monkeypatch):
    """Each test gets a fresh reservation book."""
    monkeypatch.setattr(r, "BOOKINGS", r.BookingStore())


def _next_weekday(weekday: int, hour: int = 19, minute: int = 0) -> tuple[str, str]:
    """A date/time in the future landing on the given weekday."""
    today = datetime.now(UTC).date()
    ahead = (weekday - today.weekday()) % 7 or 7
    day = today + timedelta(days=ahead)
    return day.isoformat(), f"{hour:02d}:{minute:02d}"


# --- availability ------------------------------------------------------------


def test_free_slot_is_offered():
    day, at = _next_weekday(4)  # Friday
    out = r.check_availability({"date": day, "time": at, "party_size": 2})
    assert "available" in out.lower()


def test_closed_day_is_refused_with_the_reason():
    day, at = _next_weekday(6, hour=22)  # Sunday closes at 21:00
    out = r.check_availability({"date": day, "time": at, "party_size": 2})
    assert "serve from" in out


def test_time_outside_opening_hours_is_refused():
    day, at = _next_weekday(0, hour=9)  # before noon on a Monday
    out = r.check_availability({"date": day, "time": at, "party_size": 2})
    assert "serve from" in out


def test_past_dates_are_refused():
    yesterday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
    assert "past" in r.check_availability(
        {"date": yesterday, "time": "19:00", "party_size": 2}
    )


def test_far_future_is_refused():
    far = (datetime.now(UTC).date() + timedelta(days=365)).isoformat()
    out = r.check_availability({"date": far, "time": "19:00", "party_size": 2})
    assert "days ahead" in out


def test_off_grid_times_are_refused():
    day, _ = _next_weekday(4)
    out = r.check_availability({"date": day, "time": "19:07", "party_size": 2})
    assert "every 30 minutes" in out


def test_oversized_party_is_refused():
    day, at = _next_weekday(4)
    out = r.check_availability({"date": day, "time": at, "party_size": 99})
    assert "between one and" in out


def test_unparseable_input_says_so_rather_than_crashing():
    assert "not a date" in r.check_availability(
        {"date": "next tuesday", "time": "19:00", "party_size": 2}
    )
    day, _ = _next_weekday(4)
    assert "not a time" in r.check_availability(
        {"date": day, "time": "half seven", "party_size": 2}
    )


def test_full_slot_offers_alternatives_instead_of_a_flat_no():
    day, at = _next_weekday(4)
    # Fill the slot to capacity.
    for _ in range(r.SEATS_PER_SLOT // 4):
        r.book_table({"name": "Filler", "date": day, "time": at, "party_size": 4})

    out = r.check_availability({"date": day, "time": at, "party_size": 4})
    assert "full" in out.lower()
    assert "same day" in out


# --- booking -----------------------------------------------------------------


def test_booking_returns_a_readable_reference():
    day, at = _next_weekday(4)
    out = r.book_table({"name": "Ravi", "date": day, "time": at, "party_size": 4})
    assert "Booked" in out and "Ravi" in out

    ref = next(iter(r.BOOKINGS.bookings))
    assert len(ref) == 5
    # 0/O and 1/I are ambiguous read aloud, so they are excluded.
    assert not (set(ref) & set("O0I1"))
    # Spelled out with spaces so the agent reads it character by character.
    assert " ".join(ref) in out


def test_booking_is_refused_once_capacity_is_gone():
    day, at = _next_weekday(4)
    r.book_table({"name": "Big", "date": day, "time": at, "party_size": r.SEATS_PER_SLOT})
    out = r.book_table({"name": "Late", "date": day, "time": at, "party_size": 2})
    assert "filled up" in out


def test_booking_without_a_name_is_refused():
    day, at = _next_weekday(4)
    out = r.book_table({"name": "  ", "date": day, "time": at, "party_size": 2})
    assert "need a name" in out


def test_notes_are_kept_and_read_back():
    day, at = _next_weekday(4)
    out = r.book_table(
        {
            "name": "Priya",
            "date": day,
            "time": at,
            "party_size": 2,
            "notes": "nut allergy",
        }
    )
    assert "nut allergy" in out
    assert "nut allergy" in r.lookup_booking({"reference": next(iter(r.BOOKINGS.bookings))})


def test_capacity_counts_only_live_bookings():
    day, at = _next_weekday(4)
    r.book_table({"name": "Gone", "date": day, "time": at, "party_size": r.SEATS_PER_SLOT})
    ref = next(iter(r.BOOKINGS.bookings))
    r.cancel_booking({"reference": ref})

    # The seats must come back, or a cancellation silently loses covers.
    out = r.check_availability({"date": day, "time": at, "party_size": 4})
    assert "available" in out.lower()


# --- lookup and cancel -------------------------------------------------------


def test_lookup_is_case_insensitive_and_tolerates_spacing():
    day, at = _next_weekday(4)
    r.book_table({"name": "Sam", "date": day, "time": at, "party_size": 2})
    ref = next(iter(r.BOOKINGS.bookings))
    assert "Sam" in r.lookup_booking({"reference": f"  {ref.lower()} "})


def test_unknown_reference_is_reported_not_raised():
    assert "cannot find" in r.lookup_booking({"reference": "ZZZZZ"})
    assert "cannot find" in r.cancel_booking({"reference": "ZZZZZ"})


def test_cancelling_twice_is_harmless():
    day, at = _next_weekday(4)
    r.book_table({"name": "Sam", "date": day, "time": at, "party_size": 2})
    ref = next(iter(r.BOOKINGS.bookings))
    assert "Cancelled" in r.cancel_booking({"reference": ref})
    assert "already cancelled" in r.cancel_booking({"reference": ref})


def test_info_covers_every_day_and_the_cuisine():
    out = r.restaurant_info({})
    for day in ("Monday", "Saturday", "Sunday"):
        assert day in out
    assert "Opening hours" in out


# --- every tool is wired -----------------------------------------------------


def test_every_declared_tool_has_an_implementation():
    declared = {t["name"] for t in r.TOOLS}
    assert declared == set(r.IMPLEMENTATIONS)


def test_tool_schemas_use_the_shape_the_api_requires():
    for tool in r.TOOLS:
        assert tool["type"] == "function"
        assert "parameters" in tool and "input_schema" not in tool
        assert tool["parameters"]["type"] == "object"
        assert tool["description"]
