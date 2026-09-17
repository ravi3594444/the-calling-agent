"""The menu tools.

Everything here is read aloud, so what is asserted is what a caller hears.
The menu itself is per business now -- these tests load one, because a venue's
food is theirs and the agent must never answer from somebody else's card.
"""

from __future__ import annotations

from calling_agent import menu
from calling_agent.db import transaction
from tests.conftest import make_business


def _stock(business, items):
    from sqlalchemy import text

    with transaction() as conn:
        for position, item in enumerate(items):
            conn.execute(
                text(
                    "INSERT INTO menu_items (business_id, name, section, price,"
                    " description, tags, available, position)"
                    " VALUES (:b, :name, :section, :price, :description, :tags,"
                    " :available, :position)"
                ),
                {
                    "b": str(business.id),
                    "name": item["name"],
                    "section": item.get("section", ""),
                    "price": item.get("price"),
                    "description": item.get("description", ""),
                    "tags": item.get("tags", []),
                    "available": item.get("available", True),
                    "position": position,
                },
            )


def _venue():
    business = make_business(config={"features": {"menu": True}})
    _stock(
        business,
        [
            {"name": "Samosa Chaat", "section": "starters", "price": 220,
             "description": "Crushed samosas with chickpeas and yoghurt",
             "tags": ["vegetarian", "Contains dairy", "Contains gluten"]},
            {"name": "Paneer Tikka", "section": "starters", "price": 340,
             "description": "Cottage cheese charred in the tandoor",
             "tags": ["vegetarian", "Contains dairy"]},
            {"name": "Goan Fish Curry", "section": "mains", "price": 420,
             "description": "Kingfish in a coconut and tamarind gravy",
             "tags": ["Contains fish"]},
            {"name": "Mushroom Xacuti", "section": "mains", "price": 360,
             "description": "Mushrooms in a roasted coconut masala",
             "tags": ["vegan"]},
            {"name": "Pork Vindaloo", "section": "mains", "price": 480,
             "description": "Off tonight", "tags": [], "available": False},
        ],
    )
    return business


# --- what a caller hears -----------------------------------------------------


def test_the_agent_is_never_handed_the_whole_menu():
    """A tool that returned forty dishes would be read at the caller."""
    business = _venue()
    spoken = menu.get_menu(business, {"category": "mains"})
    assert spoken.count(",") <= menu.MAX_SPOKEN * 2


def test_asking_with_no_category_lists_the_sections():
    business = _venue()
    spoken = menu.get_menu(business, {})
    assert "starters" in spoken and "mains" in spoken


def test_prices_are_spoken_as_words_not_as_a_number_with_decimals():
    business = _venue()
    spoken = menu.get_menu(business, {"category": "mains"})
    assert "rupee" in spoken.lower()
    assert ".00" not in spoken


def test_a_dish_that_is_off_is_not_offered():
    business = _venue()
    assert "Vindaloo" not in menu.get_menu(business, {"category": "mains"})
    # ...but staff can still look it up, which is how they answer "do you do it?"
    assert "Vindaloo" in menu.dish_details(business, {"name": "vindaloo"})


# --- allergies ---------------------------------------------------------------


def test_an_allergen_is_excluded_rather_than_reasoned_about():
    business = _venue()
    spoken = menu.find_dishes(business, {"avoid": ["fish"]})
    assert "Fish Curry" not in spoken
    assert "Xacuti" in spoken


def test_exclusion_reads_the_description_too_not_just_the_tags():
    """A venue writes an ingredient in prose as often as in a tag."""
    business = _venue()
    spoken = menu.find_dishes(business, {"avoid": ["coconut"]})
    assert "Xacuti" not in spoken and "Fish Curry" not in spoken


def test_dish_details_says_what_it_contains_not_what_it_is_free_of():
    business = _venue()
    spoken = menu.dish_details(business, {"name": "samosa"})
    assert "Contains dairy" in spoken or "contains dairy" in spoken.lower()
    assert "free of" not in spoken.lower()
    assert "kitchen" in spoken.lower(), "an allergy answer must hand off to the kitchen"


def test_a_dietary_filter_keeps_only_what_is_marked():
    business = _venue()
    spoken = menu.find_dishes(business, {"dietary": "vegan"})
    assert "Xacuti" in spoken
    assert "Paneer" not in spoken


# --- edges -------------------------------------------------------------------


def test_a_venue_with_no_menu_says_so_rather_than_inventing_one():
    business = make_business()
    for tool in (menu.get_menu, menu.find_dishes, menu.recommend_dishes):
        assert "kitchen" in tool(business, {}).lower()


def test_an_unknown_dish_is_reported_not_invented():
    business = _venue()
    assert "not on the menu" in menu.dish_details(business, {"name": "lasagne"})


def test_a_price_ceiling_is_respected():
    business = _venue()
    spoken = menu.find_dishes(business, {"max_price": 300})
    assert "Samosa" in spoken
    assert "Fish Curry" not in spoken


def test_one_venue_never_sees_another_venue_s_menu():
    """business_id is on every query (PRD §20)."""
    stocked = _venue()
    other = make_business()
    _stock(other, [{"name": "Beef Wellington", "section": "mains", "price": 900}])

    assert "Wellington" not in menu.get_menu(stocked, {"category": "mains"})
    assert "Xacuti" not in menu.get_menu(other, {"category": "mains"})


def test_every_declared_menu_tool_has_an_implementation():
    declared = {tool["name"] for tool in menu.TOOLS}
    assert declared == set(menu.IMPLEMENTATIONS)
