"""The dashboard API (PRD §16b).

One endpoint per method on the dashboard's `api` object, in the same order, so
the two files can be read side by side. Every response is JSON the client
renders directly -- no formatting decisions are left to the browser, because
the clock, the date order and the currency are per business and the server is
the only thing that knows them.

Every handler takes `business` from `current_business`. None of them reads a
business id from the request.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import text

from .. import availability, bookings, business_config, businesses, formatting, holidays
from ..businesses import Business
from ..db import fetch_all, fetch_one, healthy, readonly, transaction
from .deps import current_business

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["dashboard"])

#: Day tabs the bookings screen offers. Values are resolved server-side so
#: "tonight" means the venue's tonight, not the browser's.
DAY_KEYS = ("tonight", "tomorrow", "week")


# --- health ------------------------------------------------------------------


@router.get("/health")
def health() -> dict[str, Any]:
    """Is the server usable? Answers without a token: it is a probe, not data."""
    ok, detail = healthy()
    return {"ok": ok, "detail": detail, "time": datetime.now(UTC).isoformat()}


# --- bootstrap ---------------------------------------------------------------


@router.get("/bootstrap")
def bootstrap(business: Business = Depends(current_business)) -> dict[str, Any]:
    """Everything the page needs before it can render anything.

    One request rather than six, because the dashboard is opened on a phone on
    restaurant wifi and must be usable in under two seconds (PRD §14).
    """
    config = business.config
    today = datetime.now(business.tz).date()
    return {
        "business": {
            "id": str(business.id),
            "slug": business.slug,
            "name": config["identity"]["display_name"] or business.name,
            "vertical": business.vertical,
            "timezone": business.timezone,
            "phone_number": business.phone_number,
            "tracks_capacity": business.tracks_capacity,
            "unit_singular": business.unit_singular,
            "unit_plural": business.unit_plural,
        },
        "locale": config["locale"],
        "config": config,
        "today": today.isoformat(),
        "window_last_day": businesses.window_last_day(business, today).isoformat(),
        "capacity_rules": [
            {
                "weekday": rule.weekday,
                "start_time": rule.start_time.isoformat(timespec="minutes"),
                "end_time": rule.end_time.isoformat(timespec="minutes"),
                "total_units": rule.total_units,
                "slot_minutes": rule.slot_minutes,
                "turn_minutes": rule.turn_minutes,
                "label": rule.label,
            }
            for rule in business.rules
        ],
        "holidays_supported": holidays.supported(config["locale"]["country"]),
    }


# --- bookings ----------------------------------------------------------------


@router.get("/bookings")
def list_bookings(
    business: Business = Depends(current_business),
    date_key: str = Query("tonight", alias="date"),
) -> dict[str, Any]:
    """The bookings screen. `date` is a tab name or an ISO date."""
    first, last, title = _range_for(business, date_key)
    rows = bookings.between(business, first, last)
    visible = [b for b in rows if b.status != bookings.PENDING]

    blocks: dict[str, dict[str, Any]] = {}
    for booking in visible:
        key = _block_key(business, booking.start_time, date_key)
        block = blocks.setdefault(key, {"label": key, "committed": 0, "capacity": None})
        block["committed"] += booking.party_size

    if business.tracks_capacity:
        for booking in visible:
            key = _block_key(business, booking.start_time, date_key)
            service = availability.service_for(business, booking.start_time)
            if service is not None:
                blocks[key]["capacity"] = availability.sellable(service, business)

    covers = sum(b.party_size for b in visible)
    seated = sum(b.party_size for b in visible if b.status == bookings.ARRIVED)
    return {
        "key": date_key,
        "title": title,
        "bookings": [_booking_json(business, b, date_key) for b in visible],
        "blocks": list(blocks.values()),
        "summary": {
            "covers": covers,
            "count": len(visible),
            "seated": seated,
            "unit_plural": business.unit_plural,
        },
        "served_at": datetime.now(UTC).isoformat(),
        "last_seating": _last_seating(business, first),
    }


@router.get("/bookings/pending")
def list_pending(business: Business = Depends(current_business)) -> dict[str, Any]:
    """The "Waiting on you" card: the only thing on screen asking for a decision."""
    waiting = bookings.pending_requests(business)
    if not waiting:
        return {"pending": None, "count": 0}

    first = waiting[0]
    deadline = bookings.overflow_deadline(business, first.created_at)
    alternatives = availability.alternatives_for(business, first.start_time, first.party_size)
    policy = business.config["policy"]

    return {
        "count": len(waiting),
        "pending": {
            "id": str(first.id),
            "reference": first.reference,
            "time": formatting.time_str(business, first.start_time),
            "date_long": formatting.date_long(business, first.start_time),
            "name": first.name,
            "party_size": first.party_size,
            "phone": first.phone,
            "why": _why_pending(business, first, policy),
            "deadline": formatting.time_str(business, deadline),
            "deadline_iso": deadline.isoformat(),
            "alternative": (
                formatting.time_str(business, alternatives[0]) if alternatives else ""
            ),
            "notes": first.notes,
        },
        "defaults": business.config["telling_guest"],
    }


@router.patch("/bookings/{booking_id}")
def update_booking(
    booking_id: str,
    patch: dict[str, Any] = Body(...),
    business: Business = Depends(current_business),
) -> dict[str, Any]:
    """Status, notes or time. One field at a time, as the UI sends them."""
    if "status" in patch:
        return _apply_status(business, booking_id, str(patch["status"]))

    if "notes" in patch:
        updated = bookings.update_notes(business, booking_id, str(patch["notes"]))
        return _booking_json(business, updated)

    if "start_time" in patch:
        try:
            moved = bookings.move(
                business, booking_id, datetime.fromisoformat(str(patch["start_time"]))
            )
        except bookings.NoCapacity as exc:
            raise HTTPException(409, str(exc)) from exc
        return _booking_json(business, moved)

    raise HTTPException(400, "Nothing in that change is editable.")


@router.post("/bookings/{booking_id}/decide")
def decide(
    booking_id: str,
    body: dict[str, Any] = Body(default={}),
    business: Business = Depends(current_business),
) -> dict[str, Any]:
    """Accept or decline a waitlist request, and say what the guest was told."""
    accepted = bool(body.get("accept"))
    channel = str(body.get("channel") or "").strip() or None

    try:
        result = (
            bookings.accept(business, booking_id, channel=channel)
            if accepted
            else bookings.decline(business, booking_id, channel=channel)
        )
    except bookings.BookingError as exc:
        raise HTTPException(409, str(exc)) from exc

    return {
        "booking": _booking_json(business, result),
        "accepted": accepted,
        "told": _told_sentence(channel or _default_channel(business, accepted)),
    }


@router.post("/bookings")
def create_booking(
    body: dict[str, Any] = Body(...),
    business: Business = Depends(current_business),
) -> dict[str, Any]:
    """Take a booking by hand. Contends for capacity exactly like the agent does."""
    try:
        start = datetime.fromisoformat(str(body["start_time"]))
        units = int(body["party_size"])
        name = str(body["name"]).strip()
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, "A booking needs a start time, a size and a name.") from exc

    try:
        created = bookings.create_direct(
            business,
            start=start,
            units=units,
            name=name,
            phone=str(body.get("phone", "")),
            notes=str(body.get("notes", "")),
            allow_overflow=bool(body.get("allow_overflow")),
        )
    except bookings.NoCapacity as exc:
        raise HTTPException(409, str(exc)) from exc
    return _booking_json(business, created)


# --- calendar ----------------------------------------------------------------


@router.get("/calendar")
def calendar_month(
    business: Business = Depends(current_business),
    y: int = Query(..., ge=1970, le=2200),
    m: int = Query(..., ge=1, le=12),
) -> dict[str, Any]:
    return availability.month_view(business, y, m)


@router.get("/calendar/{day}")
def calendar_day(day: str, business: Business = Depends(current_business)) -> dict[str, Any]:
    """Slot by slot, with empty slots kept: the gaps are the useful part."""
    try:
        parsed = date.fromisoformat(day)
    except ValueError as exc:
        raise HTTPException(400, "That is not a date.") from exc

    view = availability.day_view(business, parsed)
    view["slot_labels"] = {
        service["starts_at"]: service["label"] for service in view["services"]
    }
    return view


@router.get("/blocked")
def blocked_days(
    business: Business = Depends(current_business),
    days: int = Query(90, ge=1, le=400),
) -> dict[str, Any]:
    """Owner closures and public holidays in one list, as the settings screen shows them."""
    today = datetime.now(business.tz).date()
    last = today + timedelta(days=days)
    overrides = businesses.overrides_between(business.id, today, last)
    country = business.config["locale"]["country"]
    public = (
        holidays.between(country, today, last)
        if business.config["locale"]["observe_public_holidays"]
        else {}
    )

    rows = [
        {
            "date": day.isoformat(),
            "label": formatting.date_long(business, day),
            "why": override.reason or "You closed this day",
            "effect": _override_effect(business, override),
            "kind": "override",
            "type": override.type,
        }
        for day, override in sorted(overrides.items())
    ]
    rows += [
        {
            "date": day.isoformat(),
            "label": formatting.date_long(business, day),
            "why": f"{name} — a public holiday where you are",
            "effect": "Closed",
            "kind": "holiday",
            "type": "closed",
        }
        for day, name in sorted(public.items())
        if day not in overrides
    ]
    closed_weekdays = [wd for wd in range(7) if not business.rules_for(wd)]
    return {
        "rows": sorted(rows, key=lambda r: r["date"]),
        "closed_weekdays": closed_weekdays,
        "holidays_supported": holidays.supported(country),
        "country": country,
    }


@router.put("/blocked/{day}")
def set_blocked(
    day: str,
    body: dict[str, Any] = Body(default={}),
    business: Business = Depends(current_business),
) -> dict[str, Any]:
    """Close a day, reduce it, or open one the holiday calendar closed."""
    try:
        parsed = date.fromisoformat(day)
    except ValueError as exc:
        raise HTTPException(400, "That is not a date.") from exc

    kind = str(body.get("type", "closed"))
    if kind == "open":
        # "Open anyway" on a public holiday: an explicit override beats the
        # holiday calendar, which is only ever a default.
        businesses.set_override(
            business.id, parsed, type="reduced", total_units=None,
            reason=str(body.get("reason", "Open as usual")),
        )
        return {"ok": True, "date": day, "type": "open"}

    if kind not in ("closed", "reduced", "extended"):
        raise HTTPException(400, "A blocked day is closed, reduced or extended.")

    businesses.set_override(
        business.id,
        parsed,
        type=kind,
        total_units=body.get("total_units"),
        reason=str(body.get("reason", "")),
    )
    return {"ok": True, "date": day, "type": kind}


@router.delete("/blocked/{day}")
def clear_blocked(day: str, business: Business = Depends(current_business)) -> dict[str, Any]:
    businesses.clear_override(business.id, date.fromisoformat(day))
    return {"ok": True}


# --- calls -------------------------------------------------------------------


@router.get("/calls")
def list_calls(
    business: Business = Depends(current_business),
    range: str = Query("today"),
) -> dict[str, Any]:
    """Every inbound call with its outcome, plus the ones that fell short."""
    first = _call_range_start(business, range)
    with readonly() as conn:
        rows = fetch_all(
            conn,
            "SELECT c.id, c.caller_phone, c.started_at, c.duration_s, c.outcome,"
            "       c.resolved, c.recording_url, b.reference"
            "  FROM calls c LEFT JOIN bookings b ON b.id = c.booking_id"
            " WHERE c.business_id = :b AND c.started_at >= :first"
            " ORDER BY c.started_at DESC LIMIT 500",
            b=str(business.id),
            first=first,
        )

    calls = [
        {
            "id": str(r.id),
            "time": (
                formatting.time_str(business, r.started_at)
                if range == "today"
                else formatting.date_short(business, r.started_at)
            ),
            "started_at": r.started_at.isoformat(),
            "caller": r.caller_phone or "Withheld",
            "duration": _duration(r.duration_s),
            "outcome": r.outcome,
            "needs_attention": not r.resolved,
            "reference": r.reference or "—",
            "has_recording": bool(r.recording_url),
        }
        for r in rows
    ]
    return {
        "range": range,
        "calls": calls,
        "fell_short": [c for c in calls if c["needs_attention"]],
    }


@router.get("/calls/{call_id}/transcript")
def call_transcript(
    call_id: str, business: Business = Depends(current_business)
) -> dict[str, Any]:
    """Turn by turn, with tool calls inline where the agent checked and booked."""
    with readonly() as conn:
        row = fetch_one(
            conn,
            "SELECT id, transcript, recording_url, outcome, booking_id FROM calls"
            " WHERE id = :i AND business_id = :b",
            i=call_id,
            b=str(business.id),
        )
    if row is None:
        raise HTTPException(404, "No such call.")
    return {
        "id": str(row.id),
        "turns": row.transcript or [],
        "recording_url": row.recording_url,
        "outcome": row.outcome,
        "booking_id": str(row.booking_id) if row.booking_id else None,
    }


# --- guests ------------------------------------------------------------------


@router.get("/guests")
def list_guests(
    business: Business = Depends(current_business),
    filter: str = Query("all"),
) -> dict[str, Any]:
    clauses = {
        "all": "",
        "regulars": " AND visit_count >= 4",
        "noshows": " AND no_show_count > 0",
        "dnc": " AND do_not_call",
    }
    if filter not in clauses:
        raise HTTPException(400, "Unknown guest filter.")

    with readonly() as conn:
        rows = fetch_all(
            conn,
            "SELECT c.id, c.name, c.phone_e164, c.visit_count, c.no_show_count,"
            "       c.do_not_call, c.notes,"
            "       (SELECT max(start_time) FROM bookings b WHERE b.customer_id = c.id"
            "          AND b.status IN ('arrived', 'confirmed')) AS last_seen"
            "  FROM customers c WHERE c.business_id = :b" + clauses[filter] +
            " ORDER BY c.visit_count DESC, c.name LIMIT 1000",
            b=str(business.id),
        )

    return {
        "filter": filter,
        "guests": [
            {
                "id": str(r.id),
                "name": r.name or "Unknown",
                "phone": r.phone_e164,
                "visits": r.visit_count,
                "no_shows": r.no_show_count,
                "do_not_call": r.do_not_call,
                "note": r.notes,
                "last": formatting.date_short(business, r.last_seen) if r.last_seen else "—",
            }
            for r in rows
        ],
    }


@router.patch("/guests/{guest_id}")
def update_guest(
    guest_id: str,
    patch: dict[str, Any] = Body(...),
    business: Business = Depends(current_business),
) -> dict[str, Any]:
    fields = {k: v for k, v in patch.items() if k in ("do_not_call", "notes", "name")}
    if not fields:
        raise HTTPException(400, "Nothing in that change is editable.")

    assignments = ", ".join(f"{k} = :{k}" for k in fields)
    with transaction() as conn:
        result = conn.execute(
            text(
                f"UPDATE customers SET {assignments}, updated_at = now()"
                " WHERE id = :i AND business_id = :b"
            ),
            {**fields, "i": guest_id, "b": str(business.id)},
        )
    if result.rowcount == 0:
        raise HTTPException(404, "No such guest.")
    return {"ok": True}


# --- menu --------------------------------------------------------------------


@router.get("/menu")
def read_menu(business: Business = Depends(current_business)) -> dict[str, Any]:
    with readonly() as conn:
        rows = fetch_all(
            conn,
            "SELECT id, name, section, price, description, tags, available, position"
            "  FROM menu_items WHERE business_id = :b ORDER BY position, name",
            b=str(business.id),
        )
    return {
        "currency_symbol": business.config["locale"]["currency_symbol"],
        "items": [
            {
                "id": str(r.id),
                "name": r.name,
                "section": r.section,
                "price": float(r.price) if r.price is not None else None,
                "price_display": formatting.money(business, r.price),
                "description": r.description,
                "tags": list(r.tags or []),
                "available": r.available,
            }
            for r in rows
        ],
    }


@router.post("/menu")
def add_menu_item(
    body: dict[str, Any] = Body(...),
    business: Business = Depends(current_business),
) -> dict[str, Any]:
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "A dish needs a name.")

    with transaction() as conn:
        row = fetch_one(
            conn,
            "INSERT INTO menu_items (business_id, name, section, price, description, tags)"
            " VALUES (:b, :name, :section, :price, :description, :tags) RETURNING id",
            b=str(business.id),
            name=name,
            section=str(body.get("section", "")),
            price=body.get("price"),
            description=str(body.get("description", "")),
            tags=list(body.get("tags") or []),
        )
    return {"ok": True, "id": str(row.id)}


@router.patch("/menu/{item_id}")
def update_menu_item(
    item_id: str,
    patch: dict[str, Any] = Body(...),
    business: Business = Depends(current_business),
) -> dict[str, Any]:
    fields = {
        k: v for k, v in patch.items()
        if k in ("name", "section", "price", "description", "available", "tags")
    }
    if not fields:
        raise HTTPException(400, "Nothing in that change is editable.")

    assignments = ", ".join(f"{k} = :{k}" for k in fields)
    with transaction() as conn:
        result = conn.execute(
            text(
                f"UPDATE menu_items SET {assignments}, updated_at = now()"
                " WHERE id = :i AND business_id = :b"
            ),
            {**fields, "i": item_id, "b": str(business.id)},
        )
    if result.rowcount == 0:
        raise HTTPException(404, "No such dish.")
    return {"ok": True}


@router.delete("/menu/{item_id}")
def delete_menu_item(
    item_id: str, business: Business = Depends(current_business)
) -> dict[str, Any]:
    with transaction() as conn:
        conn.execute(
            text("DELETE FROM menu_items WHERE id = :i AND business_id = :b"),
            {"i": item_id, "b": str(business.id)},
        )
    return {"ok": True}


# --- settings ----------------------------------------------------------------


@router.get("/settings")
def read_settings(business: Business = Depends(current_business)) -> dict[str, Any]:
    return {
        "config": business.config,
        "stored": business.stored_config,
        "sections": list(business_config.SECTIONS),
        "capacity_rules": [
            {
                "weekday": rule.weekday,
                "start_time": rule.start_time.isoformat(timespec="minutes"),
                "end_time": rule.end_time.isoformat(timespec="minutes"),
                "total_units": rule.total_units,
                "slot_minutes": rule.slot_minutes,
                "turn_minutes": rule.turn_minutes,
                "label": rule.label,
            }
            for rule in business.rules
        ],
        "changes": _recent_changes(business),
    }


@router.put("/settings/{section}")
def save_settings(
    section: str,
    values: dict[str, Any] = Body(...),
    business: Business = Depends(current_business),
) -> dict[str, Any]:
    """Validate, save, and say when -- the UI stamps the time it is given.

    Validation failures come back as 422 with every problem named, because the
    save bar shows one message and a per-round-trip error list is a chore.
    """
    if section == "hours":
        return _save_hours(business, values)

    try:
        updated = businesses.save_config(business.id, section, values, actor="dashboard")
    except business_config.ConfigError as exc:
        raise HTTPException(422, str(exc)) from exc

    return {
        "ok": True,
        "section": section,
        "config": updated.config,
        "saved_at": datetime.now(UTC).isoformat(),
    }


@router.get("/stats")
def stats(business: Business = Depends(current_business)) -> dict[str, Any]:
    """What the agent has done this month. Counted, never estimated."""
    today = datetime.now(business.tz).date()
    first = datetime.combine(
        today.replace(day=1), datetime.min.time(), tzinfo=business.tz
    ).astimezone(UTC)

    with readonly() as conn:
        row = fetch_one(
            conn,
            """
            SELECT
              (SELECT count(*) FROM calls
                WHERE business_id = :b AND started_at >= :first)          AS calls,
              (SELECT count(*) FROM bookings
                WHERE business_id = :b AND created_at >= :first
                  AND status <> 'declined')                                AS taken,
              (SELECT COALESCE(sum(party_size), 0) FROM bookings
                WHERE business_id = :b AND start_time >= :first
                  AND status = 'arrived')                                  AS seated,
              (SELECT count(*) FROM messages
                WHERE business_id = :b AND created_at >= :first
                  AND status IN ('sent', 'delivered'))                     AS messages
            """,
            b=str(business.id),
            first=first,
        )

    return {
        "since": first.astimezone(business.tz).date().isoformat(),
        "rows": [
            {"label": "Calls answered", "value": row.calls},
            {"label": "Bookings taken", "value": row.taken},
            {"label": f"{business.unit_plural.capitalize()} seated from them", "value": row.seated},
            {"label": "Texts sent", "value": row.messages},
        ],
    }


@router.get("/locales")
def locales() -> dict[str, Any]:
    """Countries, currencies, timezones and languages, from the server.

    Server-side so a country added here reaches every dashboard without
    shipping a new page (PRD §15b).
    """
    import json
    from importlib import resources

    raw = resources.files("calling_agent").joinpath("data/locales.json").read_text("utf-8")
    data = json.loads(raw)
    data.pop("_comment", None)
    return data


@router.get("/holidays")
def holiday_list(
    business: Business = Depends(current_business),
    year: int = Query(None),
) -> dict[str, Any]:
    country = business.config["locale"]["country"]
    target = year or datetime.now(business.tz).year
    found = holidays.between(country, date(target, 1, 1), date(target, 12, 31))
    return {
        "country": country,
        "year": target,
        "supported": holidays.supported(country),
        "holidays": [
            {"date": day.isoformat(), "name": name} for day, name in sorted(found.items())
        ],
    }


# --- helpers -----------------------------------------------------------------


def _range_for(business: Business, key: str) -> tuple[datetime, datetime, str]:
    """Turn a tab name into a real window in the venue's own time."""
    today = datetime.now(business.tz).date()

    if key == "tomorrow":
        return _service_window(business, today + timedelta(days=1)) + ("Tomorrow",)
    if key == "week":
        first = datetime.combine(today, datetime.min.time(), tzinfo=business.tz).astimezone(UTC)
        return first, first + timedelta(days=7), "This week"
    if key != "tonight":
        try:
            chosen = date.fromisoformat(key)
        except ValueError as exc:
            raise HTTPException(400, "Unknown day.") from exc
        return _service_window(business, chosen) + (formatting.date_long(business, chosen),)

    return _service_window(business, today) + ("Tonight",)


