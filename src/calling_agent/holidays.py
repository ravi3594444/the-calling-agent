"""Public holidays, from the server (PRD §15b).

Dates shift year to year and lunar holidays -- Eid, Diwali, Lunar New Year --
cannot be hardcoded. A client-side table of "12-25: Christmas" is wrong in
every country that matters to this product, and wrong quietly: the venue takes
bookings on a day nobody is working and finds out on the day.

The `holidays` package computes them per country per year, lunar calendars
included. It is wrapped rather than used directly so that the one thing the
product needs -- "is this date a holiday where this venue is" -- stays a single
call, and an unavailable package degrades to "no holidays" rather than taking
the calendar down.
"""

from __future__ import annotations

import logging
from datetime import date
from functools import lru_cache

log = logging.getLogger(__name__)

try:  # pragma: no cover - exercised by its absence, not its presence
    import holidays as _holidays

    AVAILABLE = True
except Exception:  # noqa: BLE001
    _holidays = None  # type: ignore[assignment]
    AVAILABLE = False
    log.warning("the `holidays` package is not installed: no public holidays will be marked")


@lru_cache(maxsize=512)
def _for_year(country: str, year: int) -> dict[date, str]:
    """Every holiday in one country-year. Cached: the computation is not free.

    An unknown country code is normal, not an error -- the product sells
    everywhere and the package does not cover everywhere. It means "no
    holidays on file", which the dashboard says out loud so an owner knows to
    block those days themselves.
    """
    if not AVAILABLE or not country:
        return {}
    try:
        found = _holidays.country_holidays(country.upper(), years=[year])
    except (NotImplementedError, KeyError, AttributeError):
        return {}
    except Exception:  # noqa: BLE001 - a calendar must never take down a call
        log.exception("holiday lookup failed for %s %s", country, year)
        return {}
    return {day: str(name) for day, name in found.items()}


def holiday_name(country: str, day: date) -> str | None:
    return _for_year(country, day.year).get(day)


def is_holiday(country: str, day: date) -> bool:
    return day in _for_year(country, day.year)


def between(country: str, first: date, last: date) -> dict[date, str]:
    """Holidays in a range, spanning a year boundary correctly."""
    out: dict[date, str] = {}
    for year in range(first.year, last.year + 1):
        for day, name in _for_year(country, year).items():
            if first <= day <= last:
                out[day] = name
    return out


def supported(country: str) -> bool:
    """Does this country have holidays on file at all?

    Asked against a year that certainly exists rather than the current one, so
    the answer does not change on New Year's Eve.
    """
    return bool(_for_year(country, date.today().year))


def count_for_year(country: str, year: int) -> int:
    return len(_for_year(country, year))
