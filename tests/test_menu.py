"""Menu tools.

These answers are read aloud verbatim, so they are tested for two things: the
facts, and whether the sentence is actually speakable.
"""

import pytest

from calling_agent import menu as m


def test_no_category_lists_the_sections():
    out = m.get_menu({})
    for section in ("starters", "mains", "desserts"):
        assert section in out


def test_unknown_category_says_what_exists():
    out = m.get_menu({"category": "sushi"})
    assert "no 'sushi' section" in out
    assert "starters" in out


def test_a_section_names_dishes_with_prices():
    out = m.get_menu({"category": "mains"})
    assert "Butter Chicken" in out
    assert "480 rupees" in out


def test_long_sections_are_truncated_for_speech():
    """Reading forty dishes at a caller is useless."""
    out = m.get_menu({"category": "mains"})
    named = sum(1 for d in m.MENU if d.category == "mains" and d.name in out)
    assert named <= m.SPOKEN_LIMIT
    assert "more if they want" in out


def test_remainder_is_grammatical_when_exactly_one_is_left():
    """'There are 1 more' would be read out loud as written."""
    dishes = list(m.MENU)[: m.SPOKEN_LIMIT + 1]
    out = m._describe(dishes)
    assert "There is one more" in out
    assert "are 1 more" not in out


def test_remainder_spells_out_small_numbers():
    dishes = list(m.MENU)[: m.SPOKEN_LIMIT + 3]
    assert "three more" in m._describe(dishes)


def test_lists_read_naturally():
    assert m._join(["a"]) == "a"
    assert m._join(["a", "b"]) == "a and b"
    assert m._join(["a", "b", "c"]) == "a, b and c"


# --- dietary and allergens ---------------------------------------------------


@pytest.mark.parametrize("diet,attr", [("vegan", "vegan"), ("vegetarian", "vegetarian")])
def test_dietary_filters_only_return_matching_dishes(diet, attr):
    out = m.find_dishes({"dietary": diet})
    named = [d for d in m.MENU if d.name in out]
    assert named
    assert all(getattr(d, attr) for d in named)


def test_allergen_exclusion_is_honoured():
    """A wrong answer here could hurt someone."""
    out = m.find_dishes({"avoid": ["dairy"]})
    named = [d for d in m.MENU if d.name in out]
    assert named
    assert all("dairy" not in d.allergens for d in named)


def test_multiple_allergens_are_all_excluded():
    out = m.find_dishes({"avoid": ["dairy", "nuts", "gluten"]})
    named = [d for d in m.MENU if d.name in out]
    assert named
    for dish in named:
        assert not ({"dairy", "nuts", "gluten"} & set(dish.allergens))


def test_spice_ceiling_is_respected():
    out = m.find_dishes({"max_spice": 0})
    named = [d for d in m.MENU if d.name in out]
    assert named
    assert all(d.spice == 0 for d in named)


def test_price_ceiling_is_respected():
    out = m.find_dishes({"max_price": 200})
    named = [d for d in m.MENU if d.name in out]
    assert named
    assert all(d.price <= 200 for d in named)


def test_no_match_tells_the_agent_to_check_rather_than_invent():
    out = m.find_dishes({"query": "spaghetti carbonara"})
    assert "kitchen" in out


def test_unreadable_filters_are_reported_not_raised():
    assert "not a price" in m.find_dishes({"max_price": "cheap"})
    assert "not a spice level" in m.find_dishes({"max_spice": "very"})


# --- dish details ------------------------------------------------------------


def test_details_cover_price_spice_and_allergens():
    out = m.dish_details({"name": "butter chicken"})
    assert "480 rupees" in out
    assert "mild" in out
    assert "dairy" in out and "nuts" in out


def test_details_match_on_a_partial_name():
    """Speech recognition rarely delivers the full dish name."""
    assert "Gulab Jamun" in m.dish_details({"name": "gulab"})


def test_unknown_dish_is_reported():
    assert "not on the menu" in m.dish_details({"name": "pizza"})


def test_missing_name_asks_which_dish():
    assert "Which dish" in m.dish_details({})


def test_vegan_dishes_are_not_also_called_vegetarian():
    """Saying both is noise; vegan already implies it."""
    out = m.dish_details({"name": "chana masala"})
    assert "vegan" in out
    assert "vegetarian" not in out


# --- recommendations ---------------------------------------------------------


def test_recommendations_are_popular_dishes():
    out = m.recommend_dishes({})
    named = [d for d in m.MENU if d.name in out]
    assert named and all(d.popular for d in named)


def test_vegan_recommendations_are_all_vegan():
    out = m.recommend_dishes({"dietary": "vegan"})
    named = [d for d in m.MENU if d.name in out]
    assert named and all(d.vegan for d in named)


# --- data integrity ----------------------------------------------------------


def test_every_dish_sits_in_a_real_category():
    assert {d.category for d in m.MENU} <= set(m.CATEGORIES)


def test_every_category_has_at_least_one_dish():
    for category in m.CATEGORIES:
        assert any(d.category == category for d in m.MENU), category


def test_vegan_dishes_are_marked_vegetarian_too():
    """Otherwise a vegetarian search silently hides vegan food."""
    for dish in m.MENU:
        if dish.vegan:
            assert dish.vegetarian, dish.name


def test_dairy_dishes_are_never_marked_vegan():
    for dish in m.MENU:
        if "dairy" in dish.allergens:
            assert not dish.vegan, dish.name


def test_gluten_dishes_are_never_marked_gluten_free():
    for dish in m.MENU:
        if "gluten" in dish.allergens:
            assert not dish.gluten_free, dish.name


def test_every_declared_tool_has_an_implementation():
    assert {t["name"] for t in m.TOOLS} == set(m.IMPLEMENTATIONS)


def test_tool_schemas_use_the_shape_the_api_requires():
    for tool in m.TOOLS:
        assert tool["type"] == "function"
        assert "parameters" in tool and "input_schema" not in tool
        assert tool["description"]
