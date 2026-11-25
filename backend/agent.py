"""
AI campaign copilot — an LLM agent with tool access to the optimizer.

POST /agent/chat runs a function-calling loop against an OpenAI-compatible
endpoint (DeepSeek by default). The model can call internal tools — run an
optimization, browse the creator library, review campaigns, check the plan —
and then explains the results in the merchant's language.

Configuration (env vars)
------------------------
  DEEPSEEK_API_KEY   API key (or AGENT_API_KEY). Unset → /agent/chat returns 503.
  AGENT_BASE_URL     OpenAI-compatible base URL (default https://api.deepseek.com)
  AGENT_MODEL        Model name (default deepseek-chat)

No extra Python dependencies: HTTP via stdlib urllib.
"""
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import ratelimit, repo
from backend.auth import require_tenant
from backend.billing import PLANS, check_and_consume_agent

router = APIRouter()
log = logging.getLogger("kol.agent")

_API_KEY = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("AGENT_API_KEY", "")
_BASE_URL = os.environ.get("AGENT_BASE_URL", "https://api.deepseek.com").rstrip("/")
_MODEL = os.environ.get("AGENT_MODEL", "deepseek-chat")
_MAX_ROUNDS = 6          # tool-call iterations per request
_HISTORY_LIMIT = 16      # client messages kept as context


