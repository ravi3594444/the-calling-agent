"""The menu, and the tools for answering questions about it.

Placeholder data. Prices are in rupees.

Everything here is written to be *spoken*. A menu tool that returns the whole
card is useless on a phone call: the agent would read forty dishes at a
caller. So each function returns a handful of items in a sentence the agent can
say as-is, and the prompt tells it to offer a few and ask.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Dish:
    name: str
    category: str
    price: int
    description: str
    spice: int = 0  # 0 mild, 1 medium, 2 hot
    vegan: bool = False
    vegetarian: bool = False
    gluten_free: bool = False
    popular: bool = False
    allergens: tuple[str, ...] = field(default_factory=tuple)

    def spoken(self) -> str:
        return f"{self.name}, {self.price} rupees"


MENU: tuple[Dish, ...] = (
    # --- Starters ---
    Dish("Samosa Chaat", "starters", 220,
         "Crushed samosas with chickpeas, yoghurt and tamarind chutney",
         spice=1, vegetarian=True, popular=True, allergens=("gluten", "dairy")),
    Dish("Paneer Tikka", "starters", 340,
         "Cottage cheese charred in the tandoor with peppers and onion",
         spice=1, vegetarian=True, gluten_free=True, popular=True, allergens=("dairy",)),
    Dish("Chilli Gobi", "starters", 280,
         "Crisp cauliflower tossed with green chilli and spring onion",
         spice=2, vegan=True, vegetarian=True),
    Dish("Tandoori Chicken Wings", "starters", 360,
         "Yoghurt and red chilli marinade, cooked over charcoal",
         spice=1, gluten_free=True, allergens=("dairy",)),
    Dish("Amritsari Fish", "starters", 420,
         "Gram flour battered river fish with ajwain and lemon",
         spice=1, allergens=("fish",)),

    # --- Mains ---
    Dish("Butter Chicken", "mains", 480,
         "Charcoal chicken in a tomato and cream gravy, lightly sweet",
         spice=0, gluten_free=True, popular=True, allergens=("dairy", "nuts")),
    Dish("Rogan Josh", "mains", 540,
         "Slow-cooked Kashmiri lamb with fennel and dried chilli",
         spice=2, gluten_free=True, allergens=("dairy",)),
    Dish("Dal Makhani", "mains", 380,
         "Black lentils simmered overnight with butter and cream",
         spice=0, vegetarian=True, gluten_free=True, popular=True, allergens=("dairy",)),
    Dish("Chana Masala", "mains", 340,
         "Chickpeas with ginger, tomato and roasted cumin",
         spice=1, vegan=True, vegetarian=True, gluten_free=True),
    Dish("Palak Paneer", "mains", 420,
         "Cottage cheese in a spinach gravy with garlic",
         spice=0, vegetarian=True, gluten_free=True, allergens=("dairy",)),
    Dish("Baingan Bharta", "mains", 360,
         "Smoked aubergine mashed with tomato and green chilli",
         spice=1, vegan=True, vegetarian=True, gluten_free=True),
    Dish("Goan Prawn Curry", "mains", 620,
         "Prawns in coconut and kokum, tart and hot",
         spice=2, gluten_free=True, allergens=("shellfish",)),

    # --- Breads and rice ---
    Dish("Butter Naan", "breads", 90, "Leavened flatbread from the tandoor",
         vegetarian=True, popular=True, allergens=("gluten", "dairy")),
    Dish("Garlic Naan", "breads", 110, "Naan with garlic and coriander",
         vegetarian=True, allergens=("gluten", "dairy")),
    Dish("Laccha Paratha", "breads", 120, "Layered wholewheat flatbread",
         vegetarian=True, allergens=("gluten", "dairy")),
    Dish("Steamed Basmati", "rice", 150, "Plain long-grain rice",
         vegan=True, vegetarian=True, gluten_free=True),
    Dish("Hyderabadi Biryani", "rice", 520,
         "Layered rice with mutton, saffron and fried onion",
         spice=1, allergens=("dairy",)),
    Dish("Vegetable Pulao", "rice", 320,
         "Basmati with peas, carrot and whole spices",
         vegan=True, vegetarian=True, gluten_free=True),

    # --- Sides ---
    Dish("Raita", "sides", 120, "Whisked yoghurt with cucumber and cumin",
         vegetarian=True, gluten_free=True, allergens=("dairy",)),
    Dish("Kachumber Salad", "sides", 140, "Onion, tomato and cucumber with lime",
         vegan=True, vegetarian=True, gluten_free=True),

    # --- Desserts ---
    Dish("Gulab Jamun", "desserts", 180, "Milk dumplings in rose syrup, served warm",
         vegetarian=True, popular=True, allergens=("dairy", "gluten")),
    Dish("Kulfi", "desserts", 200, "Set pistachio and cardamom ice cream",
         vegetarian=True, gluten_free=True, allergens=("dairy", "nuts")),
    Dish("Gajar Halwa", "desserts", 190, "Carrot slow-cooked in milk with ghee",
         vegetarian=True, gluten_free=True, allergens=("dairy", "nuts")),

    # --- Drinks ---
    Dish("Masala Chai", "drinks", 90, "Black tea boiled with ginger and cardamom",
         vegetarian=True, gluten_free=True),
    Dish("Sweet Lassi", "drinks", 140, "Chilled yoghurt drink",
         vegetarian=True, gluten_free=True, allergens=("dairy",)),
    Dish("Nimbu Pani", "drinks", 80, "Fresh lime with salt or sugar",
         vegan=True, vegetarian=True, gluten_free=True),
)

CATEGORIES = ("starters", "mains", "breads", "rice", "sides", "desserts", "drinks")

# How many dishes to name in one spoken answer. More than this and the caller
# has stopped listening.
SPOKEN_LIMIT = 4


def _join(items: list[str]) -> str:
    """Natural list: 'a, b and c'."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


