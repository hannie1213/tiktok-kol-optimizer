"""
Category taxonomy, parent fallback in the optimizer, and Chinese error messages.
"""
import os
import sys
import uuid

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from backend.main import app
from backend import categories, crud, i18n
from engine import models

client = TestClient(app)


def _account():
    email = f"cat_{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "secret123"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_every_category_has_engine_economics_and_a_valid_parent():
    assert len(categories.VALID_CATEGORIES) == len(set(categories.VALID_CATEGORIES)) >= 25
    for cid in categories.VALID_CATEGORIES:
        for table in (models._CTR, models._CVR, models._AOV):
            assert cid in table and set(table[cid]) >= {"MY", "ID", "TH", "PH", "SG", "VN"}, cid
    for child, parent in categories.PARENT.items():
        assert parent in categories.VALID_CATEGORIES and parent not in categories.PARENT, child
    assert categories.pool("womenswear") == ["womenswear", "fashion"]
    assert categories.pool("beauty") == ["beauty"]


def test_importer_synonyms_only_point_at_real_categories():
    assert set(crud._CATEGORY_SYNONYMS.values()) <= set(categories.VALID_CATEGORIES)
    assert crud._CATEGORY_SYNONYMS["shoes"] == "shoes" and crud._CATEGORY_SYNONYMS["food_beverage"] == "food"


def test_catalog_endpoint_lists_groups():
    groups = client.get("/categories").json()["groups"]
    ids = [c["id"] for g in groups for c in g["categories"]]
    assert ids == categories.VALID_CATEGORIES and len(groups) >= 6


def test_specific_category_falls_back_to_its_parent_in_the_optimizer_only():
    h = _account()
    # The seed library has "fashion" creators but none tagged "womenswear".
    assert client.get("/kols?category=womenswear&limit=5", headers=h).json()["total"] == 0
    r = client.post("/optimize", headers=h, json={"budget": 5000, "category": "womenswear", "countries": []})
    assert r.status_code == 200, r.text
    picked = r.json()["selected_kols"] if "selected_kols" in r.json() else r.json().get("best", {}).get("selected_kols", [])
    assert picked and all(k["category"] == "fashion" for k in picked)

    # A creator tagged with the specific category is usable, and listed exactly.
    add = client.post("/kols/add", headers=h, json={
        "name": "Dress Diary", "country": "MY", "category": "womenswear", "followers": 80000,
        "engagement_rate": 0.05, "fit_score": 0.8, "commission_rate": 0.1, "cost": 300,
        "avg_views": 20000, "avg_likes": 1500})
    assert add.status_code in (200, 201), add.text
    assert client.get("/kols?category=womenswear&limit=5", headers=h).json()["total"] == 1
    assert client.post("/optimize", headers=h, json={"budget": 5000, "category": "nonsense"}).status_code == 422


def test_error_messages_follow_the_interface_language():
    zh = {"X-Lang": "zh"}
    r = client.post("/auth/login", json={"email": "nobody@example.com", "password": "wrong-pass"}, headers=zh)
    assert r.status_code == 401 and r.json()["detail"] == "邮箱或密码不正确。"
    r = client.post("/auth/login", json={"email": "nobody@example.com", "password": "wrong-pass"})
    assert r.json()["detail"] == "Invalid email or password"

    h = {**_account(), **zh}
    assert client.post("/auth/register", headers=zh,
                       json={"email": "a@b.co", "password": "short"}).json()["detail"] == "密码至少需要 8 位。"
    assert client.post("/team/invites", headers=h, json={"email": "x@example.com"}).json()["detail"] \
        == "当前套餐包含 1 个席位，已全部占用。升级套餐后可邀请更多人。"
    assert client.get("/contracts/999999", headers=h).json()["detail"] == "找不到这份合同。"


def test_localise_substitutes_groups_and_leaves_unknown_text_alone():
    assert i18n.localise("The Pro plan is activated after payment — contact us to upgrade and we will switch your plan.", "zh") \
        == "专业版需要付款后开通，请联系我们升级。"
    assert i18n.localise("This order is already paid.", "zh-CN") == "这个订单已是「已付款」状态。"
    assert i18n.localise("Something nobody translated", "zh") == "Something nobody translated"
    assert i18n.localise("Not authenticated", "en") == "Not authenticated"
    assert i18n.localise([{"loc": ["body"]}], "zh") == [{"loc": ["body"]}]
