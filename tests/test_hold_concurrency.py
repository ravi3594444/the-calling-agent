"""The acceptance test from PRD §7, written before the hold code it tests.

    Two concurrent holds for the last available unit -- exactly one succeeds,
    the other receives alternatives, and committed_units is correct.

This is the test the PRD says to write first, and the reason is worth keeping
in the file: an agent asked to write the hold and its test together writes a
test that passes against its own bug. So this file states the contract only --
what a caller gets, and what the counter reads afterwards -- and never reaches
into how holds are stored.

Everything the product sells rests here. If two callers can hold the last
table, the failure is not a stack trace; it is two families at one table on a
Friday night.
"""

from __future__ import annotations

import threading
from datetime import UTC, timedelta

import pytest

from calling_agent import holds
from tests.conftest import committed, future_slot, make_business


def _race(fn, n: int):
    """Run fn() in n threads released at the same instant. Returns results in order.

    A barrier, not just threads: without one the first thread routinely finishes
    before the last starts, and the test passes on a serial execution that
    proves nothing.
    """
    barrier = threading.Barrier(n)
    results: list[object] = [None] * n
    errors: list[BaseException | None] = [None] * n

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=20)
            results[i] = fn(i)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the main thread
            errors[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    for err in errors:
        if err is not None:
            raise err
    return results


def test_two_holds_for_the_last_unit_exactly_one_wins():
    business = make_business(total_units=2, turn_minutes=90, slot_minutes=30)
    at = future_slot(business)

    results = _race(lambda _i: holds.take(business.id, at, 2), 2)

    won = [r for r in results if r.ok]
    lost = [r for r in results if not r.ok]
    assert len(won) == 1, "both callers were given the last table"
    assert len(lost) == 1

    # The loser is never left with a flat no.
    assert lost[0].alternatives, "the caller who lost the race got no alternatives"

    # And the counter reflects exactly one booking's worth, in every slot the
    # turn covers -- not just the first.
    for step in range(3):  # 90 minutes of 30-minute slots
        assert committed(business.id, at + timedelta(minutes=30 * step)) == 2


def test_many_concurrent_holds_never_exceed_capacity():
    """Ten callers, room for four. The counter is the assertion."""
    business = make_business(total_units=4, turn_minutes=30, slot_minutes=30)
    at = future_slot(business)

    results = _race(lambda _i: holds.take(business.id, at, 1), 10)

    won = [r for r in results if r.ok]
    assert len(won) == 4, f"{len(won)} callers held 4 units of capacity"
    assert committed(business.id, at) == 4


def test_multi_slot_holds_race_without_deadlocking():
    """Overlapping multi-slot bookings taken in opposite directions.

    Two bookings that overlap on the middle slot, started simultaneously. If
    slots were locked in the order each request happens to name them rather
    than in ascending slot_start, this is the shape that deadlocks: one
    transaction holding A waiting for B while the other holds B waiting for A.
    """
    business = make_business(total_units=2, turn_minutes=60, slot_minutes=30)
    first = future_slot(business, hour=19, minute=0)   # 19:00 + 19:30
    second = first + timedelta(minutes=30)             # 19:30 + 20:00

    results = _race(lambda i: holds.take(business.id, first if i % 2 == 0 else second, 2), 6)

    won = [r for r in results if r.ok]
    assert len(won) == 1, "the overlapping slot was sold twice"
    assert committed(business.id, first + timedelta(minutes=30)) == 2


def test_confirm_revalidates_and_cannot_resurrect_an_expired_hold():
    """PRD §7: confirm must never succeed on the strength of an earlier check."""
    business = make_business(total_units=4)
    at = future_slot(business)

    held = holds.take(business.id, at, 4, ttl_seconds=30)
    assert held.ok

    holds.expire_now(held.hold_id)          # the sweeper's effect, deterministically
    assert holds.sweep_expired() >= 1
    assert committed(business.id, at) == 0, "an expired hold kept its capacity"

    with pytest.raises(holds.HoldExpired):
        holds.confirm(business.id, held.hold_id, name="Ravi", phone="+919876543210")


def test_a_confirmed_booking_keeps_the_capacity_the_hold_took():
    """The counter moves once, at the hold, and does not double on confirm."""
    business = make_business(total_units=4)
    at = future_slot(business)

    held = holds.take(business.id, at, 3)
    assert held.ok
    assert committed(business.id, at) == 3

    booking = holds.confirm(business.id, held.hold_id, name="Ravi", phone="+919876543210")
    assert booking.reference
    assert committed(business.id, at) == 3, "confirming charged the slot twice"

    # And the freed unit is still sellable to somebody else.
    assert holds.take(business.id, at, 1).ok
    assert not holds.take(business.id, at, 1).ok


def test_confirming_the_same_hold_twice_returns_the_same_booking():
    """A retried tool call must not mint a second table (PRD §8)."""
    business = make_business(total_units=6)
    at = future_slot(business)
    held = holds.take(business.id, at, 2)

    first = holds.confirm(business.id, held.hold_id, name="Ravi", phone="+919876543210")
    second = holds.confirm(business.id, held.hold_id, name="Ravi", phone="+919876543210")

    assert first.reference == second.reference
    assert committed(business.id, at) == 2


def test_concurrent_confirms_of_one_hold_make_one_booking():
    business = make_business(total_units=6)
    at = future_slot(business)
    held = holds.take(business.id, at, 2)

    results = _race(
        lambda _i: holds.confirm(business.id, held.hold_id, name="Ravi", phone="+91987654321"),
        4,
    )
    assert len({r.reference for r in results}) == 1
    assert committed(business.id, at) == 2


def test_a_hold_belongs_to_its_tenant_only():
    """business_id is on every query (PRD §20). A hold is not portable."""
    one = make_business(total_units=4)
    two = make_business(total_units=4)
    at = future_slot(one)

    held = holds.take(one.id, at, 2)
    assert held.ok
    with pytest.raises(holds.HoldNotFound):
        holds.confirm(two.id, held.hold_id, name="Someone", phone="+910000000000")


# --- deterministic cover for what the races only catch by luck ---------------
#
# The tests above were run against deliberately broken implementations to see
# whether they notice. Two bugs survived them:
#
#   * checking headroom on the FIRST slot of a turn instead of every slot --
#     caught by the multi-slot race only when the threads happen to interleave
#     the wrong way, which on this machine they mostly do not;
#   * confirm() skipping its expiry re-check -- never caught, because the test
#     above sweeps the hold first and the sweep sets released_at, which a
#     different branch rejects.
#
# A test that catches a bug one run in five is not a test. These two are
# deterministic and single-threaded, and they fail on those bugs every time.


def test_a_turn_is_refused_when_a_LATER_slot_is_full():
    """The booking increments every slot it overlaps, so every one must have room.

    A 90-minute turn starting at 19:00 needs 19:00, 19:30 and 20:00. Fill only
    20:00 and the 19:00 booking must still be refused -- it would otherwise sit
    down at seven and be asked to leave at eight.
    """
    business = make_business(total_units=4, turn_minutes=90, slot_minutes=30)
    at = future_slot(business, hour=19, minute=0)

    # Fill 20:00 by booking the 20:00 turn to capacity. Nothing touches 19:00.
    filler = holds.take(business.id, at + timedelta(minutes=60), 4)
    assert filler.ok
    assert committed(business.id, at) == 0, "the filler should not touch 19:00"
    assert committed(business.id, at + timedelta(minutes=60)) == 4

    blocked = holds.take(business.id, at, 1)
    assert not blocked.ok, "held a table whose turn runs into a full slot"
    assert blocked.reason == "full"


def test_confirm_refuses_a_hold_that_expired_but_has_not_been_swept():
    """The dangerous window: expired seconds ago, sweeper runs once a minute.

    Nothing has set released_at yet, so only confirm's own re-read of the
    expiry stands between a lapsed claim and a table that was sold to somebody
    else in the meantime.
    """
    business = make_business(total_units=4)
    at = future_slot(business)

    held = holds.take(business.id, at, 2, ttl_seconds=60)
    assert held.ok
    holds.expire_now(held.hold_id)          # no sweep -- released_at is still NULL

    with pytest.raises(holds.HoldExpired):
        holds.confirm(business.id, held.hold_id, name="Ravi", phone="+919876543210")


def test_a_released_hold_cannot_be_confirmed():
    business = make_business(total_units=4)
    at = future_slot(business)
    held = holds.take(business.id, at, 2)

    assert holds.release(business.id, held.hold_id) is True
    assert committed(business.id, at) == 0
    assert holds.release(business.id, held.hold_id) is False, "double release moved the counter"

    with pytest.raises(holds.HoldExpired):
        holds.confirm(business.id, held.hold_id, name="Ravi")


def test_headroom_is_decided_by_the_tightest_slot():
    """A unit test, because the race only reaches this function by luck.

    `headroom` is what stands between a stale availability answer and a double
    booking. The race above exercises it only when the threads interleave the
    wrong way, which is not most runs -- so its contract is pinned directly.
    """
    from datetime import datetime

    from calling_agent import slots

    base = datetime(2030, 1, 1, 19, 0, tzinfo=UTC)

    def row(offset_minutes: int, committed: int):
        at = base + timedelta(minutes=offset_minutes)
        return at, slots.SlotRow(at, 30, 10, committed)

    rows = dict([row(0, 1), row(30, 9), row(60, 2)])
    assert slots.headroom(rows, sellable=10) == 1, "the fullest slot must decide"
    assert slots.headroom({}, sellable=10) == 10


def test_the_hold_refuses_even_when_availability_says_yes(monkeypatch):
    """The answer given before the lock is a guess. The lock is the truth.

    Simulates the race deterministically: availability is forced to say yes
    while the slot is in fact full. Only the re-check inside the transaction
    can catch this, so this test fails on any implementation that trusts the
    answer it was given a moment earlier.
    """
    from calling_agent import availability

    # Multi-slot on purpose: the full slot is the LAST one the turn covers, so
    # an implementation that re-checks only the slot the caller named is caught
    # here too, not just one that re-checks nothing.
    business = make_business(total_units=2, turn_minutes=90, slot_minutes=30)
    at = future_slot(business, hour=19, minute=0)
    later = at + timedelta(minutes=60)

    assert holds.take(business.id, later, 2).ok
    assert committed(business.id, later) == 2
    assert committed(business.id, at) == 0

    monkeypatch.setattr(
        availability,
        "is_available",
        lambda *a, **k: availability.Availability(True, availability.OK),
    )
    result = holds.take(business.id, at, 2)

    assert not result.ok, "the hold trusted a stale availability answer"
    assert committed(business.id, at) == 0, "a refused hold still moved the counter"
