"""The booking lifecycle and the overflow queue (PRD §9, §11).

The counter is the assertion in most of these. A status change that does not
move capacity correctly is invisible in the UI and catastrophic in the room.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from calling_agent import availability, bookings, holds, notifications
from calling_agent.db import readonly
from tests.conftest import committed, future_slot, make_business
from tests.conftest import test_phone_number as a_number


def _confirmed(business, at, units=2, name="Ravi", phone="+919876543210"):
    return bookings.create_direct(
        business, start=at, units=units, name=name, phone=phone
    )


def _messages(business_id, kind=None):
    from sqlalchemy import text

    sql = "SELECT kind, body, to_address, channel, status FROM messages WHERE business_id = :b"
    if kind:
        sql += " AND kind = :k"
    with readonly() as conn:
        return conn.execute(
            text(sql), {"b": str(business_id), "k": kind} if kind else {"b": str(business_id)}
        ).fetchall()


# --- pending and the counter -------------------------------------------------


def test_a_pending_request_never_touches_the_counter():
    """PRD §9: only `confirmed` holds capacity."""
    business = make_business(total_units=4)
    at = future_slot(business)

    booking, deadline = bookings.request_overflow(
        business, start=at, units=4, name="Kulkarni", phone="+919930261147"
    )
    assert booking.status == "pending"
    assert committed(business.id, at) == 0, "a waitlist request took capacity"
    assert deadline is not None

    # And the room is still sellable to someone who can be confirmed now.
    assert availability.is_available(business, at, 4).ok


def test_accepting_a_pending_request_takes_the_capacity():
    business = make_business(total_units=4)
    at = future_slot(business)
    booking, _ = bookings.request_overflow(
        business, start=at, units=3, name="Kulkarni", phone="+919930261147"
    )

    accepted = bookings.accept(business, booking.id)

    assert accepted.status == "confirmed"
    assert committed(business.id, at) == 3
    assert accepted.reference == booking.reference, "accepting minted a new reference"


def test_an_owner_may_accept_past_the_walk_in_buffer():
    """PRD §11 step 4: accept may deliberately exceed sellable_pct."""
    business = make_business(total_units=10, config={"capacity": {"sellable_pct": 0.5}})
    at = future_slot(business)

    _confirmed(business, at, units=5)                      # fills the sellable share
    assert not availability.is_available(business, at, 1).ok

    waiting, _ = bookings.request_overflow(
        business, start=at, units=2, name="Late", phone="+910000000001"
    )
    accepted = bookings.accept(business, waiting.id)

    assert accepted.status == "confirmed"
    assert committed(business.id, at) == 7, "the owner's override did not take the seats"


def test_declining_offers_the_nearest_alternative():
    business = make_business(total_units=4)
    at = future_slot(business)
    waiting, _ = bookings.request_overflow(
        business, start=at, units=2, name="Kulkarni", phone="+919930261147"
    )

    declined = bookings.decline(business, waiting.id)

    assert declined.status == "declined"
    assert committed(business.id, at) == 0
    bodies = [m.body for m in _messages(business.id, "declined")]
    assert bodies and bodies[0], "nobody was told they had been declined"


# --- cancellation and no-shows ----------------------------------------------


def test_cancelling_puts_the_seats_back_on_sale():
    business = make_business(total_units=4)
    at = future_slot(business)
    booking = _confirmed(business, at, units=4)
    assert committed(business.id, at) == 4

    bookings.cancel(business, booking.id)

    assert committed(business.id, at) == 0
    assert availability.is_available(business, at, 4).ok


def test_a_no_show_frees_the_seats_and_is_remembered():
    business = make_business(total_units=4)
    at = future_slot(business)
    booking = _confirmed(business, at, units=2)

    bookings.mark_arrived(business, booking.id)
    after_arrival = bookings.by_id(business, booking.id)
    assert after_arrival.visits == 1
    assert committed(business.id, at) == 2, "arriving should not change capacity"

    bookings.mark_no_show(business, booking.id)
    marked = bookings.by_id(business, booking.id)

    assert marked.status == "no_show"
    assert marked.no_shows == 1
    assert marked.visits == 0, "the visit should have been taken back"
    assert committed(business.id, at) == 0, "a no-show's seats stayed sold"


def test_undo_leaves_no_trace_on_the_guest():
    """A mistap must not follow a guest around forever."""
    business = make_business(total_units=4)
    at = future_slot(business)
    booking = _confirmed(business, at, units=2)

    bookings.mark_no_show(business, booking.id)
    bookings.undo(business, booking.id)
    after = bookings.by_id(business, booking.id)

    assert after.status == "confirmed"
    assert after.no_shows == 0, "the undone no-show is still on the guest's record"
    assert committed(business.id, at) == 2, "undo did not re-take the seats"


def test_illegal_transitions_are_refused_by_name():
    business = make_business(total_units=4)
    at = future_slot(business)
    booking = _confirmed(business, at, units=2)
    bookings.cancel(business, booking.id)

    with pytest.raises(bookings.IllegalTransition):
        bookings.mark_arrived(business, booking.id)


def test_status_changes_are_idempotent():
    business = make_business(total_units=4)
    at = future_slot(business)
    booking = _confirmed(business, at, units=2)

    bookings.cancel(business, booking.id)
    again = bookings.cancel(business, booking.id)

    assert again.status == "cancelled"
    assert committed(business.id, at) == 0, "cancelling twice released the seats twice"


# --- the waitlist ------------------------------------------------------------


def test_a_cancellation_auto_accepts_the_oldest_fitting_request():
    """PRD §11: on any cancellation the oldest fitting request is auto-accepted."""
    business = make_business(total_units=4)
    at = future_slot(business)
    booked = _confirmed(business, at, units=4)

    first, _ = bookings.request_overflow(
        business, start=at, units=2, name="First", phone="+910000000011"
    )
    second, _ = bookings.request_overflow(
        business, start=at, units=2, name="Second", phone="+910000000022"
    )

    bookings.cancel(business, booked.id)

    assert bookings.by_id(business, first.id).status == "confirmed"
    assert bookings.by_id(business, second.id).status == "pending", "both were let in"
    assert committed(business.id, at) == 2


def test_a_slot_takes_only_so_many_waiting_requests():
    business = make_business(total_units=2, config={"policy": {"max_pending_per_slot": 2}})
    at = future_slot(business)
    _confirmed(business, at, units=2)

    bookings.request_overflow(business, start=at, units=2, name="A", phone="+910000000001")
    bookings.request_overflow(business, start=at, units=2, name="B", phone="+910000000002")
    with pytest.raises(bookings.BookingError):
        bookings.request_overflow(business, start=at, units=2, name="C", phone="+910000000003")


def test_an_unanswered_request_declines_itself_rather_than_going_quiet():
    """PRD §11 step 6: never leave the customer silent."""
    business = make_business(total_units=2)
    at = future_slot(business)
    _confirmed(business, at, units=2)
    waiting, _ = bookings.request_overflow(
        business, start=at, units=2, name="Kulkarni", phone="+919930261147"
    )

    expired = bookings.expire_overflow(business, waiting.id)

    assert expired is not None and expired.status == "declined"
    assert [m.body for m in _messages(business.id, "declined")], "the deadline passed in silence"


# --- moving ------------------------------------------------------------------


def test_moving_keeps_the_reference_and_the_capacity_follows():
    business = make_business(total_units=4)
    at = future_slot(business, hour=19)
    later = future_slot(business, hour=20)
    booking = _confirmed(business, at, units=2)

    moved = bookings.move(business, booking.id, later)

    assert moved.reference == booking.reference, "a reschedule minted a new reference"
    assert committed(business.id, at) == 0, "the old time kept the seats"
    assert committed(business.id, later) == 2


def test_a_move_with_no_room_leaves_the_booking_where_it_was():
    business = make_business(total_units=4)
    at = future_slot(business, hour=19)
    # Another day, so the two bookings' 90-minute turns cannot overlap and the
    # refusal is unambiguously "that time is full".
    full = future_slot(business, days_ahead=3, hour=19)
    booking = _confirmed(business, at, units=2)
    _confirmed(business, full, units=4, name="Other", phone="+910000000099")

    with pytest.raises(bookings.NoCapacity):
        bookings.move(business, booking.id, full)

    assert bookings.by_id(business, booking.id).start_time == at
    assert committed(business.id, at) == 2, "a failed move dropped the original seats"


# --- messages ----------------------------------------------------------------


def test_a_confirmed_booking_queues_a_text_and_a_reminder():
    business = make_business(total_units=4)
    at = future_slot(business, days_ahead=3)
    _confirmed(business, at, units=2)

    confirmations = _messages(business.id, "confirmed")
    assert confirmations, "no confirmation was queued"
    assert confirmations[0].status == "queued", "a message was sent from the request path"
    assert "Reference" in confirmations[0].body or confirmations[0].body

    from sqlalchemy import text

    with readonly() as conn:
        tasks = conn.execute(
            text("SELECT reason, status FROM outbound_tasks WHERE business_id = :b"),
            {"b": str(business.id)},
        ).fetchall()
    assert any(t.reason == "reminder" for t in tasks), "no day-before reminder was scheduled"


def test_cancelling_drops_the_reminder_that_is_no_longer_true():
    business = make_business(total_units=4)
    at = future_slot(business, days_ahead=3)
    booking = _confirmed(business, at, units=2)

    bookings.cancel(business, booking.id)

    from sqlalchemy import text

    with readonly() as conn:
        reminders = conn.execute(
            text(
                "SELECT status FROM outbound_tasks WHERE business_id = :b AND reason = 'reminder'"
            ),
            {"b": str(business.id)},
        ).fetchall()
    assert all(r.status == "cancelled" for r in reminders), (
        "a cancelled booking would still have been reminded about"
    )


def test_a_do_not_call_guest_is_never_rung():
    """PRD §13: do_not_call is absolute."""
    from sqlalchemy import text

    business = make_business(total_units=4)
    at = future_slot(business)
    booking = _confirmed(business, at, units=2, phone="+919999999999")
    with readonly() as conn:
        pass
    from calling_agent.db import transaction

    with transaction() as conn:
        conn.execute(
            text("UPDATE customers SET do_not_call = true WHERE business_id = :b"),
            {"b": str(business.id)},
        )

    bookings.transition(business, booking.id, "cancelled", channel="call")

    voice = [m for m in _messages(business.id) if m.channel == "voice"]
    assert not voice, "a do-not-call guest was queued for a phone call"


def test_the_sender_is_a_plug_not_a_hardcoded_provider():
    """Another system drops this agent in with its own carrier (PRD §16)."""
    business = make_business(total_units=4)
    at = future_slot(business)
    _confirmed(business, at, units=2)

    sent: list[tuple[str, str]] = []

    @notifications.provider("test-carrier")
    def _carrier(to, body, biz):
        sent.append((to, body))
        return "carrier-1"

    from calling_agent.config import settings

    before = settings.sms_provider
    settings.sms_provider = "test-carrier"
    try:
        # Drained rather than sent once: the table accumulates across the
        # session, and send_queued takes a page at a time, so a single call
        # quietly stopped covering this test's own message.
        assert sum(iter(lambda: notifications.send_queued(), 0)) >= 1
    finally:
        settings.sms_provider = before
        notifications.PROVIDERS.pop("test-carrier", None)

    assert "+919876543210" in [to for to, _ in sent]


def test_holds_expire_back_into_availability():
    business = make_business(total_units=2)
    at = future_slot(business)
    held = holds.take(business.id, at, 2, ttl_seconds=1)
    assert held.ok
    assert not availability.is_available(business, at, 1).ok

    holds.expire_now(held.hold_id)
    holds.sweep_expired()

    assert availability.is_available(business, at, 2).ok


def test_a_booking_is_listed_on_the_service_it_belongs_to_not_the_calendar_day():
    """A venue open until 01:00 puts a 00:30 booking on the night it started."""
    from datetime import UTC, datetime, time

    business = make_business(
        open_at=time(18, 0), close_at=time(1, 0), total_units=8, turn_minutes=30
    )
    local_day = (datetime.now(business.tz) + timedelta(days=2)).date()
    after_midnight = datetime.combine(
        local_day + timedelta(days=1), time(0, 30), tzinfo=business.tz
    ).astimezone(UTC)

    _confirmed(business, after_midnight, units=2)

    listed = bookings.on_local_date(business, local_day)
    assert [b.start_time for b in listed] == [after_midnight], (
        "a late booking fell off the night it belongs to"
    )


# --- the task queue survives a restart ---------------------------------------


def _queue_task(business, reason="reminder", booking_id=None, status="queued", attempts=0):
    from sqlalchemy import text

    from calling_agent.db import transaction

    with transaction() as conn:
        row = conn.execute(
            text(
                "INSERT INTO outbound_tasks"
                " (business_id, booking_id, reason, payload, scheduled_for, status, attempts,"
                "  updated_at)"
                " VALUES (:b, :bk, :r, '{}'::jsonb, now() - interval '1 minute', :s, :a,"
                "         now() - interval '1 hour')"
                " RETURNING id"
            ),
            {"b": str(business.id), "bk": str(booking_id) if booking_id else None,
             "r": reason, "s": status, "a": attempts},
        ).first()
    return row[0]


def _task_status(task_id):
    from sqlalchemy import text

    from calling_agent.db import readonly

    with readonly() as conn:
        return conn.execute(
            text("SELECT status, attempts FROM outbound_tasks WHERE id = :i"),
            {"i": str(task_id)},
        ).first()


def test_a_task_stranded_by_a_restart_is_picked_up_again(business):
    """A deploy between claiming a task and running it used to lose it forever.

    The claim commits, then the handler runs. Kill the process in between and
    the row sits in 'running' with nobody running it -- and only 'queued' rows
    were ever claimed. For overflow_timeout that means a waitlisted guest
    waiting in silence, which is the one thing PRD §11 forbids.
    """
    from calling_agent import jobs

    stranded = _queue_task(business, reason="digest", status="running", attempts=1)
    jobs.run_due_tasks()
    assert _task_status(stranded).status == "done"


def test_a_failed_task_is_retried_until_its_attempts_run_out(business, monkeypatch):
    from calling_agent import jobs
    from calling_agent.config import settings

    monkeypatch.setattr(settings, "task_max_attempts", 3)
    tries = []

    @jobs.handles("flaky_test_task")
    def _flaky(business, task):
        tries.append(task["id"])
        raise RuntimeError("carrier blip")

    try:
        task_id = _queue_task(business, reason="flaky_test_task")
        for _ in range(5):
            jobs.run_due_tasks()
            _age_task(task_id)

        assert len(tries) == 3, "tried three times, then stopped"
        assert _task_status(task_id).status == "abandoned"
    finally:
        jobs.HANDLERS.pop("flaky_test_task", None)


def _age_task(task_id):
    """Push updated_at back so the next pass sees the lease as expired."""
    from sqlalchemy import text

    from calling_agent.db import transaction

    with transaction() as conn:
        conn.execute(
            text("UPDATE outbound_tasks SET updated_at = now() - interval '1 hour'"
                 " WHERE id = :i"),
            {"i": str(task_id)},
        )


def test_a_task_with_no_handler_is_not_retried_forever(business):
    from calling_agent import jobs

    task_id = _queue_task(business, reason="nonexistent_reason")
    jobs.run_due_tasks()
    assert _task_status(task_id).status == "abandoned"


def test_a_retried_reminder_does_not_text_the_guest_twice(business):
    """Why retrying was never safe to add until messages deduped."""
    from sqlalchemy import text

    from calling_agent import jobs, notifications
    from calling_agent.db import readonly, transaction

    at = future_slot(business)
    held = holds.take(business.id, at, 2)
    booking = holds.confirm(business.id, held.hold_id, name="Ravi",
                            phone=a_number())

    task = {"id": _queue_task(business, booking_id=booking.id), "booking_id": booking.id}
    for _ in range(3):
        with transaction() as conn:
            notifications.queue_for_booking(
                conn, business, booking, "reminder", dedupe_key=jobs.message_key(task)
            )

    with readonly() as conn:
        sent = conn.execute(
            text("SELECT count(*) FROM messages WHERE booking_id = :b AND kind = 'reminder'"),
            {"b": str(booking.id)},
        ).scalar_one()
    assert sent == 1, "three runs of one task is still one text"


# --- the staff phone --------------------------------------------------------


def _staff_texts(business, kind):
    from sqlalchemy import text

    from calling_agent.db import readonly

    with readonly() as conn:
        return conn.execute(
            text("SELECT to_address, body FROM messages WHERE business_id = :b AND kind = :k"),
            {"b": str(business.id), "k": kind},
        ).fetchall()


def test_the_digest_is_texted_to_the_staff_number_once(business):
    """It computed the right list for weeks and nobody saw it."""
    from calling_agent import jobs

    at = future_slot(business, days_ahead=0, hour=19)
    held = holds.take(business.id, at, 3)
    holds.confirm(business.id, held.hold_id, name="Sharma", phone=a_number())

    staffed = make_business(config={"messaging": {"staff_number": "+919999900001"}})
    task = {"id": _queue_task(staffed, reason="digest")}
    jobs.daily_digest(staffed, task)
    jobs.daily_digest(staffed, task)          # a retry is not a second text

    sent = _staff_texts(staffed, "digest")
    assert len(sent) == 1
    assert sent[0].to_address == "+919999900001"
    assert "tonight" in sent[0].body


def test_no_staff_number_means_no_text_and_no_error(business):
    from calling_agent import jobs

    jobs.daily_digest(business, {"id": _queue_task(business, reason="digest")})
    assert _staff_texts(business, "digest") == []


def test_the_nudge_switch_turns_nudges_off_but_not_the_digest(business):
    from calling_agent import jobs

    staffed = make_business(
        config={"messaging": {"staff_number": "+919999900002", "send_nudges": False}}
    )
    at = future_slot(staffed)
    held = holds.take(staffed.id, at, 2)
    booking = holds.confirm(staffed.id, held.hold_id, name="Patel", phone=a_number())

    jobs.arrival_nudge(
        staffed, {"id": _queue_task(staffed, reason="arrival_nudge", booking_id=booking.id),
                  "booking_id": booking.id},
    )
    assert _staff_texts(staffed, "nudge") == [], "switched off"

    jobs.daily_digest(staffed, {"id": _queue_task(staffed, reason="digest")})
    assert len(_staff_texts(staffed, "digest")) == 1, "the other switch is still on"


# --- the dialler -------------------------------------------------------------


def _queue_voice(business, to="+919999900010", body="We are sorry, 8 PM is full."):
    from sqlalchemy import text

    from calling_agent.db import transaction

    with transaction() as conn:
        row = conn.execute(
            text(
                "INSERT INTO messages (business_id, direction, channel, to_address, body,"
                " status, kind) VALUES (:b, 'outbound', 'voice', :to, :body, 'queued', 'declined')"
                " RETURNING id"
            ),
            {"b": str(business.id), "to": to, "body": body},
        ).first()
    return row[0]


def _message(message_id):
    from sqlalchemy import text

    from calling_agent.db import readonly

    with readonly() as conn:
        return conn.execute(
            text("SELECT status, channel, provider_id, attempts, send_after FROM messages"
                 " WHERE id = :i"),
            {"i": str(message_id)},
        ).first()


def _hhmm(dt):
    return dt.strftime("%H:%M")


def _dialler_ready(monkeypatch):
    from calling_agent.config import settings

    monkeypatch.setattr(settings, "voice_provider", "log")
    monkeypatch.setattr(settings, "public_hostname", "venue.test")


def test_a_voice_message_inside_the_window_is_dialled(monkeypatch):
    """A row whose channel is voice is a call, not a text."""
    _dialler_ready(monkeypatch)
    always = make_business(config={"outbound": {"window_start": "00:00", "window_end": "23:59"}})
    message_id = _queue_voice(always)

    assert notifications.send_queued() >= 1
    row = _message(message_id)
    assert row.status == "sent"
    assert row.provider_id.startswith("log-call-"), "went to the dialler, not the SMS provider"
    assert row.attempts == 1


def test_a_voice_message_outside_the_window_waits_for_it(monkeypatch):
    """Nobody is rung by a restaurant at seven in the morning."""
    _dialler_ready(monkeypatch)
    now = datetime.now(UTC)
    shut = make_business(config={"outbound": {
        "window_start": _hhmm(now + timedelta(hours=2)),
        "window_end": _hhmm(now + timedelta(hours=3)),
    }})
    message_id = _queue_voice(shut)

    notifications.send_queued()
    row = _message(message_id)
    assert row.status == "queued", "still waiting"
    assert row.provider_id is None
    assert row.send_after is not None and row.send_after > now, "parked until the window opens"


def test_outbound_calls_switched_off_fall_back_to_a_text(monkeypatch):
    _dialler_ready(monkeypatch)
    off = make_business(config={"outbound": {"enabled": False}})
    message_id = _queue_voice(off)
    notifications.send_queued()
    assert _message(message_id).channel == "sms"


def test_an_unanswered_call_rings_again_then_becomes_a_text(monkeypatch):
    """PRD §13: two attempts, then the same words go by SMS."""
    _dialler_ready(monkeypatch)
    always = make_business(config={"outbound": {
        "window_start": "00:00", "window_end": "23:59", "max_attempts": 2,
    }})
    message_id = _queue_voice(always)
    notifications.send_queued()
    first = _message(message_id)
    assert first.attempts == 1

    notifications.call_ended(first.provider_id, "no-answer")
    second = _message(message_id)
    assert second.status == "queued" and second.channel == "voice", "one more try"
    assert second.send_after is not None, "but not straight away"

    # make it due, ring again, unanswered again
    from sqlalchemy import text

    from calling_agent.db import transaction

    with transaction() as conn:
        conn.execute(text("UPDATE messages SET send_after = NULL WHERE id = :i"),
                     {"i": str(message_id)})
    notifications.send_queued()
    rung_twice = _message(message_id)
    assert rung_twice.attempts == 2
    notifications.call_ended(rung_twice.provider_id, "no-answer")

    final = _message(message_id)
    assert final.channel == "sms" and final.status == "queued", "attempts spent: text instead"


def test_an_answered_call_is_delivered_and_not_retried(monkeypatch):
    _dialler_ready(monkeypatch)
    always = make_business(config={"outbound": {"window_start": "00:00", "window_end": "23:59"}})
    message_id = _queue_voice(always)
    notifications.send_queued()
    notifications.call_ended(_message(message_id).provider_id, "completed")
    assert _message(message_id).status == "delivered"


def test_a_status_for_a_call_we_did_not_place_is_ignored():
    notifications.call_ended("CA-not-ours", "no-answer")   # must not raise
