"""Tenancy: who is this call for, and what are their rules.

Every query in this product carries a business_id. It is resolved ONCE, at the
edge -- from the dialled number for a phone call, from the dashboard token for
a dashboard request -- and passed down. Nothing below the edge ever infers a
tenant from anything a caller said, because a caller can say anything.

Config is read fresh per call (PRD §15) with a short TTL cache in front. The
TTL is the honest cost of "the agent is using it from the next call": a save
invalidates immediately in this process, and a sibling process picks it up
within the TTL rather than on restart.
"""

from __future__ import annotations

import logging
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Connection
from sqlalchemy import text as _text

from . import business_config as bc
from .db import fetch_all, fetch_one, readonly, transaction

log = logging.getLogger(__name__)

#: How long a resolved business may be reused before it is read again.
CONFIG_TTL_SECONDS = 20


class UnknownBusiness(LookupError):
    """No tenant for that number, slug or id. Never guess one."""


@dataclass(frozen=True)
class CapacityRule:
    weekday: int
    start_time: time
    end_time: time
    total_units: int
    slot_minutes: int
    turn_minutes: int
    label: str = ""


@dataclass(frozen=True)
class Business:
    id: UUID
    slug: str
    name: str
    timezone: str
    phone_number: str | None
    vertical: str
    status: str
    #: DEFAULTS <- vertical <- stored. What the product actually runs on.
    config: dict[str, Any]
    #: Exactly what is stored, for the dashboard to edit without inheriting
    #: every default into the venue's own document on the first save.
    stored_config: dict[str, Any]
    rules: tuple[CapacityRule, ...] = ()
    loaded_at: float = field(default_factory=_time.monotonic, compare=False)

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            # A bad timezone must not take the phone down. UTC renders wrong;
            # refusing the call renders nothing.
            log.warning("business %s has unusable timezone %r; using UTC", self.slug, self.timezone)
            return ZoneInfo("UTC")

    @property
    def unit_plural(self) -> str:
        return self.config["capacity"]["unit_label_plural"]

    @property
    def unit_singular(self) -> str:
        return self.config["capacity"]["unit_label_singular"]

    def units(self, n: int) -> str:
        return f"{n} {self.unit_singular if n == 1 else self.unit_plural}"

    def rules_for(self, weekday: int) -> list[CapacityRule]:
        return [r for r in self.rules if r.weekday == weekday]

    @property
    def tracks_capacity(self) -> bool:
        """PRD §15d: no rules, or capacity switched off, means take everything."""
        return bool(self.config["capacity"]["enabled"]) and bool(self.rules)


# --- cache -------------------------------------------------------------------

_cache: dict[str, Business] = {}
_cache_lock = threading.Lock()


def _cache_put(business: Business) -> Business:
    with _cache_lock:
        _cache[f"id:{business.id}"] = business
        _cache[f"slug:{business.slug}"] = business
        if business.phone_number:
            _cache[f"phone:{business.phone_number}"] = business
    return business


def _cache_get(key: str) -> Business | None:
    with _cache_lock:
        found = _cache.get(key)
    if found is None:
        return None
    if _time.monotonic() - found.loaded_at > CONFIG_TTL_SECONDS:
        return None
    return found


def invalidate(business_id: UUID | str | None = None) -> None:
    """Drop cached config. Called on every write that changes behaviour."""
    with _cache_lock:
        if business_id is None:
            _cache.clear()
            return
        stale = [k for k, v in _cache.items() if str(v.id) == str(business_id)]
        for key in stale:
            _cache.pop(key, None)


# --- loading -----------------------------------------------------------------

_SELECT = """
SELECT id, slug, name, timezone, phone_number, vertical, status, config
  FROM businesses
"""


def _hydrate(conn: Connection, row: Any) -> Business:
    stored = row.config or {}
    rules = tuple(
        CapacityRule(
            weekday=r.weekday,
            start_time=r.start_time,
            end_time=r.end_time,
            total_units=r.total_units,
            slot_minutes=r.slot_minutes,
            turn_minutes=r.turn_minutes,
            label=r.label or "",
        )
        for r in fetch_all(
            conn,
            "SELECT weekday, start_time, end_time, total_units, slot_minutes,"
            " turn_minutes, label FROM capacity_rules WHERE business_id = :b"
            " ORDER BY weekday, start_time",
            b=row.id,
        )
    )
    return Business(
        id=row.id,
        slug=row.slug,
        name=row.name,
        timezone=row.timezone,
        phone_number=row.phone_number,
        vertical=row.vertical,
        status=row.status,
        config=bc.effective(stored, row.vertical),
        stored_config=stored,
        rules=rules,
    )


