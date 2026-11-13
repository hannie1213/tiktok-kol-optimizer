"""
Collaboration pipeline, analytics and AI script tools — the part of a campaign
between "the optimizer picked these creators" and "we logged the GMV".

Pipeline
--------
Every creator in a saved campaign moves through outreach stages:

    shortlisted → invited → negotiating → contracted → submitted → live → paid
                                  ↘ declined (from any stage)

The state lives inside the campaign document (`doc["pipeline"][kol_id]`), so
it needs no table of its own, is deleted with the campaign, and is exported
with the workspace.

  GET  /pipeline                       Every creator × campaign, with filters
  PUT  /pipeline/{campaign}/{kol}      Move a creator / record fee, note, link
  GET  /pipeline/export.csv            The filtered list as CSV

Analytics
---------
  GET  /analytics                      KPIs, month-on-month trend, funnel, top creators
  POST /analytics/insights             AI read-out of those numbers (uses copilot quota)

Scripts
-------
  POST /scripts/generate               Short-video / live script for a product + creator
  POST /scripts/check                  Review a draft script: hook, clarity, risky claims

The AI endpoints share the copilot's LLM and its monthly quota.
"""
import csv
import io
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from backend import activity, agent, ratelimit, repo
from backend.auth import current_user, require_tenant
from backend.billing import check_and_consume_agent

router = APIRouter()

STAGES = ("shortlisted", "invited", "negotiating", "contracted", "submitted", "live", "paid", "declined")
# Stages at which a fee has been agreed and is owed or already paid.
_COMMITTED = ("contracted", "submitted", "live", "paid")
_HISTORY_MAX = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _entry(campaign: dict, kol_id: int) -> dict:
    return (campaign.get("pipeline") or {}).get(str(kol_id)) or {}


def _items(tenant_id: str) -> list:
    """One row per creator per campaign, newest campaign first."""
    rows = []
    campaigns = sorted(repo.campaigns_all(tenant_id), key=lambda c: c.get("created_at", ""), reverse=True)
    for c in campaigns:
        for k in c.get("selected_kols") or []:
            e = _entry(c, k.get("id"))
            rows.append({
                "campaign_id": c["id"], "campaign_name": c.get("name", ""),
                "kol_id": k.get("id"), "name": k.get("name", ""),
                "country": k.get("country", ""), "category": k.get("category", ""),
                "followers": k.get("followers", 0), "tier": k.get("tier", ""),
                "planned_cost": k.get("cost", 0), "predicted_gmv": k.get("predicted_gmv", 0),
                "status": e.get("status", "shortlisted"),
                "fee": e.get("fee"), "note": e.get("note") or "",
                "content_url": e.get("content_url") or "", "contact": e.get("contact") or "",
                "assignee": e.get("assignee") or "",
                "updated_at": e.get("updated_at") or c.get("created_at", ""),
            })
    return rows


def _filter(rows: list, status: Optional[str], campaign_id: Optional[int], q: Optional[str],
            assignee: Optional[str] = None) -> list:
    if assignee is not None:
        rows = [r for r in rows if r["assignee"] == assignee]
    if status:
        rows = [r for r in rows if r["status"] == status]
    if campaign_id is not None:
        rows = [r for r in rows if r["campaign_id"] == campaign_id]
    if q:
        needle = q.strip().lower()
        rows = [r for r in rows if needle in r["name"].lower() or needle in r["contact"].lower()
                or needle in r["campaign_name"].lower()]
    return rows


def _check_status(status: Optional[str]):
    if status and status not in STAGES:
        raise HTTPException(status_code=422, detail=f"status must be one of: {', '.join(STAGES)}")


# ── Pipeline ──────────────────────────────────────────────────────
@router.get("/pipeline")
def list_pipeline(status: Optional[str] = Query(None), campaign_id: Optional[int] = Query(None),
                  q: Optional[str] = Query(None, max_length=120), mine: bool = Query(False),
                  tenant_id: str = Depends(require_tenant)):
    status = status or None
    _check_status(status)
    me = current_user()
    my_email = me["email"] if me else "(nobody)"
    everything = _items(tenant_id)
    counts = {s: 0 for s in STAGES}
    for r in everything:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    campaigns = []
    for r in everything:
        if not campaigns or campaigns[-1]["id"] != r["campaign_id"]:
            campaigns.append({"id": r["campaign_id"], "name": r["campaign_name"]})
    return {"stages": list(STAGES), "counts": counts, "campaigns": campaigns,
            "total": len(everything),
            "mine_count": sum(1 for r in everything if r["assignee"] == my_email),
            "members": [u["email"] for u in repo.users_for_tenant(tenant_id)],
            "items": _filter(everything, status, campaign_id, q, my_email if mine else None)}