def _last_seating(business: Business, first: datetime) -> str:
    """The latest start whose whole turn still finishes before closing.

    Read from the rules rather than written in the page, because it moves with
    the turn time and with any override on the day.
    """
    local_day = first.astimezone(business.tz).date()
    latest = None
    for service in availability.services_on(business, local_day):
        bookable = service.bookable_starts()
        if bookable:
            latest = max(latest, bookable[-1]) if latest else bookable[-1]
    return formatting.time_str(business, latest) if latest else ""


def _service_window(business: Business, day: date) -> tuple[datetime, datetime]:
    services = availability.services_on(business, day)
    if services:
        return min(s.starts_at for s in services), max(s.ends_at for s in services)
    first = datetime.combine(day, datetime.min.time(), tzinfo=business.tz).astimezone(UTC)
    return first, first + timedelta(days=1)


def _block_key(business: Business, moment: datetime, date_key: str) -> str:
    """Rows group under a time header -- or a day header on the week tab."""
    if date_key == "week":
        return (
            f"{moment.astimezone(business.tz).strftime('%a')} "
            f"{formatting.time_str(business, moment)}"
        )
    return formatting.time_str(business, moment)


def _booking_json(
    business: Business, booking: bookings.BookingRow, date_key: str = "tonight"
) -> dict[str, Any]:
    data = booking.as_dict(business)
    data["block"] = _block_key(business, booking.start_time, date_key)
    data["meta"] = _meta_line(business, booking)
    return data