def _load(where: str, key: str, **params: Any) -> Business:
    cached = _cache_get(key)
    if cached is not None:
        return cached
    with readonly() as conn:
        row = fetch_one(conn, _SELECT + where, **params)
        if row is None:
            raise UnknownBusiness(key)
        return _cache_put(_hydrate(conn, row))


def by_id(business_id: UUID | str) -> Business:
    return _load(" WHERE id = :v", f"id:{business_id}", v=str(business_id))


def by_slug(slug: str) -> Business:
    return _load(" WHERE slug = :v", f"slug:{slug}", v=slug)


def by_phone(phone_e164: str) -> Business:
    """The dialled number is the tenant key for an inbound call (PRD §17)."""
    return _load(" WHERE phone_number = :v", f"phone:{phone_e164}", v=phone_e164)


def resolve(
    *,
    business_id: str | UUID | None = None,
    slug: str | None = None,
    dialled_number: str | None = None,
) -> Business:
    """Resolve a tenant from whatever the edge knows, most specific first.

    Raises rather than falling back to "the first business in the table": on a
    shared line, guessing hands one venue's book to another venue's caller.
    """
    if business_id:
        return by_id(business_id)
    if dialled_number:
        return by_phone(dialled_number)
    if slug:
        return by_slug(slug)
    raise UnknownBusiness("no business identifier given")


def exists(slug: str) -> bool:
    try:
        by_slug(slug)
    except UnknownBusiness:
        return False
    return True


# --- writes ------------------------------------------------------------------


def create(
    *,
    slug: str,
    name: str,
    timezone: str = "UTC",
    vertical: str = "restaurant",
    phone_number: str | None = None,
    config: dict[str, Any] | None = None,
) -> Business:
    """Onboard a tenant. Config is validated before a row exists."""
    document = config or {}
    bc.validate(document)
    with transaction() as conn:
        row = fetch_one(
            conn,
            """
            INSERT INTO businesses (slug, name, timezone, vertical, phone_number,
                                    config, config_version)
            VALUES (:slug, :name, :tz, :vertical, :phone, CAST(:config AS jsonb), :v)
            RETURNING id
            """,
            slug=slug,
            name=name,
            tz=timezone,
            vertical=vertical,
            phone=phone_number,
            config=_json(document),
            v=bc.CONFIG_VERSION,
        )
        assert row is not None
    invalidate()
    return by_id(row.id)


def save_config(
    business_id: UUID | str,
    section: str,
    values: dict[str, Any],
    *,
    actor: str = "",
) -> Business:
    """Merge one section into the stored document, validating the whole thing.

    The WHOLE document, not the section: `auto_approve_max_party` alone is
    always fine and is nonsense above `max_party_size`. Cross-field rules only
    exist against the merged result.
    """
    if section not in bc.SECTIONS:
        raise bc.ConfigError(f"unknown settings section {section!r}")

    with transaction() as conn:
        row = fetch_one(
            conn, "SELECT config FROM businesses WHERE id = :b FOR UPDATE", b=str(business_id)
        )
        if row is None:
            raise UnknownBusiness(str(business_id))
        stored = row.config or {}
        before = stored.get(section)
        merged = bc.deep_merge(stored, {section: values})
        bc.validate(merged)

        conn.execute(
            _text(
                "UPDATE businesses SET config = CAST(:c AS jsonb), config_version = :v,"
                " updated_at = now() WHERE id = :b"
            ),
            {"c": _json(merged), "v": bc.CONFIG_VERSION, "b": str(business_id)},
        )
        conn.execute(
            _text(
                "INSERT INTO config_changes (business_id, section, summary, actor,"
                " before, after) VALUES (:b, :s, :summary, :actor,"
                " CAST(:before AS jsonb), CAST(:after AS jsonb))"
            ),
            {
                "b": str(business_id),
                "s": section,
                "summary": _summarise(section, before or {}, merged.get(section) or {}),
                "actor": actor,
                "before": _json(before),
                "after": _json(merged.get(section)),
            },
        )
    invalidate(business_id)
    return by_id(business_id)


