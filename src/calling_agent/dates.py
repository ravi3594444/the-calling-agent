"""Turning what a caller said into a date, a time, or a phone number.

This module is a BOUNDARY. Three third-party libraries do the actual work --
parsedatetime, dateparser and phonenumbers -- and the rest of the codebase
never imports any of them. If one is swapped out, this file changes and
nothing else does.

Nothing here is hand-rolled calendar arithmetic. "Next Monday" crossing a year
boundary, a leap day, a timezone that shifted its offset last week: these are
solved problems, and solving them again by hand is how you get a booking on a
day that does not exist.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from functools import lru_cache

import dateparser
import langcodes
import parsedatetime
import phonenumbers

log = logging.getLogger(__name__)

_calendar = parsedatetime.Calendar()

#: parsedatetime returns a status flag: 0 means it understood nothing.
_PARSED_NOTHING = 0


def resolve_date(phrase: str, *, today: date, languages: tuple[str, ...] = ()) -> date | None:
    """"next Monday", "tomorrow", "21 September" -> a real date, or None.

    `today` is the business's local date, not the server's. A venue in Mumbai
    and a server in Iowa disagree about what "tomorrow" means for six hours
    every day, and the caller means the venue's.

    Tried in order: the relative-phrase parser, then the multilingual one.
    The first is far better at "this Friday"; the second is the only one that
    understands a caller who is not speaking English.
    """
    phrase = (phrase or "").strip()
    if not phrase:
        return None

    base = datetime.combine(today, time(12, 0))

    parsed, status = _calendar.parseDT(phrase, sourceTime=base)
    if status != _PARSED_NOTHING:
        return parsed.date()

    fallback = dateparser.parse(
        phrase,
        languages=iso_codes(languages) or None,
        settings={"RELATIVE_BASE": base, "PREFER_DATES_FROM": "future"},
    )
    if fallback is not None:
        return fallback.date()

    log.debug("could not resolve date phrase %r", phrase)
    return None


@lru_cache(maxsize=256)
def iso_code(language_name: str) -> str | None:
    """"Hindi" -> "hi". None for anything not recognised.

    The config stores display names, because that is what a venue owner picks
    from a list; parsers want ISO codes. Translating here rather than storing
    codes keeps the dashboard readable and the parsers correct -- and an
    unrecognised name is dropped rather than raised, because a language nobody
    can parse must not stop the agent understanding "tomorrow".
    """
    try:
        return langcodes.find(language_name).language
    except LookupError:
        log.debug("no ISO code for language %r", language_name)
        return None


def iso_codes(language_names: tuple[str, ...] | list[str]) -> list[str]:
    return [code for name in language_names if (code := iso_code(name))]


def resolve_time(phrase: str) -> time | None:
    """"half seven", "7pm", "19:30" -> a time, or None."""
    phrase = (phrase or "").strip()
    if not phrase:
        return None

    parsed = dateparser.parse(phrase)
    if parsed is not None:
        return parsed.time()

    parsed_dt, status = _calendar.parseDT(phrase)
    if status != _PARSED_NOTHING:
        return parsed_dt.time()

    return None


def parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def parse_iso_time(value: str) -> time | None:
    """Accepts "19:30" and "19:30:00". Anything else is a caller's typo."""
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return time.fromisoformat(raw)
    except ValueError:
        return None


def normalise_phone(raw: str, *, region: str) -> str:
    """To E.164, using the venue's country to read a local number.

    Guests say "98765 43210", not "+91 98765 43210". Without a region the
    number is unparseable; with the wrong one it is somebody else's number, so
    the region comes from the business's locale rather than a default.

    An unparseable number is returned trimmed rather than dropped: a booking
    with a number we cannot format is still better than a booking with no way
    to reach anybody.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        parsed = phonenumbers.parse(raw, region or None)
    except phonenumbers.NumberParseException:
        return raw
    if not phonenumbers.is_valid_number(parsed):
        return raw
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def looks_like_phone(value: str) -> bool:
    """Enough digits to be a number rather than a booking reference."""
    return sum(character.isdigit() for character in value or "") >= 7