def _meta_line(business: Business, booking: bookings.BookingRow) -> str:
    """The small grey line under a name. Notes first: an allergy outranks a count."""
    if booking.notes:
        return booking.notes
    if booking.status == bookings.ARRIVED:
        return "Arrived"
    if booking.status == bookings.NO_SHOW:
        return "Marked as a no-show. The seats are back on sale."
    if booking.no_shows:
        return f"No-showed {booking.no_shows} time{'s' if booking.no_shows > 1 else ''}"
    if booking.visits >= 4:
        return f"Regular — {booking.visits} visits"
    return ""


def _apply_status(business: Business, booking_id: str, status: str) -> dict[str, Any]:
    moves = {
        "arrived": bookings.mark_arrived,
        "seated": bookings.mark_arrived,
        "no_show": bookings.mark_no_show,
        "gone": bookings.mark_no_show,
        "confirmed": bookings.undo,
        "due": bookings.undo,
        "cancelled": bookings.cancel,
    }
    move = moves.get(status)
    if move is None:
        raise HTTPException(400, f"Unknown status {status!r}.")

    try:
        return _booking_json(business, move(business, booking_id))
    except bookings.IllegalTransition as exc:
        raise HTTPException(409, str(exc)) from exc
    except bookings.BookingNotFound as exc:
        raise HTTPException(404, "No such booking.") from exc


