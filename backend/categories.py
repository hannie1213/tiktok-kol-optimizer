"""
Product categories — one list for the API, the importer and the console.

The taxonomy follows the departments the big SEA marketplaces share. Each
category belongs to a display group, and the more specific ones name a
`parent`: the broader category whose creators are still a sensible fit.

Why parents matter: an optimization for "womenswear" should not come back
empty just because the merchant's library tags its fashion creators as plain
"fashion". `pool("womenswear")` is therefore ["womenswear", "fashion"] — the
optimizer draws on both, while the creator library's own filter stays exact.

Per-category conversion economics live in engine/models.py and must have an
entry for every id here (tests/test_categories.py checks that).
"""

# (group id, [(category id, parent or None), …]) — display order.
CATEGORY_GROUPS = [
    ("beauty", [("beauty", None), ("personal_care", "beauty")]),
    ("fashion", [("fashion", None), ("womenswear", "fashion"), ("menswear", "fashion"),
                 ("kids_fashion", "fashion"), ("muslim_fashion", None),
                 ("accessories", None), ("shoes", "accessories"), ("bags", "accessories"),
                 ("jewelry", "accessories")]),
    ("electronics", [("electronics", None), ("computers", "electronics"), ("appliances", None)]),
    ("home", [("home", None), ("furniture", "home"), ("textiles", "home"), ("kitchen", None),
              ("tools", "home")]),
    ("family", [("mother_baby", None), ("toys", None), ("stationery", "toys")]),
    ("food_health", [("food", "fmcg"), ("fmcg", None), ("health", None)]),
    ("leisure", [("sports_outdoor", None), ("pet", None), ("automotive", None)]),
]

VALID_CATEGORIES = [cid for _, items in CATEGORY_GROUPS for cid, _ in items]
PARENT = {cid: parent for _, items in CATEGORY_GROUPS for cid, parent in items if parent}


def pool(category: str) -> list:
    """Categories whose creators can serve a campaign in `category`."""
    return [category, PARENT[category]] if category in PARENT else [category]


def catalog() -> list:
    """The grouped list, for the console's category pickers."""
    return [{"group": gid, "categories": [{"id": cid, "parent": parent} for cid, parent in items]}
            for gid, items in CATEGORY_GROUPS]