def set_capacity_rules(business_id: UUID | str, rules: list[dict[str, Any]]) -> Business:
    """Replace the whole weekly shape in one transaction.

    Replaced rather than patched because the dashboard edits a week as a unit,
    and a half-applied week is a venue that is open on Tuesday and unreachable
    on Wednesday.
    """
    with transaction() as conn:
        conn.execute(_text("DELETE FROM capacity_rules WHERE business_id = :b"),
                     {"b": str(business_id)})
        for rule in rules:
            conn.execute(
                _text(
                    "INSERT INTO capacity_rules (business_id, weekday, start_time, end_time,"
                    " total_units, slot_minutes, turn_minutes, label)"
                    " VALUES (:b, :wd, :start, :end, :total, :slot, :turn, :label)"
                ),
                {
                    "b": str(business_id),
                    "wd": int(rule["weekday"]),
                    "start": rule["start_time"],
                    "end": rule["end_time"],
                    "total": int(rule["total_units"]),
                    "slot": int(rule.get("slot_minutes") or 30),
                    "turn": int(rule.get("turn_minutes") or 90),
                    "label": rule.get("label") or "",
                },
            )
    invalidate(business_id)
    return by_id(business_id)


def set_phone_number(business_id: UUID | str, phone_e164: str | None) -> Business:
    with transaction() as conn:
        conn.execute(
            _text("UPDATE businesses SET phone_number = :p, updated_at = now() WHERE id = :b"),
            {"p": phone_e164, "b": str(business_id)},
        )
    invalidate()
    return by_id(business_id)


# --- overrides ---------------------------------------------------------------


@dataclass(frozen=True)
class Override:
    date: date
    type: str
    total_units: int | None
    start_time: time | None
    end_time: time | None
    reason: str


def overrides_between(business_id: UUID | str, first: date, last: date) -> dict[date, Override]:
    with readonly() as conn:
        rows = fetch_all(
            conn,
            "SELECT date, type, total_units, start_time, end_time, reason FROM overrides"
            " WHERE business_id = :b AND date BETWEEN :first AND :last",
            b=str(business_id),
            first=first,
            last=last,
        )
    return {
        r.date: Override(r.date, r.type, r.total_units, r.start_time, r.end_time, r.reason or "")
        for r in rows
    }


def override_on(business_id: UUID | str, day: date) -> Override | None:
    return overrides_between(business_id, day, day).get(day)


def set_override(
    business_id: UUID | str,
    day: date,
    *,
    type: str,
    total_units: int | None = None,
    start_time: time | None = None,
    end_time: time | None = None,
    reason: str = "",
) -> None:
    with transaction() as conn:
        conn.execute(
            _text(
                "INSERT INTO overrides (business_id, date, type, total_units, start_time,"
                " end_time, reason) VALUES (:b, :d, :t, :u, :s, :e, :r)"
                " ON CONFLICT (business_id, date) DO UPDATE SET type = EXCLUDED.type,"
                " total_units = EXCLUDED.total_units, start_time = EXCLUDED.start_time,"
                " end_time = EXCLUDED.end_time, reason = EXCLUDED.reason"
            ),
            {
                "b": str(business_id),
                "d": day,
                "t": type,
                "u": total_units,
                "s": start_time,
                "e": end_time,
                "r": reason,
            },
        )


def clear_override(business_id: UUID | str, day: date) -> None:
    with transaction() as conn:
        conn.execute(
            _text("DELETE FROM overrides WHERE business_id = :b AND date = :d"),
            {"b": str(business_id), "d": day},
        )


# --- booking window ----------------------------------------------------------


def window_last_day(business: Business, today: date) -> date:
    """The last date the agent may book (PRD §15c).

    Rolling is today + (days - 1): "30 days open at any time" means today
    counts as one of them, which is what the dashboard's preview sentence says.
    """
    window = business.config["booking_window"]
    if window["mode"] == "fixed" and window.get("until"):
        return date.fromisoformat(window["until"])
    return today + timedelta(days=max(1, int(window["days"])) - 1)


# --- helpers -----------------------------------------------------------------


def _summarise(section: str, before: Any, after: Any) -> str:
    """A line a venue owner can read in "What changed"."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return f"{section} updated"
    changed = [k for k in set(before) | set(after) if before.get(k) != after.get(k)]
    if not changed:
        return f"{section}: saved with no changes"
    parts = [f"{k} {before.get(k)!r} → {after.get(k)!r}" for k in sorted(changed)[:3]]
    if len(changed) > 3:
        parts.append(f"and {len(changed) - 3} more")
    return f"{section}: " + ", ".join(parts)


def _json(value: Any) -> str:
    import json

    return json.dumps(value if value is not None else None)