def _why_pending(business: Business, booking: bookings.BookingRow, policy: dict) -> str:
    if booking.party_size > policy["auto_approve_max_party"]:
        return (
            f"{business.units(booking.party_size)}, above your limit of "
            f"{policy['auto_approve_max_party']}."
        )
    verdict = availability.is_available(
        business, booking.start_time, booking.party_size, with_alternatives=False
    )
    if verdict.reason == availability.FULL:
        return "That time is full, so they asked to go on the list."
    if verdict.reason == availability.TOO_SOON:
        return f"Booked inside your {policy['min_lead_minutes']}-minute notice."
    return "Needs your decision."


def _told_sentence(channel: str) -> str:
    return {
        "text": "A text has gone out.",
        "call": "The agent is ringing them now.",
        "none": "Nobody has been told — that's on you.",
    }.get(channel, "A text has gone out.")


def _default_channel(business: Business, accepted: bool) -> str:
    telling = business.config["telling_guest"]
    return telling["on_accept"] if accepted else telling["on_decline"]


def _override_effect(business: Business, override: businesses.Override) -> str:
    if override.type == "closed":
        return "Closed"
    if override.type == "reduced" and override.total_units is not None:
        return f"{override.total_units} {business.unit_plural}"
    if override.type == "extended":
        return "Extended"
    return "Open as usual"


