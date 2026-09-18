"""Reservation logic, through the agent's own tools.

The agent is only as good as what these return: it reads their output aloud
and treats it as fact, so a wrong "yes" here becomes a table that does not
exist.

This file used to test an in-memory `BookingStore`. PRD §17 replaced that with
Postgres, so the same behaviours are checked against the real engine -- same
questions, real answers. What is asserted is what a caller HEARS, because that
is the product: the structured payload is checked where a screen depends on it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from calling_agent import agent_tools
from tests.conftest import committed, future_slot, make_business


def _args(when: datetime, business, **extra):
    local = when.astimezone(business.tz)
    return {
        "date": local.date().isoformat(),
        "time": local.strftime("%H:%M"),
        **extra,
    }


# --- availability ------------------------------------------------------------


def test_free_slot_is_offered(business):
    at = future_slot(business)
    out = agent_tools.check_availability(business, _args(at, business, units=2))
    assert out.data["ok"]
    assert "works for" in out


def test_a_closed_day_is_refused_with_the_reason():
    business = make_business(weekdays=[0, 1, 2, 3, 4])      # weekdays only
    at = future_slot(business)
    while at.astimezone(business.tz).weekday() != 5:        # find a Saturday
        at += timedelta(days=1)

    out = agent_tools.check_availability(business, _args(at, business, units=2))

    assert not out.data["ok"]
    assert out.data["reason"] == "closed"
    assert "closed on Saturday" in out


def test_a_time_outside_opening_hours_is_refused(business):
    at = future_slot(business, hour=9)                      # opens at noon
    out = agent_tools.check_availability(business, _args(at, business, units=2))
    assert not out.data["ok"]
    assert out.data["reason"] in ("closed", "full")


def test_past_dates_are_refused(business):
    yesterday = datetime.now(UTC) - timedelta(days=1)
    out = agent_tools.check_availability(business, _args(yesterday, business, units=2))
    assert out.data["reason"] == "past"
    assert "in the past" in out


def test_far_future_is_refused(business):
    far = future_slot(business, days_ahead=400)
    out = agent_tools.check_availability(business, _args(far, business, units=2))
    assert out.data["reason"] == "outside_window"
    assert "that far ahead" in out


def test_off_grid_times_are_refused(business):
    at = future_slot(business, hour=19, minute=17)
    out = agent_tools.check_availability(business, _args(at, business, units=2))
    assert out.data["reason"] == "off_grid"
    assert "every 30 minutes" in out


def test_an_oversized_party_is_refused(business):
    at = future_slot(business)
    out = agent_tools.check_availability(business, _args(at, business, units=500))
    assert out.data["reason"] == "party_too_large"
    assert "largest we can take" in out


def test_unparseable_input_says_so_rather_than_crashing(business):
    out = agent_tools.check_availability(business, {"date": "soonish", "time": "??", "units": 2})
    assert out.data["error"]
    assert out.data["reason"] in ("date_unclear", "time_unclear")
    assert "did not catch" in out


def test_a_full_slot_offers_alternatives_instead_of_a_flat_no():
    """A caller told only "no" phones the next restaurant."""
    business = make_business(total_units=4)
    at = future_slot(business)

    held = agent_tools.hold(business, _args(at, business, units=4))
    assert held.data["ok"]
    agent_tools.confirm(business, {"hold_id": held.data["hold_id"], "name": "First"})

    out = agent_tools.check_availability(business, _args(at, business, units=4))

    assert not out.data["ok"]
    assert out.data["alternatives"], "a full slot offered nothing else"
    assert "I could do" in out or "The next I have" in out


# --- booking -----------------------------------------------------------------


def test_a_booking_reads_its_reference_back_one_character_at_a_time(business):
    """It is read down a phone line, so it is spelled, not spoken as a word."""
    at = future_slot(business)
    held = agent_tools.hold(business, _args(at, business, units=4))
    out = agent_tools.confirm(
        business, {"hold_id": held.data["hold_id"], "name": "Ravi", "phone": "+919876543210"}
    )

    reference = out.data["reference"]
    assert len(reference) == 6
    assert " ".join(reference) in out


def test_a_booking_is_refused_once_capacity_is_gone():
    business = make_business(total_units=4)
    at = future_slot(business)

    first = agent_tools.hold(business, _args(at, business, units=4))
    agent_tools.confirm(business, {"hold_id": first.data["hold_id"], "name": "First"})

    second = agent_tools.hold(business, _args(at, business, units=2))

    assert not second.data["ok"]
    assert committed(business.id, at) == 4


def test_a_booking_without_a_name_is_refused(business):
    at = future_slot(business)
    held = agent_tools.hold(business, _args(at, business, units=2))
    out = agent_tools.confirm(business, {"hold_id": held.data["hold_id"], "name": "  "})
    assert out.data.get("error")
    assert "need a name" in out


def test_notes_are_kept_and_read_back(business):
    at = future_slot(business)
    held = agent_tools.hold(business, _args(at, business, units=2))
    booked = agent_tools.confirm(
        business,
        {"hold_id": held.data["hold_id"], "name": "Ravi", "notes": "Shellfish allergy"},
    )

    out = agent_tools.lookup_booking(business, {"reference": booked.data["reference"]})

    assert "Shellfish allergy" in out


def test_capacity_counts_only_live_bookings():
    business = make_business(total_units=4)
    at = future_slot(business)

    held = agent_tools.hold(business, _args(at, business, units=4))
    booked = agent_tools.confirm(
        business, {"hold_id": held.data["hold_id"], "name": "Ravi"}
    )
    assert committed(business.id, at) == 4

    agent_tools.cancel_booking(
        business, {"reference": booked.data["reference"], "name": "Ravi"}
    )

    assert committed(business.id, at) == 0
    assert agent_tools.check_availability(business, _args(at, business, units=4)).data["ok"]


# --- lookup and cancellation -------------------------------------------------


def test_lookup_is_case_insensitive_and_tolerates_spacing(business):
    """References arrive as the caller read them out."""
    at = future_slot(business)
    held = agent_tools.hold(business, _args(at, business, units=2))
    booked = agent_tools.confirm(business, {"hold_id": held.data["hold_id"], "name": "Ravi"})
    reference = booked.data["reference"]

    spoken = " ".join(reference).lower()
    out = agent_tools.lookup_booking(business, {"reference": spoken})

    assert out.data["found"]
    assert out.data["reference"] == reference


def test_an_unknown_reference_is_reported_not_raised(business):
    out = agent_tools.lookup_booking(business, {"reference": "ZZZZZZ"})
    assert out.data["found"] is False
    assert "cannot find" in out


def test_cancelling_needs_the_name_on_the_booking(business):
    """A destructive action needs a detail only the booker would know (PRD §8).

    The reference is not one: it is read aloud on the phone and printed in a
    text, so anyone in earshot has it.
    """
    at = future_slot(business)
    held = agent_tools.hold(business, _args(at, business, units=2))
    booked = agent_tools.confirm(business, {"hold_id": held.data["hold_id"], "name": "Ravi Sharma"})
    reference = booked.data["reference"]

    asked = agent_tools.cancel_booking(business, {"reference": reference})
    assert asked.data.get("needs_confirmation")

    refused = agent_tools.cancel_booking(business, {"reference": reference, "name": "Someone Else"})
    assert refused.data.get("mismatch")
    assert committed(business.id, at) == 2, "a refused cancellation freed the table"

    done = agent_tools.cancel_booking(business, {"reference": reference, "name": "Sharma"})
    assert done.data["ok"]


def test_cancelling_twice_is_harmless(business):
    at = future_slot(business)
    held = agent_tools.hold(business, _args(at, business, units=2))
    booked = agent_tools.confirm(business, {"hold_id": held.data["hold_id"], "name": "Ravi"})
    reference = booked.data["reference"]

    agent_tools.cancel_booking(business, {"reference": reference, "name": "Ravi"})
    again = agent_tools.cancel_booking(business, {"reference": reference, "name": "Ravi"})

    assert "already cancelled" in again
    assert committed(business.id, at) == 0


# --- the venue ---------------------------------------------------------------


def test_info_covers_the_week_and_what_the_place_is(business):
    out = agent_tools.business_info(business, {})
    assert "Test Venue" in out or "The Copper Kettle" in out
    assert len(out.data["hours"]) == 7


def test_now_reports_the_business_clock_not_the_server(business):
    """A venue in Mumbai and a server in Iowa disagree for six hours a day."""
    out = agent_tools.now(business, {})
    assert out.data["timezone"] == business.timezone
    assert out.data["date"] == datetime.now(business.tz).date().isoformat()


# --- declarations ------------------------------------------------------------


def test_every_declared_tool_has_an_implementation(business):
    declared = {tool["name"] for tool in agent_tools.tool_declarations(business)}
    available = set(agent_tools.IMPLEMENTATIONS) | set(
        __import__("calling_agent.menu", fromlist=["IMPLEMENTATIONS"]).IMPLEMENTATIONS
    )
    assert declared <= available, declared - available


def test_tool_schemas_use_the_shape_the_api_requires(business):
    for tool in agent_tools.tool_declarations(business):
        assert tool["type"] == "function"
        assert tool["name"] and tool["description"]
        parameters = tool["parameters"]
        assert parameters["type"] == "object"
        assert isinstance(parameters["properties"], dict)
        assert set(parameters["required"]) <= set(parameters["properties"])


def test_the_tools_are_worded_for_the_vertical():
    """"how many covers" and "how many patients" are different questions."""
    restaurant = make_business(vertical="restaurant")
    clinic = make_business(vertical="clinic")

    def units_description(b):
        tool = next(t for t in agent_tools.tool_declarations(b) if t["name"] == "hold")
        return tool["parameters"]["properties"]["units"]["description"]

    assert "covers" in units_description(restaurant)
    assert "appointments" in units_description(clinic)


def test_no_business_id_is_ever_special_cased():
    """PRD §15: the property that makes onboarding a ten-minute form."""
    import pathlib
    import re

    source = pathlib.Path(__file__).resolve().parents[1] / "src" / "calling_agent"
    offenders = [
        path.name
        for path in source.rglob("*.py")
        if re.search(r"business(_id)?\s*==\s*['\"0-9]", path.read_text())
    ]
    assert not offenders, f"a business is special-cased in {offenders}"


# --- bugs found on the first real conversation -------------------------------
#
# All three came out of one live call. None was reachable from a test that fed
# the tools well-formed arguments, because the problem in every case was what a
# language model sends when it is improvising.


def test_a_bare_hour_is_read_as_the_time_the_venue_is_open(business):
    """A caller saying "seven" means seven in the evening at a restaurant.

    dateparser reads "7" as the SEVENTH OF THE MONTH and returns midnight, so
    a booking for tonight was refused with "that is in the past" -- a true
    sentence about a time nobody asked for.
    """
    at = future_slot(business, hour=19)
    local_day = at.astimezone(business.tz).date().isoformat()

    out = agent_tools.check_availability(business, {"date": local_day, "time": "7", "units": 2})

    assert out.data["ok"], f"a bare hour was not understood: {out}"
    assert "7:00 PM" in out


def test_a_two_digit_bare_hour_does_not_slip_through_as_iso(business):
    """Python 3.11 made `time.fromisoformat("11")` valid, which looks decisive.

    It is not: "eleven" is two different times, and taking the literal reading
    books the morning for somebody who wanted the evening.
    """
    at = future_slot(business)
    local_day = at.astimezone(business.tz).date().isoformat()

    # The fixture venue opens 12:00-22:00, so neither 11:00 nor 23:00 works
    # and the only honest answer is to ask.
    out = agent_tools.check_availability(business, {"date": local_day, "time": "11", "units": 2})

    assert out.data["reason"] == "time_unclear"
    assert "what time" in str(out).lower()


def test_an_unambiguous_time_is_never_second_guessed(business):
    at = future_slot(business)
    local_day = at.astimezone(business.tz).date().isoformat()

    for spoken in ("19:30", "7:30 PM"):
        out = agent_tools.check_availability(
            business, {"date": local_day, "time": spoken, "units": 2}
        )
        assert out.data["ok"], f"{spoken!r} was not understood: {out}"


def test_a_fabricated_hold_id_is_refused_without_leaking_the_database(business):
    """A model that never called hold() invents one that looks like an id.

    Passing "hold_12345" to Postgres raises InvalidTextRepresentation, and the
    message names the table, the SQL and the tenant's own uuid. Whatever a tool
    returns goes in front of the model, which paraphrases it to the caller --
    so one bad id put a stack trace into a live conversation.
    """
    out = agent_tools.confirm(business, {"hold_id": "hold_12345", "name": "Ravi"})

    assert out.data["error"]
    assert out.data["reason"] == "hold_not_found"
    for leak in ("psycopg", "SELECT", "uuid", str(business.id), "holds"):
        assert leak not in str(out), f"the answer leaked {leak!r}"
    # And it must tell the agent what to do instead of just what went wrong.
    assert "take the time again" in str(out).lower()


def test_a_crashed_tool_never_reports_the_exception_text(business, monkeypatch):
    """The log is where a stack trace is useful. A phone call is not."""
    def explode(*_args, **_kwargs):
        raise RuntimeError("connection to server at 10.1.2.3 failed: password authentication")

    monkeypatch.setitem(agent_tools.IMPLEMENTATIONS, "check_availability", explode)
    spoken, is_error = agent_tools.run_tool_for(business, "check_availability", {})

    assert is_error
    assert "10.1.2.3" not in spoken and "password" not in spoken
    assert "do not invent" in spoken.lower()
