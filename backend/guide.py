"""
Merchant guide — curated TikTok Shop SEA playbooks served to the console
and to the AI copilot.

Content lives in data/merchant_guide.json (distilled from the official
"TikTok Shop东南亚电商" WeChat account: store setup, product selection,
five-country market insights, mega-sale playbooks and case studies).

Endpoints
---------
  GET /guide            All topics in one language (?lang=zh|en, default en)
  GET /guide/{topic}    One topic (same lang switch)
"""
import json
import os
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query

from backend import tenancy
from backend.auth import require_tenant

router = APIRouter()

_GUIDE_PATH = os.path.join(tenancy.DATA_DIR, "merchant_guide.json")


@lru_cache(maxsize=1)
def _load() -> dict:
    with open(_GUIDE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _localize(doc: dict, lang: str) -> dict:
    """Flatten the bilingual document to one language for the client."""
    lang = "zh" if (lang or "").lower().startswith("zh") else "en"
    return {
        "version": doc.get("version"),
        "source_note": doc.get(f"source_note_{lang}"),
        "topics": [
            {
                "id": t["id"],
                "icon": t.get("icon", "menu_book"),
                "title": t["title"][lang],
                "tagline": t["tagline"][lang],
                "sections": [
                    {
                        "title": s["title"][lang],
                        "points": [p[lang] for p in s["points"]],
                    }
                    for s in t["sections"]
                ],
            }
            for t in doc["topics"]
        ],
    }


def guide_topic(topic_id: str, lang: str = "zh") -> dict:
    """One localized topic — also used by the AI copilot tool."""
    doc = _localize(_load(), lang)
    match = next((t for t in doc["topics"] if t["id"] == topic_id), None)
    if match is None:
        raise KeyError(topic_id)
    return match


@router.get("/guide")
def get_guide(lang: str = Query("en"), _t: str = Depends(require_tenant)):
    return _localize(_load(), lang)


@router.get("/guide/{topic_id}")
def get_guide_topic(topic_id: str, lang: str = Query("en"), _t: str = Depends(require_tenant)):
    try:
        return guide_topic(topic_id, lang)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown guide topic: {topic_id}")