def _call_range_start(business: Business, key: str) -> datetime:
    now = datetime.now(UTC)
    if key == "today":
        today = now.astimezone(business.tz).date()
        return datetime.combine(today, datetime.min.time(), tzinfo=business.tz).astimezone(UTC)
    try:
        return now - timedelta(days=int(key))
    except ValueError as exc:
        raise HTTPException(400, "Unknown range.") from exc


def _duration(seconds: int | None) -> str:
    if not seconds:
        return "—"
    return f"{seconds // 60}m {seconds % 60:02d}s"


def _save_hours(business: Business, values: dict[str, Any]) -> dict[str, Any]:
    """Opening hours are capacity_rules, not config: a week replaced as a unit."""
    rules = values.get("rules")
    if not isinstance(rules, list):
        raise HTTPException(400, "Hours are a list of rules.")
    businesses.set_capacity_rules(business.id, rules)
    return {"ok": True, "section": "hours", "saved_at": datetime.now(UTC).isoformat()}


def _recent_changes(business: Business) -> list[dict[str, Any]]:
    with readonly() as conn:
        rows = fetch_all(
            conn,
            "SELECT section, summary, actor, created_at FROM config_changes"
            " WHERE business_id = :b ORDER BY created_at DESC LIMIT 20",
            b=str(business.id),
        )
    return [
        {
            "when": formatting.date_short(business, r.created_at)
            + ", "
            + formatting.time_str(business, r.created_at),
            "what": r.summary,
            "who": r.actor or "You",
        }
        for r in rows
    ]
