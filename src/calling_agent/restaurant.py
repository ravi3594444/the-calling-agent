"""Backwards compatibility for the in-memory reservation book that used to live here.

PRD §17 replaced it. Bookings are rows in Postgres now, behind the availability
engine and the hold transaction, because the old `BookingStore` was a process-
local dict: on any host with more than one worker, a booking taken by one was
invisible to the next, and the repo said so in its own docstring.

What remains is a shim. Anything that imported `restaurant.check_availability`
and friends still works -- the calls are simply routed to the default business
through the real engine. New code should use `agent_tools` with an explicit
Business, because a module-level "the restaurant" cannot exist in a product
that answers for many of them.
"""

from __future__ import annotations

import warnings
from typing import Any

from . import agent_tools, businesses
from .agent_tools import Spoken
from .businesses import Business
from .config import settings

__all__ = [
    "Spoken",
    "TOOLS",
    "IMPLEMENTATIONS",
    "run_tool",
    "default_business",
    "check_availability",
    "book_table",
    "lookup_booking",
    "cancel_booking",
    "restaurant_info",
]


def default_business() -> Business:
    """The business DEFAULT_BUSINESS_SLUG points at."""
    slug = settings.default_business_slug.strip()
    if not slug:
        raise businesses.UnknownBusiness(
            "DEFAULT_BUSINESS_SLUG is not set, so there is no default business"
        )
    return businesses.by_slug(slug)


def _run(name: str, args: dict[str, Any]) -> Spoken:
    return agent_tools.IMPLEMENTATIONS[name](default_business(), args)


def check_availability(args: dict[str, Any]) -> Spoken:
    return _run("check_availability", args)


def lookup_booking(args: dict[str, Any]) -> Spoken:
    return _run("lookup_booking", args)


def cancel_booking(args: dict[str, Any]) -> Spoken:
    return _run("cancel_booking", args)


def restaurant_info(args: dict[str, Any]) -> Spoken:
    return _run("business_info", args)


def book_table(args: dict[str, Any]) -> Spoken:
    """Hold and confirm in one call, for callers written against the old API.

    The agent must NOT use this. It takes the hold and the details in the same
    breath, which is exactly the ordering PRD §8 forbids: by the time a caller
    has spelled their name, the time they were promised may be gone.
    """
    warnings.warn(
        "book_table is the pre-hold API; use hold() then confirm()",
        DeprecationWarning,
        stacklevel=2,
    )
    business = default_business()
    held = agent_tools.hold(business, args)
    if not held.data.get("ok"):
        return held

    return agent_tools.confirm(
        business,
        {
            "hold_id": held.data["hold_id"],
            "name": args.get("name", ""),
            "phone": args.get("phone", ""),
            "notes": args.get("notes", ""),
        },
    )


def run_tool(name: str, args: dict[str, Any], call_id: str = "") -> tuple[str, bool]:
    return agent_tools.run_tool_for(default_business(), name, args, call_id)


def TOOLS() -> list[dict[str, Any]]:  # noqa: N802 - kept for import compatibility
    """Tool declarations for the default business.

    A function, not a list: the declarations are worded from config now, and
    config is a database read that must not happen at import.
    """
    return agent_tools.tool_declarations(default_business())


IMPLEMENTATIONS = {
    "check_availability": check_availability,
    "book_table": book_table,
    "lookup_booking": lookup_booking,
    "cancel_booking": cancel_booking,
    "restaurant_info": restaurant_info,
}
