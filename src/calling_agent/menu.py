"""The menu, and the tools for answering questions about it.

The menu is per business and lives in `menu_items`, edited in the dashboard.
There is no built-in menu: a venue's food is theirs, and an agent that answered
from a demo card would confidently describe dishes the kitchen has never made.

Everything here is written to be SPOKEN. A menu tool that returns the whole
card is useless on a phone call -- the agent would read forty dishes at a
caller. So each function returns a handful of items in a sentence the agent can
say as-is, and the prompt tells it to offer a few and ask.

ALLERGIES BIAS TOWARDS EXCLUDING. `find_dishes(avoid=...)` drops anything whose
name, description or tags mention the allergen at all. Over-excluding costs a
recommendation; under-excluding could hurt someone.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from . import formatting
from .businesses import Business
from .db import fetch_all, readonly

#: Tags treated as dietary claims rather than warnings. Everything else a venue
#: writes is shown as-is, because "Contains fish" and "Chef's pick" are both
#: things they type and neither should be silently reinterpreted.
DIETARY_TAGS = frozenset(
    {
        "vegan",
        "vegetarian",
        "veg",
        "gluten free",
        "gluten-free",
        "dairy free",
        "dairy-free",
        "nut free",
        "nut-free",
        "halal",
        "kosher",
        "jain",
    }
)

MAX_SPOKEN = 3


@dataclass(frozen=True)
class Dish:
    name: str
    section: str
    price: Decimal | None
    description: str
    tags: tuple[str, ...]

    @property
    def dietary(self) -> list[str]:
        return [t for t in self.tags if t.strip().lower() in DIETARY_TAGS]

    @property
    def notes(self) -> list[str]:
        return [t for t in self.tags if t.strip().lower() not in DIETARY_TAGS]

    def haystack(self) -> str:
        return " ".join([self.name, self.description, *self.tags]).lower()


def _load(business: Business, *, available_only: bool = True) -> list[Dish]:
    clause = " AND available" if available_only else ""
    with readonly() as conn:
        rows = fetch_all(
            conn,
            "SELECT name, section, price, description, tags FROM menu_items"
            " WHERE business_id = :b" + clause + " ORDER BY position, name",
            b=str(business.id),
        )
    return [
        Dish(
            name=r.name,
            section=(r.section or "").strip(),
            price=r.price,
            description=(r.description or "").strip(),
            tags=tuple(r.tags or []),
        )
        for r in rows
    ]


def _price(business: Business, dish: Dish) -> str:
    """"420 Indian rupees" -- read aloud, so a name rather than a symbol."""
    return formatting.spoken_money(business, dish.price)


def _describe(business: Business, dishes: list[Dish]) -> str:
    spoken = []
    for dish in dishes[:MAX_SPOKEN]:
        price = _price(business, dish)
        spoken.append(f"{dish.name}, {price}" if price else dish.name)
    return _join(spoken)


def _join(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" and {items[-1]}"


# --- tools -------------------------------------------------------------------


def get_menu(business: Business, args: dict[str, Any]) -> str:
    """A few dishes from one section, or the list of sections."""
    dishes = _load(business)
    if not dishes:
        return "There is no menu loaded, so offer to check with the kitchen."

    category = str(args.get("category", "")).strip().lower()
    if not category:
        sections = sorted({d.section for d in dishes if d.section})
        if not sections:
            return f"We have {_describe(business, dishes)}, among other things."
        return "The menu has " + _join(sections) + "."

    found = [d for d in dishes if d.section.lower() == category]
    if not found:
        return f"There is no {category} section. Offer to check with the kitchen."
    return f"In {category} we have {_describe(business, found)}."


def find_dishes(business: Business, args: dict[str, Any]) -> str:
    """Search, with allergies handled by exclusion rather than by reasoning."""
    dishes = _load(business)
    if not dishes:
        return "There is no menu loaded, so offer to check with the kitchen."

    query = str(args.get("query", "")).strip().lower()
    if query:
        dishes = [d for d in dishes if query in d.haystack()]

    dietary = str(args.get("dietary", "")).strip().lower()
    if dietary:
        dishes = [d for d in dishes if dietary in " ".join(d.dietary).lower()]

    for allergen in args.get("avoid") or []:
        word = str(allergen).strip().lower()
        if word:
            dishes = [d for d in dishes if word not in d.haystack()]

    max_price = args.get("max_price")
    if max_price is not None:
        try:
            ceiling = Decimal(str(max_price))
            dishes = [d for d in dishes if d.price is not None and d.price <= ceiling]
        except (TypeError, ValueError, ArithmeticError):
            return "I could not read that price. Ask them for a number."

    if not dishes:
        return "Nothing on the menu matches that. Offer to check with the kitchen."
    return _describe(business, dishes) + "."


def dish_details(business: Business, args: dict[str, Any]) -> str:
    """What one dish IS. Says what it contains, never what it is free of."""
    name = str(args.get("name", "")).strip().lower()
    if not name:
        return "Which dish?"

    dishes = _load(business, available_only=False)
    match = next((d for d in dishes if name in d.name.lower()), None)
    if match is None:
        return f"'{args.get('name')}' is not on the menu."

    price = _price(business, match)
    parts = [f"{match.name} is {price}." if price else f"{match.name}."]
    if match.description:
        parts.append(f"{match.description}.")
    if match.dietary:
        parts.append(f"It is {_join(match.dietary)}.")
    if match.notes:
        parts.append(f"Noted on it: {_join(match.notes)}.")
    parts.append("If they have an allergy, say the kitchen will go through it with them.")
    return " ".join(parts)


def recommend_dishes(business: Business, args: dict[str, Any]) -> str:
    """Have an opinion. A list is not a recommendation."""
    dishes = _load(business)
    if not dishes:
        return "There is no menu loaded, so offer to check with the kitchen."

    diet = str(args.get("dietary", "")).strip().lower()
    if diet:
        dishes = [d for d in dishes if diet in " ".join(d.dietary).lower()]
    if not dishes:
        return "Nothing stands out for that. Offer to ask the kitchen."
    return f"Try the {_describe(business, dishes)}."


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "get_menu",
        "description": (
            "A few dishes from one section of the menu. Call with no category to "
            "hear which sections exist. Never read the whole menu aloud."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "A section name, as the venue wrote it.",
                }
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "find_dishes",
        "description": (
            "Search the menu. Use for dietary requirements, allergies and budgets. "
            "Search with the allergen excluded rather than reasoning about it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free text, e.g. 'chicken'."},
                "dietary": {
                    "type": "string",
                    "description": "vegan, vegetarian, gluten free, halal, jain.",
                },
                "avoid": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Allergens to exclude: dairy, nuts, gluten, fish, shellfish.",
                },
                "max_price": {
                    "type": "integer",
                    "description": "Most they want to spend, in the venue's currency.",
                },
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "dish_details",
        "description": (
            "Price, description and anything noted on one dish. Use whenever a "
            "caller asks what is in something."
        ),
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Dish name."}},
            "required": ["name"],
        },
    },
    {
        "type": "function",
        "name": "recommend_dishes",
        "description": "Suggest something, optionally for a dietary requirement.",
        "parameters": {
            "type": "object",
            "properties": {
                "dietary": {
                    "type": "string",
                    "description": "Optional: vegan, vegetarian, gluten free.",
                }
            },
            "required": [],
        },
    },
]

IMPLEMENTATIONS = {
    "get_menu": get_menu,
    "find_dishes": find_dishes,
    "dish_details": dish_details,
    "recommend_dishes": recommend_dishes,
}
