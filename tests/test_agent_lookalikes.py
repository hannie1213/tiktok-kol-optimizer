"""find_real_lookalikes: map synthetic placeholder creators to real ones."""
from backend import repo
from backend.agent import _tool_find_real_lookalikes, _tier

_TENANT = "lookalike_test"


def _kol(id, name, country, category, followers, er, source, url=None):
    return {
        "id": id, "name": name, "country": country, "category": category,
        "followers": followers, "engagement_rate": er, "fit_score": 0.7,
        "commission_rate": 0.15, "cost": 500.0, "avg_views": followers // 3,
        "avg_likes": 100, "gender_ratio": 0.5, "age_group": "18-24",
        "tiktok_url": url, "source": source,
    }


def _seed_library():
    repo.kols_replace(_TENANT, [
        # Synthetic placeholders (no URL, cannot be contacted)
        _kol(1, "cookjoy.sg01", "SG", "kitchen", 50_000, 0.06, "synthetic"),
        _kol(2, "pawsome.my01", "MY", "pet", 8_000, 0.15, "synthetic"),
        # Real FastMoss creators with TikTok links
        _kol(10, "sg_kitchen_micro", "SG", "kitchen", 60_000, 0.055, "fastmoss",
             "https://www.tiktok.com/@sg_kitchen_micro"),
        _kol(11, "th_kitchen_micro", "TH", "kitchen", 45_000, 0.06, "fastmoss",
             "https://www.tiktok.com/@th_kitchen_micro"),
        _kol(12, "sg_kitchen_mega", "SG", "kitchen", 2_000_000, 0.02, "fastmoss",
             "https://www.tiktok.com/@sg_kitchen_mega"),
        _kol(13, "sg_beauty_micro", "SG", "beauty", 55_000, 0.06, "fastmoss",
             "https://www.tiktok.com/@sg_beauty_micro"),
        # Real but unlinked — must never be suggested
        _kol(14, "no_url_real", "SG", "kitchen", 50_000, 0.06, "fastmoss"),
    ])


def test_tier_brackets():
    assert _tier(2_000_000) == "Mega"
    assert _tier(100_000) == "Macro"
    assert _tier(10_000) == "Micro"
    assert _tier(9_999) == "Nano"
    assert _tier(None) == "Nano"


def test_synthetic_creator_gets_ranked_real_matches():
    _seed_library()
    out = _tool_find_real_lookalikes(_TENANT, {"names": ["cookjoy.sg01"], "limit": 3})
    assert out["real_creators_in_library"] == 4  # linked real creators only
    (res,) = out["results"]
    assert res["profile"]["tier"] == "Micro"
    matches = res["real_matches"]
    assert matches and all(m["tiktok_url"] for m in matches)
    assert all(m["name"] != "no_url_real" for m in matches)
    # Same country + category + tier must outrank everything else.
    assert matches[0]["name"] == "sg_kitchen_micro"


def test_already_real_creator_is_flagged_not_matched():
    _seed_library()
    out = _tool_find_real_lookalikes(_TENANT, {"names": ["sg_beauty_micro"]})
    (res,) = out["results"]
    assert res["already_real"] is True
    assert res["tiktok_url"].startswith("https://www.tiktok.com/")


def test_unknown_name_reports_error_and_lookup_ignores_case():
    _seed_library()
    out = _tool_find_real_lookalikes(_TENANT, {"names": ["nobody_here", "COOKJOY.SG01"]})
    errors, found = out["results"]
    assert "error" in errors
    assert found["name"] == "cookjoy.sg01" and found["real_matches"]