# Spoken out, "1" and "2" read better as words.
_SMALL_NUMBERS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
}


def _spoken_count(n: int) -> str:
    return _SMALL_NUMBERS.get(n, str(n))


def _describe(dishes: list[Dish], limit: int = SPOKEN_LIMIT) -> str:
    shown = dishes[:limit]
    text = _join([d.spoken() for d in shown])
    remaining = len(dishes) - len(shown)
    if remaining == 1:
        text += ". There is one more if they want to hear it"
    elif remaining > 1:
        text += f". There are {_spoken_count(remaining)} more if they want to hear them"
    return text


# --- Tools -------------------------------------------------------------------


def get_menu(args: dict[str, Any]) -> str:
    category = str(args.get("category", "")).strip().lower()
    if not category:
        return (
            "The menu has " + _join(list(CATEGORIES)) + ". "
            "Ask about one of those, or ask what is popular."
        )
    if category not in CATEGORIES:
        return f"There is no '{category}' section. We have {_join(list(CATEGORIES))}."

    dishes = [d for d in MENU if d.category == category]
    return f"In {category} we have {_describe(dishes)}."


def find_dishes(args: dict[str, Any]) -> str:
    query = str(args.get("query", "")).strip().lower()
    diet = str(args.get("dietary", "")).strip().lower()
    avoid = [a.strip().lower() for a in (args.get("avoid") or []) if str(a).strip()]
    max_price = args.get("max_price")
    spice = args.get("max_spice")

    found = list(MENU)
    if query:
        found = [
            d for d in found
            if query in d.name.lower() or query in d.description.lower()
        ]
    if diet in ("vegan",):
        found = [d for d in found if d.vegan]
    elif diet in ("vegetarian", "veg"):
        found = [d for d in found if d.vegetarian]
    elif diet in ("gluten free", "gluten-free", "gluten_free"):
        found = [d for d in found if d.gluten_free]
    if avoid:
        found = [
            d for d in found
            if not any(a in allergen for allergen in d.allergens for a in avoid)
        ]
    if max_price is not None:
        try:
            found = [d for d in found if d.price <= int(max_price)]
        except (TypeError, ValueError):
            return f"'{max_price}' is not a price I can read."
    if spice is not None:
        try:
            found = [d for d in found if d.spice <= int(spice)]
        except (TypeError, ValueError):
            return f"'{spice}' is not a spice level I can read. Use 0, 1 or 2."

    if not found:
        return "Nothing on the menu matches that. Offer to check with the kitchen."
    return _describe(found)


def dish_details(args: dict[str, Any]) -> str:
    name = str(args.get("name", "")).strip().lower()
    if not name:
        return "Which dish?"
    match = next((d for d in MENU if name in d.name.lower()), None)
    if match is None:
        return f"'{args.get('name')}' is not on the menu."

    spice = {0: "mild", 1: "medium", 2: "hot"}[match.spice]
    parts = [f"{match.name} is {match.price} rupees. {match.description}. It is {spice}."]
    tags = []
    if match.vegan:
        tags.append("vegan")
    elif match.vegetarian:
        tags.append("vegetarian")
    if match.gluten_free:
        tags.append("gluten free")
    if tags:
        parts.append(f"It is {_join(tags)}.")
    if match.allergens:
        parts.append(f"It contains {_join(list(match.allergens))}.")
    return " ".join(parts)


def recommend_dishes(args: dict[str, Any]) -> str:
    diet = str(args.get("dietary", "")).strip().lower()
    picks = [d for d in MENU if d.popular]
    if diet in ("vegan",):
        picks = [d for d in MENU if d.vegan]
    elif diet in ("vegetarian", "veg"):
        picks = [d for d in picks if d.vegetarian] or [d for d in MENU if d.vegetarian]

    if not picks:
        return "Nothing stands out for that. Offer to ask the kitchen."
    return "Most ordered: " + _describe(picks) + "."


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "get_menu",
        "description": (
            "List dishes in one section of the menu. Call with no category to "
            "hear which sections exist."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": (
                        "One of: starters, mains, breads, rice, sides, desserts, drinks."
                    ),
                }
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "find_dishes",
        "description": (
            "Search the menu. Use for dietary requirements, allergies, budgets "
            "and 'something not too spicy' style requests."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free text, e.g. 'chicken', 'lentil'."},
                "dietary": {
                    "type": "string",
                    "description": "One of: vegan, vegetarian, gluten free.",
                },
                "avoid": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Allergens to exclude: dairy, nuts, gluten, fish, shellfish."
                    ),
                },
                "max_price": {
                    "type": "integer",
                    "description": "Most they want to spend, in rupees.",
                },
                "max_spice": {
                    "type": "integer",
                    "description": "0 mild, 1 medium, 2 hot. Filters to this level or below.",
                },
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "dish_details",
        "description": (
            "Price, description, spice level, dietary tags and allergens for one "
            "dish. Use whenever a caller asks what is in something."
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
        "description": "Suggest popular dishes, optionally for a dietary requirement.",
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
