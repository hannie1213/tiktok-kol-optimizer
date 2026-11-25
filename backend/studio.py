"""
AI Studio — sales-video storyboards, operations copy, and a place to keep them.

Sales videos
------------
The server does not render video. It asks the LLM for a storyboard — a list
of timed scenes, each with an on-screen caption, a voiceover line and a note
on what to show — and the console turns that storyboard plus the merchant's
own product photos into a vertical video in the browser (canvas + MediaRecorder).
Nothing is uploaded: the photos never leave the merchant's machine.

Operations copy
---------------
One endpoint, several kinds of everyday commerce writing: a livestream
run-of-show, a product listing, post captions with hashtags, a creator
outreach message, customer-service answers, and a week of content ideas.

Both use the copilot's LLM and its monthly quota (see pipeline._ask_llm).

  POST   /studio/storyboard     Storyboard for a sales video
  POST   /studio/ops            Operations copy of a given kind
  GET    /studio/saved          Saved pieces (newest first)
  POST   /studio/saved          Save a piece
  DELETE /studio/saved/{id}
"""
import json
import re
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import repo
from backend.auth import current_user, require_tenant
from backend.pipeline import _ask_llm, _lang_name

router = APIRouter()

OPS_KINDS = {
    "live_script": "a livestream selling run-of-show: opening hook, warm-up, product demo beats with "
                   "talking points, a limited-time offer segment, objection handling, and a closing "
                   "call to action — each with rough minutes",
    "listing": "a product listing: one search-friendly title (under 120 characters), five benefit-led "
               "bullet points, and a short description paragraph",
    "caption": "three alternative short-video post captions, each with a hook line and 6–8 relevant "
               "hashtags",
    "outreach": "a short, friendly first message inviting a creator to collaborate on this product, "
                "plus a one-line follow-up to send if there is no reply",
    "faq": "eight customer-service questions shoppers are likely to ask about this product, each "
           "with a clear, honest answer",
    "calendar": "a seven-day content plan: for each day a content format (short video, livestream, "
                "post), an angle, and a one-line hook",
}
SAVED_TYPES = ("storyboard", "ops", "script", "review")
_SAVED_MAX = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_from(text: str) -> Optional[dict]:
    """The model is asked for bare JSON but may wrap it in a code fence or a
    sentence; take the outermost object."""
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        data = json.loads(text[a:b + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class StoryboardRequest(BaseModel):
    product: str = Field(..., min_length=2, max_length=200)
    selling_points: str = Field("", max_length=1200)
    duration_sec: int = Field(20, ge=8, le=90)
    style: str = Field("", max_length=80)
    offer: str = Field("", max_length=160)                  # e.g. "RM39, free shipping this week"
    lang: str = Field("en", max_length=8)                   # interface language (notes)
    script_lang: str = Field("", max_length=40)             # language of captions and voiceover


def _clean_scenes(raw, duration: int) -> List[dict]:
    scenes = []
    for s in raw if isinstance(raw, list) else []:
        if not isinstance(s, dict):
            continue
        caption = " ".join(str(s.get("caption") or "").split())[:90]
        voice = " ".join(str(s.get("voiceover") or "").split())[:300]
        if not caption and not voice:
            continue
        try:
            seconds = float(s.get("seconds") or 0)
        except (TypeError, ValueError):
            seconds = 0
        scenes.append({"seconds": seconds if seconds > 0 else 3.0, "caption": caption or voice[:60],
                       "voiceover": voice, "visual": " ".join(str(s.get("visual") or "").split())[:200]})
    scenes = scenes[:12]
    if scenes:                                           # make the scene lengths add up to the request
        scale = duration / sum(s["seconds"] for s in scenes)
        for s in scenes:
            s["seconds"] = round(max(1.5, s["seconds"] * scale), 1)
    return scenes


@router.post("/studio/storyboard")
def storyboard(req: StoryboardRequest, tenant_id: str = Depends(require_tenant)):
    spoken = req.script_lang.strip() or _lang_name(req.lang)
    brief = {"product": req.product.strip(), "selling_points": req.selling_points.strip(),
             "offer": req.offer.strip(), "length_seconds": req.duration_sec,
             "style": req.style.strip() or "fast, upbeat, honest", "caption_language": spoken}
    system = (
        "You plan short vertical sales videos for social commerce. From the JSON brief, return ONLY a "
        "JSON object, no prose and no code fence, with these keys: "
        '"title" (string), "scenes" (array of 4 to 8 objects), "cta" (string), "hashtags" (array of '
        '5 to 8 strings without the # sign), "music" (string, a mood, not a song title). '
        'Each scene has "seconds" (number), "caption" (the on-screen text: at most 9 words, or 16 '
        'characters for Chinese), "voiceover" (one spoken sentence) and "visual" (what to film or show). '
        "The first scene is a hook that stops the scroll; the last scene is the call to action. Scene "
        "seconds must add up to length_seconds. Captions, voiceover, cta and hashtags are in "
        f"caption_language; \"visual\" and \"music\" are in {_lang_name(req.lang)}. Use only the selling "
        "points and offer given — never invent prices, discounts, certifications, or medical or "
        "earnings claims."
    )
    text = _ask_llm(tenant_id, system, json.dumps(brief, ensure_ascii=False))
    data = _json_from(text)
    scenes = _clean_scenes((data or {}).get("scenes"), req.duration_sec)
    if not data or len(scenes) < 2:
        repo.agent_refund(tenant_id, datetime.now(timezone.utc).strftime("%Y-%m"))
        raise HTTPException(status_code=502, detail="The AI returned an unreadable answer — please try again.")
    tags = [re.sub(r"[#\s]+", "", str(t))[:40] for t in (data.get("hashtags") or []) if str(t).strip()][:10]
    return {"title": " ".join(str(data.get("title") or req.product).split())[:120],
            "scenes": scenes, "cta": " ".join(str(data.get("cta") or "").split())[:160],
            "hashtags": [t for t in tags if t], "music": " ".join(str(data.get("music") or "").split())[:120],
            "duration_sec": round(sum(s["seconds"] for s in scenes), 1)}


class OpsRequest(BaseModel):
    kind: str = Field(..., max_length=20)
    product: str = Field(..., min_length=2, max_length=200)
    selling_points: str = Field("", max_length=1200)
    audience: str = Field("", max_length=160)
    lang: str = Field("en", max_length=8)
    out_lang: str = Field("", max_length=40)


@router.post("/studio/ops")
def ops_copy(req: OpsRequest, tenant_id: str = Depends(require_tenant)):
    if req.kind not in OPS_KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of: {', '.join(OPS_KINDS)}")
    brief = {"product": req.product.strip(), "selling_points": req.selling_points.strip(),
             "audience": req.audience.strip() or "online shoppers in Southeast Asia"}
    system = (
        "You write everyday operations copy for a social-commerce seller in Southeast Asia. "
        f"From the JSON brief, write {OPS_KINDS[req.kind]}. Be specific to this product and audience, "
        "ready to use as written. Use only the facts in the brief — never invent prices, discounts, "
        "stock levels, certifications, or medical or earnings claims; where a fact is needed and "
        f"missing, write a [placeholder]. Plain text with clear headings, no markdown tables. "
        f"Write in {req.out_lang.strip() or _lang_name(req.lang)}."
    )
    return {"kind": req.kind, "text": _ask_llm(tenant_id, system, json.dumps(brief, ensure_ascii=False))}


# ── Saved pieces ──────────────────────────────────────────────────
class SaveRequest(BaseModel):
    type: str = Field(..., max_length=16)
    title: str = Field(..., min_length=1, max_length=120)
    body: str = Field(..., min_length=1, max_length=20000)     # text, or a storyboard as JSON


@router.get("/studio/saved")
def list_saved(tenant_id: str = Depends(require_tenant)):
    return {"items": repo.docs_list(tenant_id, "ai_doc", limit=_SAVED_MAX)}


@router.post("/studio/saved")
def save_piece(req: SaveRequest, tenant_id: str = Depends(require_tenant)):
    if req.type not in SAVED_TYPES:
        raise HTTPException(status_code=422, detail=f"type must be one of: {', '.join(SAVED_TYPES)}")
    if repo.docs_count(tenant_id, "ai_doc") >= _SAVED_MAX:
        raise HTTPException(status_code=409, detail="Saved content limit reached — delete some you no longer need.")
    me = current_user()
    return repo.doc_add(tenant_id, "ai_doc", {"type": req.type, "title": req.title.strip(), "body": req.body,
                                               "created_by": me["email"] if me else ""}, _now())


@router.delete("/studio/saved/{doc_id}")
def delete_saved(doc_id: int, tenant_id: str = Depends(require_tenant)):
    if not repo.doc_delete(tenant_id, "ai_doc", doc_id):
        raise HTTPException(status_code=404, detail="Saved item not found")
    return {"deleted": True}