class PipelineUpdate(BaseModel):
    status: Optional[str] = None
    fee: Optional[float] = Field(None, ge=0, le=10_000_000)
    note: Optional[str] = Field(None, max_length=1000)
    content_url: Optional[str] = Field(None, max_length=500)
    contact: Optional[str] = Field(None, max_length=200)
    assignee: Optional[str] = Field(None, max_length=254)      # a workspace member's email; "" clears


@router.put("/pipeline/{campaign_id}/{kol_id}")
def update_pipeline(campaign_id: int, kol_id: int, req: PipelineUpdate,
                    tenant_id: str = Depends(require_tenant)):
    _check_status(req.status)
    c = repo.campaigns_get(tenant_id, campaign_id)
    if c is None or not any(k.get("id") == kol_id for k in c.get("selected_kols") or []):
        raise HTTPException(status_code=404, detail="That creator is not part of this campaign")
    url = (req.content_url or "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="The content link must start with http:// or https://")

    assignee = (req.assignee or "").strip().lower()
    if assignee and not any(u["email"] == assignee for u in repo.users_for_tenant(tenant_id)):
        raise HTTPException(status_code=422, detail="You can only assign someone in this workspace")

    pipeline = dict(c.get("pipeline") or {})
    e = dict(pipeline.get(str(kol_id)) or {"status": "shortlisted", "history": []})
    changes = req.model_dump(exclude_unset=True)
    name = next(k.get("name", "") for k in c["selected_kols"] if k.get("id") == kol_id)
    if "assignee" in changes and assignee != (e.get("assignee") or ""):
        e["assignee"] = assignee
        activity.record(tenant_id, "collab.assign", name, assignee or "unassigned")
    if req.status and req.status != e.get("status"):
        activity.record(tenant_id, "collab.stage", name, f"{e.get('status', 'shortlisted')} -> {req.status}")
        e["history"] = (e.get("history") or [])[-(_HISTORY_MAX - 1):] + [{"at": _now(), "status": req.status}]
        e["status"] = req.status
    for field in ("fee", "note", "contact"):
        if field in changes:
            value = changes[field]
            e[field] = value.strip() if isinstance(value, str) else value
    if "content_url" in changes:
        e["content_url"] = url
    e["updated_at"] = _now()
    pipeline[str(kol_id)] = e
    c["pipeline"] = pipeline
    repo.campaigns_put(tenant_id, campaign_id, c)
    return next(r for r in _items(tenant_id) if r["campaign_id"] == campaign_id and r["kol_id"] == kol_id)


def _csv_cell(v) -> str:
    """Neutralise spreadsheet formulas in user-entered text."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


@router.get("/pipeline/export.csv")
def export_pipeline(status: Optional[str] = Query(None), campaign_id: Optional[int] = Query(None),
                    q: Optional[str] = Query(None, max_length=120),
                    tenant_id: str = Depends(require_tenant)):
    status = status or None
    _check_status(status)
    rows = _filter(_items(tenant_id), status, campaign_id, q)
    cols = ["campaign_name", "name", "country", "category", "tier", "followers", "status",
            "planned_cost", "fee", "predicted_gmv", "contact", "content_url", "note", "updated_at"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([_csv_cell(r[c]) for c in cols])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="tiko-collaborations.csv"'})


# ── Analytics ─────────────────────────────────────────────────────
def _month_keys(n: int) -> list:
    now = datetime.now(timezone.utc)
    y, m = now.year, now.month
    keys = []
    for _ in range(n):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return keys[::-1]


def _delta(cur: float, prev: float) -> Optional[float]:
    """Month-on-month change in percent; None when there is no base to compare."""
    if not prev:
        return None
    return round((cur - prev) / prev * 100, 1)


def analytics_snapshot(tenant_id: str) -> dict:
    campaigns = repo.campaigns_all(tenant_id)
    done = [c for c in campaigns if c.get("actual_total_gmv") is not None]
    months = _month_keys(6)
    series = {k: {"month": k, "campaigns": 0, "spend": 0.0, "predicted_gmv": 0.0, "actual_gmv": 0.0}
              for k in months}
    by_category, creators = {}, {}
    for c in campaigns:
        key = (c.get("created_at") or "")[:7]
        if key in series:
            s = series[key]
            s["campaigns"] += 1
            s["spend"] += c.get("total_cost") or 0
            s["predicted_gmv"] += c.get("predicted_gmv") or 0
            s["actual_gmv"] += c.get("actual_total_gmv") or 0
        cat = by_category.setdefault(c.get("category") or "other",
                                     {"category": c.get("category") or "other", "campaigns": 0,
                                      "predicted_gmv": 0.0, "actual_gmv": 0.0})
        cat["campaigns"] += 1
        cat["predicted_gmv"] += c.get("predicted_gmv") or 0
        cat["actual_gmv"] += c.get("actual_total_gmv") or 0
        actual_by_id = {a.get("id"): a.get("actual_gmv") or 0 for a in c.get("kol_actuals") or []}
        for k in c.get("selected_kols") or []:
            row = creators.setdefault(k.get("id"), {"id": k.get("id"), "name": k.get("name", ""),
                                                    "country": k.get("country", ""), "campaigns": 0,
                                                    "predicted_gmv": 0.0, "actual_gmv": 0.0, "spend": 0.0})
            row["campaigns"] += 1
            row["predicted_gmv"] += k.get("predicted_gmv") or 0
            row["actual_gmv"] += actual_by_id.get(k.get("id"), 0)
            row["spend"] += k.get("cost") or 0

    items = _items(tenant_id)
    funnel = {s: 0 for s in STAGES}
    for r in items:
        funnel[r["status"]] += 1
    committed = [r for r in items if r["status"] in _COMMITTED]
    spend = sum(c.get("total_cost") or 0 for c in campaigns)
    predicted = sum(c.get("predicted_gmv") or 0 for c in campaigns)
    actual = sum(c.get("actual_total_gmv") or 0 for c in done)
    done_spend = sum(c.get("total_cost") or 0 for c in done)
    accuracies = [c["accuracy_pct"] for c in done if c.get("accuracy_pct") is not None]
    cur, prev = series[months[-1]], series[months[-2]]
    # Rank by real sales when any exist, otherwise by what the engine predicted.
    rank_key = "actual_gmv" if any(v["actual_gmv"] for v in creators.values()) else "predicted_gmv"
    top = sorted(creators.values(), key=lambda v: v[rank_key], reverse=True)[:8]
    for s in list(series.values()) + list(by_category.values()) + top:
        for f in ("spend", "predicted_gmv", "actual_gmv"):
            if f in s:
                s[f] = round(s[f], 2)
    return {
        "kpis": {
            "creators": repo.kols_count(tenant_id),
            "campaigns": len(campaigns),
            "campaigns_active": len(campaigns) - len(done),
            "campaigns_completed": len(done),
            "planned_spend": round(spend, 2),
            "predicted_gmv": round(predicted, 2),
            "actual_gmv": round(actual, 2),
            "predicted_roi": round(predicted / spend, 2) if spend else None,
            "actual_roi": round(actual / done_spend, 2) if done_spend else None,
            "avg_accuracy_pct": round(sum(accuracies) / len(accuracies), 1) if accuracies else None,
            "collaborations": len(items),
            "committed_fees": round(sum(r["fee"] or 0 for r in committed), 2),
            "acceptance_rate_pct": round(len(committed) / len(items) * 100, 1) if items else None,
        },
        "deltas": {
            "campaigns": _delta(cur["campaigns"], prev["campaigns"]),
            "predicted_gmv": _delta(cur["predicted_gmv"], prev["predicted_gmv"]),
            "actual_gmv": _delta(cur["actual_gmv"], prev["actual_gmv"]),
            "spend": _delta(cur["spend"], prev["spend"]),
        },
        "monthly": [series[k] for k in months],
        "funnel": funnel,
        "categories": sorted(by_category.values(), key=lambda v: v["predicted_gmv"], reverse=True),
        "top_creators": top,
        "top_ranked_by": rank_key,
    }


@router.get("/analytics")
def analytics(tenant_id: str = Depends(require_tenant)):
    return analytics_snapshot(tenant_id)


# ── AI helpers (shared copilot LLM + quota) ───────────────────────
def _ask_llm(tenant_id: str, system: str, user: str) -> str:
    """One metered LLM call. The quota is charged up front and refunded if no
    answer comes back, exactly as a copilot chat turn is."""
    if not agent._API_KEY:
        raise HTTPException(status_code=503,
                            detail="AI features are not set up on this server yet.")
    ratelimit.enforce("agent", tenant_id, limit=20, window=60,
                      detail="You're asking very quickly — give the AI a moment.")
    check_and_consume_agent(tenant_id)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        resp = agent._llm([{"role": "system", "content": system},
                           {"role": "user", "content": user}], [])
        text = (resp["choices"][0]["message"].get("content") or "").strip()
        if not text:
            raise KeyError("empty")
    except HTTPException:
        repo.agent_refund(tenant_id, month)
        raise
    except (KeyError, IndexError, TypeError):
        repo.agent_refund(tenant_id, month)
        raise HTTPException(status_code=502, detail="The AI returned an unreadable answer — please try again.")
    return text


def _lang_name(lang: str) -> str:
    return "Simplified Chinese" if (lang or "").lower().startswith("zh") else "English"


class InsightRequest(BaseModel):
    lang: str = Field("en", max_length=8)


@router.post("/analytics/insights")
def analytics_insights(req: InsightRequest, tenant_id: str = Depends(require_tenant)):
    snap = analytics_snapshot(tenant_id)
    if not snap["kpis"]["campaigns"]:
        raise HTTPException(status_code=409,
                            detail="Save at least one campaign first — there is nothing to analyse yet.")
    system = (
        "You are a creator-marketing analyst reviewing one merchant's portfolio. Using ONLY the JSON "
        "data provided, write a short read-out with three sections: (1) what is working, (2) "
        "bottlenecks or risks — look at the outreach funnel, prediction accuracy and ROI, (3) three "
        "concrete next actions. Cite the actual numbers. Never invent figures that are not in the "
        "data; if something cannot be judged from it, say so. Plain text with short bullet points, "
        f"under 220 words. Write in {_lang_name(req.lang)}."
    )
    return {"insights": _ask_llm(tenant_id, system, json.dumps(snap, ensure_ascii=False, default=str))}


class ScriptRequest(BaseModel):
    product: str = Field(..., min_length=2, max_length=200)
    selling_points: str = Field("", max_length=1200)
    format: str = Field("video", max_length=8)             # video | live
    duration_sec: int = Field(30, ge=10, le=600)
    tone: str = Field("", max_length=80)
    creator_id: Optional[int] = None
    lang: str = Field("en", max_length=8)
    script_lang: str = Field("", max_length=40)             # language the creator speaks on camera


@router.post("/scripts/generate")
def generate_script(req: ScriptRequest, tenant_id: str = Depends(require_tenant)):
    if req.format not in ("video", "live"):
        raise HTTPException(status_code=422, detail="format must be video or live")
    creator = repo.kols_get(tenant_id, req.creator_id) if req.creator_id is not None else None
    if req.creator_id is not None and creator is None:
        raise HTTPException(status_code=404, detail="Creator not found")
    brief = {
        "product": req.product.strip(), "selling_points": req.selling_points.strip(),
        "format": "livestream segment" if req.format == "live" else "short video",
        "length_seconds": req.duration_sec, "tone": req.tone.strip() or "natural, energetic",
        "spoken_language": req.script_lang.strip() or _lang_name(req.lang),
    }
    if creator:
        brief["creator"] = {k: creator.get(k) for k in ("name", "country", "category", "followers",
                                                        "age_group", "gender_ratio")}
    system = (
        "You write shoppable short-video and livestream scripts for creators selling products in "
        "Southeast Asia. From the JSON brief, produce a ready-to-shoot script: a hook for the first "
        "3 seconds, then timed beats (timestamp · what is on screen · the spoken line), and a closing "
        "call to action pointing to the product link. Match the length. Use only the selling points "
        "given — do not invent prices, discounts, certifications or medical/earnings claims; where a "
        "fact is needed and missing, write a [placeholder]. Spoken lines are written in the brief's "
        f"spoken_language; headings and directions in {_lang_name(req.lang)}. Plain text, no markdown tables."
    )
    return {"script": _ask_llm(tenant_id, system, json.dumps(brief, ensure_ascii=False))}


class ScriptCheckRequest(BaseModel):
    script: str = Field(..., min_length=20, max_length=6000)
    product: str = Field("", max_length=200)
    lang: str = Field("en", max_length=8)


@router.post("/scripts/check")
def check_script(req: ScriptCheckRequest, tenant_id: str = Depends(require_tenant)):
    system = (
        "You review creator sales scripts before they are filmed. Assess the script the user sends "
        "on: hook strength (first 3 seconds), clarity of the offer, call to action, pacing for the "
        "stated product, and RISKY CLAIMS — absolute or superlative wording, medical or health "
        "promises, guaranteed results or earnings, unverifiable comparisons, missing ad disclosure. "
        "Start with an overall score out of 10 on its own line, then 'Strengths', 'Issues' (quote "
        "the exact risky phrases) and 'Suggested rewrite' for the weakest lines. Be specific and "
        "brief. This is editorial guidance, not legal advice — say so in one closing line. "
        f"Write in {_lang_name(req.lang)}."
    )
    user = (f"Product: {req.product.strip()}\n\n" if req.product.strip() else "") + "Script:\n" + req.script
    return {"review": _ask_llm(tenant_id, system, user)}
