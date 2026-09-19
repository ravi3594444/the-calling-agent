"""The contention point (PRD §6): locking and moving the slot counters.

Every path that takes or returns capacity comes through here, so the lock
ordering rule is stated once and cannot be forgotten by the next caller.

THE RULE
Slots are always locked in ascending slot_start order. A booking spanning
19:00 and 19:30 taken at the same moment as one spanning 19:30 and 20:00
deadlocks the instant two transactions disagree about which row to take first.
Ascending order is arbitrary but shared, and shared is the whole requirement.

WHY ONE STATEMENT PER ROW
`SELECT ... WHERE slot_start = ANY(:starts) ORDER BY slot_start FOR UPDATE`
reads like it locks in order, and usually does. But the planner is free to
sort AFTER locking, and then the ORDER BY decorates the result while the locks
were taken in whatever order the scan produced. Three or four single-row
locks are a few hundred microseconds and are ordered by construction. The
PRD's ORDER BY is the intent; this is the version that keeps it under a
planner change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Connection, text

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SlotRow:
    slot_start: datetime
    slot_minutes: int
    capacity_total: int
    committed_units: int


def lock(
    conn: Connection,
    business_id: UUID | str,
    starts: list[datetime],
    *,
    capacity_total: int,
    slot_minutes: int,
) -> dict[datetime, SlotRow]:
    """Create any missing slot rows, then lock every one, ascending.

    Rows are created lazily because a year of 30-minute slots per business is
    35,000 rows nobody has booked. The insert is ON CONFLICT DO NOTHING and
    ordered for the same reason the locks are: two callers inserting the same
    two rows in opposite orders deadlock in the insert, before either reaches
    the lock.

    `capacity_total` is written on creation and left alone afterwards. A rule
    changed at four o'clock must not re-price the units already committed at
    three; the dashboard changes future capacity by writing an override, which
    reprices explicitly through `set_capacity`.
    """
    ordered = sorted(set(starts))
    for start in ordered:
        conn.execute(
            text(
                "INSERT INTO slots (business_id, slot_start, slot_minutes, capacity_total,"
                " committed_units) VALUES (:b, :s, :m, :cap, 0)"
                " ON CONFLICT (business_id, slot_start) DO NOTHING"
            ),
            {"b": str(business_id), "s": start, "m": slot_minutes, "cap": capacity_total},
        )

    rows: dict[datetime, SlotRow] = {}
    for start in ordered:
        row = conn.execute(
            text(
                "SELECT slot_start, slot_minutes, capacity_total, committed_units FROM slots"
                " WHERE business_id = :b AND slot_start = :s FOR UPDATE"
            ),
            {"b": str(business_id), "s": start},
        ).fetchone()
        if row is None:
            # Only reachable if another transaction deleted the row between the
            # insert and the lock. Nothing in this product deletes slots, so
            # this is a bug report, not a retry.
            raise RuntimeError(f"slot {start.isoformat()} vanished while locking it")
        rows[row.slot_start] = SlotRow(
            row.slot_start, row.slot_minutes, row.capacity_total, row.committed_units
        )
    return rows


def headroom(rows: dict[datetime, SlotRow], sellable: int) -> int:
    """Units still sellable across every slot: the tightest one decides."""
    if not rows:
        return sellable
    return min(sellable - row.committed_units for row in rows.values())


def add(conn: Connection, business_id: UUID | str, starts: list[datetime], units: int) -> None:
    """Move the counters. Caller must already hold the locks."""
    if not starts or not units:
        return
    conn.execute(
        text(
            "UPDATE slots SET committed_units = committed_units + :u"
            " WHERE business_id = :b AND slot_start = ANY(:starts)"
        ),
        {"u": units, "b": str(business_id), "starts": sorted(set(starts))},
    )


def give_back(
    conn: Connection, business_id: UUID | str, starts: list[datetime], units: int
) -> None:
    """Return capacity, clamped at zero.

    Clamped because the counter is the thing customers stand in: a double
    release that drove it negative would sell the room twice over, silently,
    and the CHECK constraint would only fail the unlucky later caller. GREATEST
    turns a bug into an over-count, which is the survivable direction.
    """
    if not starts or not units:
        return
    conn.execute(
        text(
            "UPDATE slots SET committed_units = GREATEST(0, committed_units - :u)"
            " WHERE business_id = :b AND slot_start = ANY(:starts)"
        ),
        {"u": units, "b": str(business_id), "starts": sorted(set(starts))},
    )


def set_capacity(
    conn: Connection, business_id: UUID | str, starts: list[datetime], capacity_total: int
) -> None:
    """Re-price existing slot rows, for a same-day capacity override."""
    if not starts:
        return
    conn.execute(
        text(
            "UPDATE slots SET capacity_total = :cap"
            " WHERE business_id = :b AND slot_start = ANY(:starts)"
        ),
        {"cap": capacity_total, "b": str(business_id), "starts": sorted(set(starts))},
    )
