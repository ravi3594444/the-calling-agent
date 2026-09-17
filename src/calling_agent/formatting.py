"""Rendering times, dates and money the way one business writes them.

All timestamps are UTC in the database; `businesses.timezone` and the locale
block decide what a human sees. Every screen and every message goes through
here, so a venue that switched to a 24-hour clock does not get it in the
dashboard and lose it in the text message.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from .businesses import Business

_ORDINAL_SUFFIX = {1: "st", 2: "nd", 3: "rd", 21: "st", 22: "nd", 23: "rd", 31: "st"}


def local(business: Business, moment: datetime) -> datetime:
    return moment.astimezone(business.tz)


def clock(business: Business) -> str:
    return str(business.config["locale"]["clock"])


def time_str(business: Business, moment: datetime) -> str:
    """"7:00 PM" or "19:00", whichever this venue reads."""
    at = local(business, moment)
    if clock(business) == "24":
        return at.strftime("%H:%M")
    # %-I is not portable; build the 12-hour number rather than strip zeros.
    hour = at.hour % 12 or 12
    return f"{hour}:{at.minute:02d} {'AM' if at.hour < 12 else 'PM'}"


def date_long(business: Business, value: datetime | date) -> str:
    """"Friday 19 September" -- or "Friday, September 19" where that is the order."""
    day = local(business, value).date() if isinstance(value, datetime) else value
    order = business.config["locale"]["date_format"]
    if order == "MDY":
        return f"{day.strftime('%A')}, {day.strftime('%B')} {day.day}"
    if order == "YMD":
        return f"{day.isoformat()} ({day.strftime('%A')})"
    return f"{day.strftime('%A')} {day.day} {day.strftime('%B')}"


def date_short(business: Business, value: datetime | date) -> str:
    day = local(business, value).date() if isinstance(value, datetime) else value
    order = business.config["locale"]["date_format"]
    if order == "MDY":
        return day.strftime("%m/%d")
    if order == "YMD":
        return day.isoformat()
    return f"{day.day} {day.strftime('%b')}"


def when(business: Business, moment: datetime) -> str:
    """A whole instant in one phrase, for a message or to be read aloud."""
    return f"{date_long(business, moment)} at {time_str(business, moment)}"


def spoken_date(business: Business, moment: datetime) -> str:
    """"Friday the nineteenth" reads better down a phone than "Friday 19"."""
    at = local(business, moment)
    day = at.day
    suffix = _ORDINAL_SUFFIX.get(day, "th")
    return f"{at.strftime('%A')} the {day}{suffix}"


def spell(reference: str) -> str:
    """"K7M2QP" -> "K 7 M 2 Q P", so the agent reads it character by character."""
    return " ".join(reference.strip().upper())


def money(business: Business, amount: Decimal | float | int | None) -> str:
    if amount is None:
        return ""
    symbol = business.config["locale"]["currency_symbol"]
    number = Decimal(str(amount)).normalize()
    if number == number.to_integral_value():
        return f"{symbol} {int(number):,}"
    return f"{symbol} {number:,.2f}"


def currency_name(business: Business) -> str:
    return business.config["locale"]["currency_name"]