# ── Tool schemas (OpenAI function-calling format) ─────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_optimization",
            "description": "Run the KOL portfolio optimizer on the merchant's own creator library. "
                           "Returns the best lineup, predicted GMV, cost, ROI and winning algorithm. "
                           "Consumes one optimization run from the merchant's monthly quota.",
            "parameters": {
                "type": "object",
                "properties": {
                    "budget": {"type": "number", "description": "Marketing budget in USD (100–200000)"},
                    "countries": {"type": "array", "items": {"type": "string", "enum": ["MY", "ID", "TH", "PH", "SG", "VN"]},
                                  "description": "Target markets; empty = all countries"},
                    "category": {"type": "string", "enum": ["beauty", "fashion", "accessories", "muslim_fashion", "home", "kitchen", "appliances", "electronics", "mother_baby", "fmcg", "health", "sports_outdoor", "pet", "toys", "automotive"]},
                    "content_type": {"type": "string", "enum": ["all", "live", "video"],
                                     "description": "Campaign format: 'live' = livestream sellers only, "
                                                    "'video' = short-video creators only, 'all' (default) = no restriction"},
                },
                "required": ["budget", "category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_creators",
            "description": "Browse the merchant's creator library (optionally filtered).",
            "parameters": {
                "type": "object",
                "properties": {
                    "country": {"type": "string", "enum": ["MY", "ID", "TH", "PH", "SG", "VN"]},
                    "category": {"type": "string", "enum": ["beauty", "fashion", "accessories", "muslim_fashion", "home", "kitchen", "appliances", "electronics", "mother_baby", "fmcg", "health", "sports_outdoor", "pet", "toys", "automotive"]},
                    "content_type": {"type": "string", "enum": ["live", "video"],
                                     "description": "Only livestream sellers ('live') or short-video creators ('video'); omit for all"},
                    "limit": {"type": "integer", "description": "Max creators to return (default 12)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_campaigns",
            "description": "List the merchant's saved campaigns with predicted vs actual GMV and accuracy.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plan_budget",
            "description": "Advise how to SPLIT a budget across creator tiers (anchor / core / test roles) "
                           "based on the merchant's own library, BEFORE running an optimization. "
                           "Free — does not consume optimization quota. Use when the merchant asks "
                           "'how should I spend/allocate my budget' rather than 'give me a lineup'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "budget": {"type": "number", "description": "Marketing budget in USD (100–200000)"},
                    "countries": {"type": "array", "items": {"type": "string", "enum": ["MY", "ID", "TH", "PH", "SG", "VN"]}},
                    "category": {"type": "string", "enum": ["beauty", "fashion", "accessories", "muslim_fashion", "home", "kitchen", "appliances", "electronics", "mother_baby", "fmcg", "health", "sports_outdoor", "pet", "toys", "automotive"]},
                    "content_type": {"type": "string", "enum": ["all", "live", "video"]},
                },
                "required": ["budget", "category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "merchant_guide",
            "description": "Curated TikTok Shop SEA merchant playbooks distilled from official platform guidance. "
                           "Use for questions about opening a store (入驻/开店), choosing products (选品), "
                           "country/market characteristics, mega-sale & livestream tactics (大促/直播打法), "
                           "or lessons from successful merchants (案例). Free — consumes no quota.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string",
                              "enum": ["store_setup", "product_selection", "markets", "campaign_playbook", "case_lessons"],
                              "description": "store_setup=开店指南, product_selection=选品指南, markets=五国市场洞察, "
                                             "campaign_playbook=大促与内容打法, case_lessons=标杆案例经验"},
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_real_lookalikes",
            "description": "Map creators from the library to the closest REAL creators (imported from "
                           "FastMoss, with a TikTok profile link the merchant can actually contact). "
                           "Use after run_optimization when the lineup contains synthetic placeholder "
                           "creators (source='synthetic') — they cannot be contacted, so offer the "
                           "merchant real alternatives with similar market, category, tier and "
                           "engagement. Free — consumes no quota.",
            "parameters": {
                "type": "object",
                "properties": {
                    "names": {"type": "array", "items": {"type": "string"},
                              "description": "Creator names exactly as returned by run_optimization or list_creators"},
                    "limit": {"type": "integer", "description": "Real matches to return per creator (default 3, max 5)"},
                },
                "required": ["names"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_billing",
            "description": "The merchant's current plan and this month's optimization usage/quota.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


# ── Tool implementations ──────────────────────────────────────────
def _tool_run_optimization(tenant_id: str, args: dict) -> dict:
    from backend import main as m   # deferred import (main imports this module)
    budget = max(100, min(200000, float(args.get("budget", 5000))))
    req = m.OptimizeRequest(
        budget=budget,
        countries=[c for c in (args.get("countries") or []) if c],
        category=args.get("category", "beauty"),
        content_type=args.get("content_type") or "all",
    )
    res = m.optimize(req, tenant_id)
    if hasattr(res, "model_dump"):
        res = res.model_dump()
    kols = sorted(res.get("selected_kols", []), key=lambda k: -(k.get("expected_gmv") or 0))
    return {
        "budget": budget,
        "total_cost": res.get("total_cost"),
        "predicted_gmv": res.get("total_gmv"),
        "roi": res.get("roi"),
        "best_algorithm": res.get("best_algorithm"),
        "candidates": res.get("candidates"),
        "selected_count": len(kols),
        "synthetic_selected": sum(1 for k in kols if k.get("source") == "synthetic"),
        "top_creators": [
            {"name": k.get("name"), "country": k.get("country"), "tier": k.get("tier"),
             "followers": k.get("followers"), "cost": k.get("cost"),
             "expected_gmv": k.get("expected_gmv"), "source": k.get("source")}
            for k in kols[:8]
        ],
    }


def _tool_list_creators(tenant_id: str, args: dict) -> dict:
    countries = [args["country"]] if args.get("country") else None
    rows = repo.kols_all(tenant_id, countries=countries, category=args.get("category"),
                         content_type=args.get("content_type"))
    limit = max(1, min(25, int(args.get("limit", 12))))
    rows = sorted(rows, key=lambda k: -(k.get("followers") or 0))[:limit]
    return {
        "total_matching": len(rows),
        "creators": [
            {"name": k.get("name"), "country": k.get("country"), "category": k.get("category"),
             "content_type": k.get("content_type") or "both",
             "followers": k.get("followers"), "engagement_rate": k.get("engagement_rate"),
             "cost": k.get("cost")}
            for k in rows
        ],
    }


def _tool_get_campaigns(tenant_id: str, args: dict) -> dict:
    rows = repo.campaigns_all(tenant_id)
    return {
        "count": len(rows),
        "campaigns": [
            {"name": c.get("name"), "status": c.get("status"), "budget": c.get("budget"),
             "predicted_gmv": c.get("predicted_gmv"), "actual_gmv": c.get("actual_total_gmv"),
             "accuracy_pct": c.get("accuracy_pct"), "created_at": c.get("created_at")}
            for c in rows[:12]
        ],
    }


def _tool_plan_budget(tenant_id: str, args: dict) -> dict:
    from backend import main as m   # deferred import (main imports this module)
    budget = max(100, min(200000, float(args.get("budget", 5000))))
    return m.compute_budget_plan(
        tenant_id, budget,
        [c for c in (args.get("countries") or []) if c],
        args.get("category", "beauty"),
        args.get("content_type") or "all",
    )


def _tool_merchant_guide(tenant_id: str, args: dict) -> dict:
    from backend.guide import guide_topic
    try:
        return guide_topic(args.get("topic", "store_setup"), lang="zh")
    except KeyError:
        return {"error": f"Unknown topic: {args.get('topic')}"}


def _tier(followers) -> str:
    f = followers or 0
    return "Mega" if f >= 1_000_000 else "Macro" if f >= 100_000 else "Micro" if f >= 10_000 else "Nano"


def _similarity(target: dict, cand: dict) -> float:
    """Profile closeness between a placeholder creator and a real candidate.
    Country and tier dominate; follower scale and engagement break ties."""
    import math
    score = 0.0
    if cand.get("country") == target.get("country"):
        score += 2.0
    if cand.get("category") == target.get("category"):
        score += 1.5
    if _tier(cand.get("followers")) == _tier(target.get("followers")):
        score += 1.0
    ta, tb = target.get("content_type") or "both", cand.get("content_type") or "both"
    if ta == tb or "both" in (ta, tb):
        score += 0.5
    ratio = max(1, cand.get("followers") or 1) / max(1, target.get("followers") or 1)
    score -= min(2.0, abs(math.log10(ratio)))
    score -= min(1.0, abs((cand.get("engagement_rate") or 0) - (target.get("engagement_rate") or 0)) * 10)
    return score


def _tool_find_real_lookalikes(tenant_id: str, args: dict) -> dict:
    rows = repo.kols_all(tenant_id)
    by_name = {str(k.get("name", "")).lower(): k for k in rows}
    real_pool = [k for k in rows
                 if (k.get("source") or "fastmoss") == "fastmoss" and k.get("tiktok_url")]
    limit = max(1, min(5, int(args.get("limit", 3))))

    results = []
    for name in (args.get("names") or [])[:10]:
        target = by_name.get(str(name).lower())
        if target is None:
            results.append({"name": name, "error": "not found in the creator library"})
            continue
        if (target.get("source") or "fastmoss") == "fastmoss" and target.get("tiktok_url"):
            results.append({"name": target["name"], "already_real": True,
                            "tiktok_url": target["tiktok_url"]})
            continue
        ranked = sorted(real_pool, key=lambda k: -_similarity(target, k))[:limit]
        results.append({
            "name": target["name"],
            "profile": {"country": target.get("country"), "category": target.get("category"),
                        "tier": _tier(target.get("followers")), "followers": target.get("followers"),
                        "engagement_rate": target.get("engagement_rate")},
            "real_matches": [
                {"name": k.get("name"), "country": k.get("country"), "category": k.get("category"),
                 "tier": _tier(k.get("followers")), "followers": k.get("followers"),
                 "engagement_rate": k.get("engagement_rate"), "cost": k.get("cost"),
                 "tiktok_url": k.get("tiktok_url")}
                for k in ranked
            ],
        })
    return {"real_creators_in_library": len(real_pool), "results": results}


def _tool_get_billing(tenant_id: str, args: dict) -> dict:
    plan_id = repo.plan_get(tenant_id)
    limits = PLANS.get(plan_id, PLANS["free"])
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    return {
        "plan": plan_id,
        "plan_name": limits["name"],
        "monthly_optimization_limit": limits["monthly_optimizations"],
        "optimizations_used_this_month": repo.optimize_runs(tenant_id, month),
        "monthly_copilot_chat_limit": limits["monthly_agent_messages"],
        "copilot_chats_used_this_month": repo.agent_messages(tenant_id, month),
        "creators_in_library": repo.kols_count(tenant_id),
    }


_DISPATCH = {
    "run_optimization": _tool_run_optimization,
    "list_creators": _tool_list_creators,
    "find_real_lookalikes": _tool_find_real_lookalikes,
    "plan_budget": _tool_plan_budget,
    "merchant_guide": _tool_merchant_guide,
    "get_campaigns": _tool_get_campaigns,
    "get_billing": _tool_get_billing,
}


# ── LLM call (stdlib HTTP, OpenAI-compatible) ─────────────────────
def _llm(messages: list, tools: list) -> dict:
    body = json.dumps({
        "model": _MODEL,
        "messages": messages,
        "tools": tools,
        "temperature": 0.3,
    }).encode("utf-8")
    request = urllib.request.Request(
        _BASE_URL + "/chat/completions",
        data=body,
        headers={"Authorization": "Bearer " + _API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # The upstream body can carry provider/account details — log it, and
        # give the merchant a neutral message.
        log.error("LLM HTTP %s: %s", e.code, e.read().decode("utf-8", "replace")[:500])
        raise HTTPException(status_code=502,
                            detail="The AI copilot is temporarily unavailable — please try again shortly.")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        log.error("LLM unreachable: %s", e)
        raise HTTPException(status_code=502,
                            detail="The AI copilot is temporarily unavailable — please try again shortly.")


def _system_prompt(tenant_id: str) -> str:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    plan = repo.plan_get(tenant_id)
    limits = PLANS.get(plan, PLANS["free"])
    return (
        "You are Lumio's campaign copilot inside a TikTok Shop KOL optimization console. "
        "The merchant assembles creator (KOL) lineups for SEA markets (MY, ID, TH, PH, SG, VN) "
        "across 15 categories (beauty, fashion, accessories, muslim_fashion, home, kitchen, "
        "appliances, electronics, mother_baby, fmcg, health, sports_outdoor, pet, toys, "
        "automotive); budgets and GMV are in USD. "
        "Creators are tagged by selling format — live (直播带货), video (短视频种草) or both — "
        "pass content_type when the merchant asks for livestream hosts or video creators specifically.\n"
        f"Merchant context: plan={limits['name']}, creators_in_library={repo.kols_count(tenant_id)}, "
        f"optimizations_used_{month}={repo.optimize_runs(tenant_id, month)}"
        f"/{limits['monthly_optimizations'] if limits['monthly_optimizations'] is not None else 'unlimited'}, "
        f"copilot_chats_used_{month}={repo.agent_messages(tenant_id, month)}"
        f"/{limits['monthly_agent_messages'] if limits['monthly_agent_messages'] is not None else 'unlimited'}.\n"
        "Rules:\n"
        "- Use the tools to answer with real data; never invent numbers. One question usually needs at most "
        "one or two tool calls.\n"
        "- run_optimization consumes quota — call it only when the merchant clearly wants a lineup/plan; "
        "confirm budget and category from their message (default budget $5000 only if they gave none).\n"
        "- Creators with source='synthetic' are demo placeholders that cannot be contacted. When a "
        "lineup includes them and the merchant wants people they can actually reach, call "
        "find_real_lookalikes with those names and present the real alternatives WITH their "
        "tiktok_url links. Never present a synthetic creator as contactable.\n"
        "- Answer in the same language the merchant writes in (中文 or English). The reply shows in a "
        "narrow chat panel, so keep it compact: a one-line summary, then key numbers as short lines, "
        "then one actionable suggestion. Formatting: use ONLY short plain lines, '- ' for list items, "
        "and **bold** for labels/emphasis. Do NOT use markdown headings (#), horizontal rules (---), or "
        "tables, and avoid blank lines except at most one between sections.\n"
        "- If a tool returns an error about quota (402), explain the plan limit and suggest upgrading."
    )


# ── Endpoint ──────────────────────────────────────────────────────
class ChatMessage(BaseModel):
    role: str = Field(..., max_length=16)
    content: str = Field(..., max_length=8000)


class ChatRequest(BaseModel):
    messages: List[ChatMessage] = Field(..., min_length=1, max_length=60)


@router.post("/agent/chat")
def chat(req: ChatRequest, tenant_id: str = Depends(require_tenant)):
    if not _API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Agent not configured — set DEEPSEEK_API_KEY (and optionally AGENT_BASE_URL / AGENT_MODEL).",
        )
    # Meter this chat turn against the plan's copilot quota (402 → upgrade page).
    # Charged once per user message, before the LLM round-trip; a run_optimization
    # tool call inside the turn still separately consumes an optimization run.
    ratelimit.enforce("agent", tenant_id, limit=20, window=60,
                      detail="You're sending messages very quickly — give the copilot a moment.")
    check_and_consume_agent(tenant_id)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    history = [m.model_dump() for m in req.messages if m.role in ("user", "assistant")][-_HISTORY_LIMIT:]
    messages = [{"role": "system", "content": _system_prompt(tenant_id)}] + history
    events = []

    for _ in range(_MAX_ROUNDS):
        try:
            resp = _llm(messages, TOOLS)
            try:
                msg = resp["choices"][0]["message"]
            except (KeyError, IndexError, TypeError):
                raise HTTPException(status_code=502, detail="The AI copilot returned an unreadable "
                                                            "answer — please try again.")
        except HTTPException:
            # The merchant got no answer for this turn — don't charge for it.
            repo.agent_refund(tenant_id, month)
            raise
        messages.append(msg)

        calls = msg.get("tool_calls")
        if not calls:
            return {"reply": msg.get("content") or "", "events": events}

        for call in calls:
            name = call.get("function", {}).get("name", "")
            try:
                args = json.loads(call.get("function", {}).get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            events.append({"tool": name, "args": args})
            fn = _DISPATCH.get(name)
            if fn is None:
                result = {"error": f"Unknown tool: {name}"}
            else:
                try:
                    result = fn(tenant_id, args)
                except HTTPException as e:
                    result = {"error": e.detail, "status": e.status_code}
                except Exception as e:                      # tool crash must not kill the chat
                    result = {"error": str(e)[:300]}
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "content": json.dumps(result, ensure_ascii=False, default=str)[:6000],
            })

    return {"reply": "…", "events": events,
            "warning": "Agent stopped after the maximum number of tool rounds."}
