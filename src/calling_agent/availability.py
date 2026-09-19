"""The one function the whole product rests on (PRD §7).

    is_available(business, start_time, units) -> {ok, alternatives[]}

If this is wrong, every downstream feature is wrong, and the failure is not a
stack trace -- it is a family standing in a full restaurant holding a
confirmation. So the rules are stated once, here, and nothing else in the
codebase decides whether a time is bookable.

Nothing in this module writes. It reads capacity_rules, overrides and the slot
counters and answers a question. Taking capacity is holds.py, and it re-asks
this question under a lock, because an answer given without one is a guess by
the time the caller finishes their sentence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Connection

from . import businesses, holidays
from .businesses import Business
from .db import fetch_all, readonly

log = logging.getLogger(__name__)

#: How far either side of a full slot to look for an alternative, in slots.
ALTERNATIVE_STEPS = (1, -1, 2, -2, 3, -3, 4, -4)
MAX_ALTERNATIVES = 3


# --- reasons -----------------------------------------------------------------
# Machine codes, so callers branch on the code and only the agent-facing layer
# writes sentences. A UI that parsed prose would break on a copy edit.

CLOSED = "closed"
HOLIDAY = "holiday"
PAST = "past"
TOO_SOON = "too_soon"
OUTSIDE_WINDOW = "outside_window"
OFF_GRID = "off_grid"
LAST_SEATING = "last_seating"
FULL = "full"
PARTY_TOO_LARGE = "party_too_large"
NO_CAPACITY_CONFIGURED = "no_capacity_configured"
OK = "ok"


@dataclass(frozen=True)
class Service:
    """One continuous stretch of opening on one local date."""

    starts_at: datetime          # UTC
    ends_at: datetime            # UTC, exclusive
    total_units: int
    slot_minutes: int
    turn_minutes: int
    label: str = ""
    service_date: date | None = None

    def slot_starts(self) -> list[datetime]:
        out, cursor = [], self.starts_at
        step = timedelta(minutes=self.slot_minutes)
        while cursor < self.ends_at:
            out.append(cursor)
            cursor += step
        return out

    def bookable_starts(self) -> list[datetime]:
        """Starts whose whole turn finishes before closing (PRD §7 step 3)."""
        turn = timedelta(minutes=self.turn_minutes)
        return [s for s in self.slot_starts() if s + turn <= self.ends_at]


@dataclass(frozen=True)
class Availability:
    ok: bool
    reason: str
    alternatives: tuple[datetime, ...] = ()
    slot_starts: tuple[datetime, ...] = ()
    end_time: datetime | None = None
    #: Free units at the requested time, when the question got that far.
    free: int | None = None
    capacity: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)


# --- opening hours -----------------------------------------------------------


def services_on(business: Business, local_date: date) -> list[Service]:
    """Every service window that STARTS on this local date, overrides applied.

    A window whose end_time is at or before its start_time runs past midnight;
    it still belongs to the date it started on, which is how a venue thinks
    about "Friday night" at one in the morning.
    """
    override = businesses.override_on(business.id, local_date)

    if override and override.type == "closed":
        return []
    if (
        override is None
        and business.config["locale"]["observe_public_holidays"]
        and holidays.is_holiday(business.config["locale"]["country"], local_date)
    ):
        # Owner has not said anything about the day and it is a public holiday
        # where they are: closed until they open it (PRD §15b).
        return []

    rules = business.rules_for(local_date.weekday())
    if override and override.type == "extended" and override.start_time and override.end_time:
        rules = [
            businesses.CapacityRule(
                weekday=local_date.weekday(),
                start_time=override.start_time,
                end_time=override.end_time,
                total_units=override.total_units
                or (rules[0].total_units if rules else 0),
                slot_minutes=rules[0].slot_minutes if rules else 30,
                turn_minutes=rules[0].turn_minutes if rules else 60,
                label=override.reason or "Extended",
            )
        ]

    tz = business.tz
    out: list[Service] = []
    for rule in rules:
        starts_at = datetime.combine(local_date, rule.start_time, tzinfo=tz)
        end_date = local_date if rule.end_time > rule.start_time else local_date + timedelta(days=1)
        ends_at = datetime.combine(end_date, rule.end_time, tzinfo=tz)
        total = rule.total_units
        if override and override.type == "reduced" and override.total_units is not None:
            total = min(total, override.total_units)
        out.append(
            Service(
                starts_at=starts_at.astimezone(UTC),
                ends_at=ends_at.astimezone(UTC),
                total_units=total,
                slot_minutes=rule.slot_minutes,
                turn_minutes=rule.turn_minutes,
                label=rule.label,
                service_date=local_date,
            )
        )
    return sorted(out, key=lambda s: s.starts_at)


def services_covering(business: Business, moment: datetime) -> list[Service]:
    """Services that contain this instant, including one that began yesterday."""
    local = moment.astimezone(business.tz)
    candidates: list[Service] = []
    for offset in (0, -1):
        candidates += services_on(business, local.date() + timedelta(days=offset))
    return [s for s in candidates if s.starts_at <= moment < s.ends_at]


def service_for(business: Business, moment: datetime) -> Service | None:
    found = services_covering(business, moment)
    return found[0] if found else None


# --- slot counters -----------------------------------------------------------


def _counters(conn: Connection, business_id: UUID, starts: list[datetime]) -> dict[datetime, int]:
    if not starts:
        return {}
    rows = fetch_all(
        conn,
        "SELECT slot_start, committed_units FROM slots"
        " WHERE business_id = :b AND slot_start = ANY(:starts)",
        b=str(business_id),
        starts=starts,
    )
    return {r.slot_start: r.committed_units for r in rows}


def sellable(service: Service, business: Business) -> int:
    """Units the agent may sell in one slot: capacity minus the walk-in buffer."""
    pct = float(business.config["capacity"]["sellable_pct"])
    return int(service.total_units * pct)


def turn_slots(service: Service, start: datetime) -> list[datetime]:
    """Every slot the booking occupies: it increments each one it overlaps."""
    step = timedelta(minutes=service.slot_minutes)
    end = start + timedelta(minutes=service.turn_minutes)
    out, cursor = [], start
    while cursor < end:
        out.append(cursor)
        cursor += step
    return out


# --- the question ------------------------------------------------------------


def is_available(
    business: Business | UUID | str,
    start: datetime,
    units: int,
    *,
    conn: Connection | None = None,
    now: datetime | None = None,
    with_alternatives: bool = True,
) -> Availability:
    """Can this business take `units` at `start`?

    Steps follow PRD §7 in order, and each one returns its own reason: "we are
    closed on Mondays" and "that slot is full" send a caller to completely
    different places, and an agent given only False says neither.
    """
    business = _business(business)
    now = now or datetime.now(UTC)
    start = start.astimezone(UTC)

    policy = business.config["policy"]
    if units < 1:
        return Availability(False, PARTY_TOO_LARGE, detail={"max": policy["max_party_size"]})
    if units > policy["max_party_size"]:
        return Availability(False, PARTY_TOO_LARGE, detail={"max": policy["max_party_size"]})

    if start <= now:
        return Availability(False, PAST)

    lead = int(policy["min_lead_minutes"])
    if lead and start < now + timedelta(minutes=lead):
        return Availability(False, TOO_SOON, detail={"min_lead_minutes": lead})

    local_day = start.astimezone(business.tz).date()
    today_local = now.astimezone(business.tz).date()
    last_day = businesses.window_last_day(business, today_local)
    if local_day > last_day:
        return Availability(False, OUTSIDE_WINDOW, detail={"last_day": last_day.isoformat()})

    # Capacity off, or never configured: the agent takes everything (PRD §15d).
    if not business.tracks_capacity:
        service = service_for(business, start)
        if business.rules and service is None:
            return _closed_or_holiday(business, local_day)
        turn = int(business.config["capacity"]["turn_minutes"])
        return Availability(
            True,
            OK if business.rules else NO_CAPACITY_CONFIGURED,
            slot_starts=(),
            end_time=start + timedelta(minutes=turn),
        )

    service = service_for(business, start)
    if service is None:
        return _closed_or_holiday(business, local_day, alternatives=_alternatives_same_day(
            business, start, units, conn=conn, now=now
        ) if with_alternatives else ())

    if (start - service.starts_at).total_seconds() % (service.slot_minutes * 60):
        return Availability(False, OFF_GRID, detail={"slot_minutes": service.slot_minutes})

    if start + timedelta(minutes=service.turn_minutes) > service.ends_at:
        alts = (
            _alternatives(business, start, units, conn=conn, now=now)
            if with_alternatives
            else ()
        )
        return Availability(
            False, LAST_SEATING, alternatives=alts,
            detail={"closes": service.ends_at.isoformat()},
        )

    starts = turn_slots(service, start)
    room = sellable(service, business)

    def _check(c: Connection) -> Availability:
        taken = _counters(c, business.id, starts)
        worst = max((taken.get(s, 0) for s in starts), default=0)
        if worst + units <= room:
            return Availability(
                True, OK,
                slot_starts=tuple(starts),
                end_time=start + timedelta(minutes=service.turn_minutes),
                free=room - worst,
                capacity=room,
            )
        alts = (
            _alternatives(business, start, units, conn=c, now=now) if with_alternatives else ()
        )
        return Availability(
            False, FULL, alternatives=alts, free=max(0, room - worst), capacity=room
        )

    if conn is not None:
        return _check(conn)
    with readonly() as c:
        return _check(c)


def _closed_or_holiday(
    business: Business, local_day: date, alternatives: tuple[datetime, ...] = ()
) -> Availability:
    country = business.config["locale"]["country"]
    if business.config["locale"]["observe_public_holidays"]:
        name = holidays.holiday_name(country, local_day)
        if name and businesses.override_on(business.id, local_day) is None:
            return Availability(False, HOLIDAY, alternatives=alternatives,
                                detail={"holiday": name})
    return Availability(False, CLOSED, alternatives=alternatives,
                        detail={"weekday": local_day.strftime("%A")})


def alternatives_for(
    business: Business | UUID | str,
    start: datetime,
    units: int,
    *,
    now: datetime | None = None,
) -> tuple[datetime, ...]:
    """Nearest times that work, for a caller who has just been told no.

    Public because holds.py needs it after losing a race -- and it must be
    called OUTSIDE that transaction, since it reads every slot around the one
    whose rows the winner is still holding.
    """
    business = _business(business)
    with readonly() as conn:
        return _alternatives(business, start, units, conn=conn, now=now or datetime.now(UTC))


def _alternatives(
    business: Business,
    start: datetime,
    units: int,
    *,
    conn: Connection | None,
    now: datetime,
) -> tuple[datetime, ...]:
    """The nearest times that DO work, searched outward from the one asked for.

    Outward rather than forward only: a caller who asked for eight is usually
    as happy with quarter to eight as with quarter past, and offering only
    later times reads as "we are full" even when we are not.
    """
    service = service_for(business, start)
    step = timedelta(minutes=service.slot_minutes if service else 30)
    found: list[datetime] = []
    for multiple in ALTERNATIVE_STEPS:
        candidate = start + step * multiple
        if candidate <= now:
            continue
        verdict = is_available(
            business, candidate, units, conn=conn, now=now, with_alternatives=False
        )
        if verdict.ok:
            found.append(candidate)
        if len(found) == MAX_ALTERNATIVES:
            break
    return tuple(sorted(found))


def _alternatives_same_day(
    business: Business, start: datetime, units: int, *, conn: Connection | None, now: datetime
) -> tuple[datetime, ...]:
    """When the venue is shut at that hour, offer times it is actually open."""
    local = start.astimezone(business.tz)
    services = services_on(business, local.date())
    if not services:
        return ()
    found: list[datetime] = []
    for service in services:
        for candidate in service.bookable_starts():
            if candidate <= now:
                continue
            if is_available(
                business, candidate, units, conn=conn, now=now, with_alternatives=False
            ).ok:
                found.append(candidate)
            if len(found) >= MAX_ALTERNATIVES:
                return tuple(found)
    return tuple(found)


def next_available(
    business: Business | UUID | str,
    units: int,
    *,
    after: datetime | None = None,
    days: int = 7,
    limit: int = 3,
) -> list[datetime]:
    """Scan forward for the next times that work, across days.

    Used when the day a caller asked for is gone entirely -- "we're full
    Friday, but I could do Saturday at eight" beats "we're full".
    """
    business = _business(business)
    now = datetime.now(UTC)
    cursor = (after or now).astimezone(business.tz)
    found: list[datetime] = []
    with readonly() as conn:
        for offset in range(days + 1):
            day = cursor.date() + timedelta(days=offset)
            for service in services_on(business, day):
                for candidate in service.bookable_starts():
                    if candidate <= max(now, cursor.astimezone(UTC)):
                        continue
                    if is_available(
                        business, candidate, units, conn=conn, now=now, with_alternatives=False
                    ).ok:
                        found.append(candidate)
                        if len(found) >= limit:
                            return found
    return found


# --- day and month views (the dashboard's calendar) --------------------------


@dataclass(frozen=True)
class SlotView:
    start: datetime
    committed: int
    capacity: int
    sellable: int
    label: str = ""


def day_view(business: Business | UUID | str, local_date: date) -> dict[str, Any]:
    """Slot-by-slot occupancy for one day, empty slots kept.

    The gaps are the useful part (PRD §14): a day that only lists its bookings
    tells a host nothing about where another one could go.
    """
    business = _business(business)
    services = services_on(business, local_date)
    override = businesses.override_on(business.id, local_date)
    country = business.config["locale"]["country"]
    holiday = (
        holidays.holiday_name(country, local_date)
        if business.config["locale"]["observe_public_holidays"]
        else None
    )

    if not services:
        why = ""
        if override and override.type == "closed":
            why = override.reason or "Closed"
        elif holiday:
            why = holiday
        elif not business.rules_for(local_date.weekday()):
            why = "Closed"
        return {
            "date": local_date.isoformat(),
            "closed": True,
            "reason": why or "Closed",
            "holiday": holiday,
            "tracks_capacity": business.tracks_capacity,
            "services": [],
            "bookings": [],
            "covers": 0,
            "booking_count": 0,
            "peak": 0,
            "peak_at": None,
            "capacity": 0,
        }

    all_starts = [s for service in services for s in service.slot_starts()]
    with readonly() as conn:
        taken = _counters(conn, business.id, all_starts)
        first = min(all_starts)
        last = max(all_starts) + timedelta(minutes=services[-1].slot_minutes)
        rows = fetch_all(
            conn,
            "SELECT b.id, b.reference, b.start_time, b.end_time, b.party_size, b.status,"
            "       b.name, b.phone, b.notes, c.visit_count, c.no_show_count"
            "  FROM bookings b LEFT JOIN customers c ON c.id = b.customer_id"
            " WHERE b.business_id = :b AND b.start_time >= :first AND b.start_time < :last"
            "   AND b.status <> 'cancelled' AND b.status <> 'declined'"
            " ORDER BY b.start_time",
            b=str(business.id),
            first=first,
            last=last,
        )

    services_out = []
    peak, peak_at, capacity_seen = 0, None, 0
    for service in services:
        room = sellable(service, business)
        capacity_seen = max(capacity_seen, room)
        slots = []
        for start in service.slot_starts():
            used = taken.get(start, 0)
            if used > peak:
                peak, peak_at = used, start
            slots.append(
                {
                    "start": start.isoformat(),
                    "committed": used,
                    "capacity": room,
                    "total_units": service.total_units,
                    "bookable": start + timedelta(minutes=service.turn_minutes) <= service.ends_at,
                }
            )
        services_out.append(
            {
                "label": service.label,
                "starts_at": service.starts_at.isoformat(),
                "ends_at": service.ends_at.isoformat(),
                "slot_minutes": service.slot_minutes,
                "turn_minutes": service.turn_minutes,
                "capacity": room,
                "total_units": service.total_units,
                "slots": slots,
            }
        )

    bookings = [
        {
            "id": str(r.id),
            "reference": r.reference,
            "start_time": r.start_time.isoformat(),
            "end_time": r.end_time.isoformat(),
            "party_size": r.party_size,
            "status": r.status,
            "name": r.name,
            "phone": r.phone,
            "notes": r.notes,
            "visits": r.visit_count or 0,
            "no_shows": r.no_show_count or 0,
        }
        for r in rows
    ]
    live = [b for b in bookings if b["status"] != "pending"]
    return {
        "date": local_date.isoformat(),
        "closed": False,
        "reason": "",
        "holiday": holiday,
        "reduced": bool(override and override.type == "reduced"),
        "override_reason": override.reason if override else "",
        "tracks_capacity": business.tracks_capacity,
        "services": services_out,
        "bookings": bookings,
        "covers": sum(b["party_size"] for b in live),
        "booking_count": len(live),
        "peak": peak,
        "peak_at": peak_at.isoformat() if peak_at else None,
        "capacity": capacity_seen,
    }


def month_view(business: Business | UUID | str, year: int, month: int) -> dict[str, Any]:
    """One row per day: covers booked, and fullness measured at the busiest slot.

    Day-level fullness is meaningless (PRD §15d): a 40-seat venue with
    90-minute turns seats ~150 covers across a service. The only number that
    decides whether another booking fits is the peak slot.
    """
    business = _business(business)
    first = date(year, month, 1)
    last = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    today_local = datetime.now(business.tz).date()
    window_end = businesses.window_last_day(business, today_local)

    overrides = businesses.overrides_between(business.id, first, last)
    country = business.config["locale"]["country"]
    holiday_map = (
        holidays.between(country, first, last)
        if business.config["locale"]["observe_public_holidays"]
        else {}
    )

    with readonly() as conn:
        rows = fetch_all(
            conn,
            "SELECT slot_start, committed_units FROM slots"
            " WHERE business_id = :b AND slot_start >= :first AND slot_start < :last",
            b=str(business.id),
            first=datetime.combine(first, time(0), tzinfo=business.tz).astimezone(UTC)
            - timedelta(days=1),
            last=datetime.combine(last + timedelta(days=2), time(0), tzinfo=business.tz).astimezone(
                UTC
            ),
        )
        counters = {r.slot_start: r.committed_units for r in rows}
        booking_rows = fetch_all(
            conn,
            "SELECT start_time, party_size FROM bookings"
            " WHERE business_id = :b AND start_time >= :first AND start_time < :last"
            "   AND status IN ('confirmed', 'arrived')",
            b=str(business.id),
            first=datetime.combine(first, time(0), tzinfo=business.tz).astimezone(UTC)
            - timedelta(days=1),
            last=datetime.combine(last + timedelta(days=2), time(0), tzinfo=business.tz).astimezone(
                UTC
            ),
        )

    days = []
    cursor = first
    while cursor <= last:
        services = services_on(business, cursor)
        override = overrides.get(cursor)
        holiday = holiday_map.get(cursor)
        entry: dict[str, Any] = {
            "date": cursor.isoformat(),
            "in_window": cursor <= window_end,
            "holiday": holiday,
            "closed": not services,
            "reason": "",
            "covers": 0,
            "bookings": 0,
            "peak": 0,
            "capacity": 0,
            "reduced": bool(override and override.type == "reduced"),
        }
        if not services:
            entry["reason"] = (
                (override.reason or "Closed")
                if override and override.type == "closed"
                else (holiday or "Closed")
            )
        else:
            starts = {s for service in services for s in service.slot_starts()}
            entry["peak"] = max((counters.get(s, 0) for s in starts), default=0)
            entry["capacity"] = max(sellable(s, business) for s in services)
            in_day = [
                r for r in booking_rows
                if any(service.starts_at <= r.start_time < service.ends_at for service in services)
            ]
            entry["covers"] = sum(r.party_size for r in in_day)
            entry["bookings"] = len(in_day)
        days.append(entry)
        cursor += timedelta(days=1)

    return {
        "year": year,
        "month": month,
        "tracks_capacity": business.tracks_capacity,
        "window_last_day": window_end.isoformat(),
        "today": today_local.isoformat(),
        "days": days,
    }


def _business(value: Business | UUID | str) -> Business:
    return value if isinstance(value, Business) else businesses.by_id(value)
