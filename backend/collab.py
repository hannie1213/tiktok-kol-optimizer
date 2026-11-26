"""
Outreach tools around the collaboration pipeline: reusable templates, the
message log, contracts, the activity trail and the getting-started checklist.

Messages are not sent by this server. A template is filled in for one creator,
the merchant reviews it and sends it from their own WhatsApp or mail app
(click-to-chat / mailto links built in the browser); the server records that
it was sent so the whole team can see who was contacted, when, and with what.

Template variables (same set for messages and contracts):
  {creator_name} {campaign_name} {category} {country} {followers}
  {fee} {planned_cost} {predicted_gmv} {sender} {today}
plus, for contracts only: {deliverables} {start_date} {end_date}

Endpoints
---------
  GET/POST        /templates                 List (seeds starters on first use) / create
  PUT/DELETE      /templates/{id}
  POST            /templates/{id}/render     Fill a template for one collaboration
  GET/POST        /messages                  Message log / record a sent message
  GET/POST        /contracts                 List / generate from a template
  GET/PUT/DELETE  /contracts/{id}
  GET             /activity                  Workspace activity trail
  GET             /onboarding                Getting-started checklist
"""
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend import activity, ratelimit, repo
from backend.auth import current_user, require_tenant
from engine.scoring.explainer import _CATEGORY_NAME, _COUNTRY_NAME

router = APIRouter()

KINDS = {"message": "msg_template", "contract": "contract_template"}
CHANNELS = ("whatsapp", "email", "copy")
CONTRACT_STATUSES = ("draft", "sent", "signed", "cancelled")
_VAR = re.compile(r"\{([a-z_]+)\}")

_STARTERS = {
    "en": {
        "message": [
            ("First invitation",
             "Hi {creator_name}! I'm with {sender}. We love your {category} content and would like to "
             "invite you to our \"{campaign_name}\" campaign in {country}.\n\n"
             "Proposed fee: {fee}. Could you share your rate card and availability this month?"),
            ("Follow-up",
             "Hi {creator_name}, just following up on our \"{campaign_name}\" invitation. "
             "Happy to answer any questions or adjust the brief — let me know what works for you."),
            ("Brief & next steps",
             "Hi {creator_name}, great to have you on \"{campaign_name}\"!\n\n"
             "Agreed fee: {fee}\nNext: we'll send the product and a short brief. "
             "Please share the draft with us before posting."),
        ],
        "contract": [
            ("Creator collaboration agreement (sample)",
             "CREATOR COLLABORATION AGREEMENT\n\nDate: {today}\n\n"
             "Between: {sender} (\"Brand\")\nAnd: {creator_name} (\"Creator\")\n\n"
             "1. Campaign\nThe Creator will take part in the campaign \"{campaign_name}\" ({category}, {country}).\n\n"
             "2. Deliverables\n{deliverables}\n\n"
             "3. Term\nFrom {start_date} to {end_date}.\n\n"
             "4. Fee\nThe Brand will pay the Creator {fee} within 14 days of the deliverables going live.\n\n"
             "5. Content approval\nThe Creator will share drafts for approval before publishing and will "
             "follow the platform's advertising disclosure rules.\n\n"
             "6. Usage rights\nThe Brand may re-share the published content on its own channels for 90 days.\n\n"
             "7. Cancellation\nEither party may cancel in writing before production starts.\n\n"
             "Brand: ____________________    Creator: ____________________\n\n"
             "This is a starting template, not legal advice — have it reviewed for your market before use."),
        ],
    },
    "zh": {
        "message": [
            ("首次邀约",
             "你好 {creator_name}！我是 {sender} 的工作人员。我们很喜欢你的{category}内容，"
             "想邀请你参加我们在 {country} 的「{campaign_name}」投放。\n\n"
             "预计费用：{fee}。方便发一下你的报价单和本月档期吗？"),
            ("跟进提醒",
             "你好 {creator_name}，想跟进一下「{campaign_name}」的合作邀请。"
             "有任何问题或需要调整的地方，随时告诉我。"),
            ("合作说明与下一步",
             "你好 {creator_name}，很高兴你加入「{campaign_name}」！\n\n"
             "谈定费用：{fee}\n接下来我们会寄出样品并发送简要说明，发布前请先把初稿发给我们确认。"),
        ],
        "contract": [
            ("达人合作协议（示例）",
             "达人合作协议\n\n日期：{today}\n\n"
             "甲方（品牌方）：{sender}\n乙方（达人）：{creator_name}\n\n"
             "一、合作项目\n乙方参与「{campaign_name}」投放（{category}，{country}）。\n\n"
             "二、交付内容\n{deliverables}\n\n"
             "三、合作期限\n自 {start_date} 起至 {end_date} 止。\n\n"
             "四、费用\n甲方在交付内容发布后 14 日内向乙方支付 {fee}。\n\n"
             "五、内容审核\n乙方发布前须将初稿提交甲方确认，并遵守平台的广告披露规范。\n\n"
             "六、使用权\n甲方可在自有渠道转发已发布内容，期限 90 天。\n\n"
             "七、取消\n任一方可在制作开始前以书面形式取消合作。\n\n"
             "甲方：____________________    乙方：____________________\n\n"
             "本模板仅供起草参考，不构成法律意见，使用前请结合当地法律审阅。"),
        ],
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lang(request: Request) -> str:
    return "zh" if (request.headers.get("x-lang") or "").lower().startswith("zh") else "en"


def _kind(kind: str) -> str:
    if kind not in KINDS:
        raise HTTPException(status_code=422, detail="kind must be message or contract")
    return KINDS[kind]


def _collab(tenant_id: str, campaign_id: int, kol_id: int) -> tuple:
    c = repo.campaigns_get(tenant_id, campaign_id)
    k = next((k for k in (c or {}).get("selected_kols") or [] if k.get("id") == kol_id), None)
    if not k:
        raise HTTPException(status_code=404, detail="That creator is not part of this campaign")
    return c, k, ((c.get("pipeline") or {}).get(str(kol_id)) or {})


def _money(v) -> str:
    return "$" + format(round(v or 0), ",")


def _values(tenant_id: str, c: dict, k: dict, entry: dict, extra: Optional[dict] = None,
            lang: str = "en") -> dict:
    owner = repo.user_find_by_tenant(tenant_id)
    me = current_user()
    fee = entry.get("fee")
    cat = str(k.get("category") or c.get("category") or "")
    country = k.get("country", "")
    values = {
        "creator_name": k.get("name", ""), "campaign_name": c.get("name", ""),
        # Category and market read naturally in the template's language.
        "category": _CATEGORY_NAME.get(lang, {}).get(cat) or cat.replace("_", " "),
        "country": _COUNTRY_NAME.get(lang, {}).get(country) or country, "followers": format(int(k.get("followers") or 0), ","),
        "fee": _money(fee if fee is not None else k.get("cost")),
        "planned_cost": _money(k.get("cost")), "predicted_gmv": _money(k.get("predicted_gmv")),
        "sender": (me or owner or {}).get("email", ""),
        "today": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    values.update(extra or {})
    return values


def _fill(body: str, values: dict) -> str:
    """Substitute known {variables}; unknown ones are left visible so the
    merchant notices and fixes them rather than sending a blank."""
    return _VAR.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), body)


def _starter_in(row: Optional[dict], kind: str, lang: str) -> Optional[dict]:
    """An untouched starter template follows the interface language.

    Starters are seeded once, in whatever language the console used on that
    first visit. Until the merchant edits one (an edit drops the `starter`
    flag), show it — and fill it — in the language in use now, so a workspace
    that switches to Chinese doesn't keep English starters (and vice versa)."""
    if not row or not row.get("starter"):
        return row
    for src in _STARTERS.values():
        pairs = src.get(kind) or []
        for i, (name, body) in enumerate(pairs):
            if row.get("name") == name and row.get("body") == body:
                name2, body2 = _STARTERS[lang][kind][i]
                return {**row, "name": name2, "body": body2}
    return row


def _seed(tenant_id: str, kind: str, lang: str):
    for name, body in reversed(_STARTERS[lang][kind]):          # newest-first listing keeps this order
        repo.doc_add(tenant_id, KINDS[kind], {"name": name, "body": body, "starter": True}, _now())


# ── Templates ─────────────────────────────────────────────────────
class TemplateIn(BaseModel):
    kind: str = Field("message", max_length=12)
    name: str = Field(..., min_length=1, max_length=80)
    body: str = Field(..., min_length=1, max_length=8000)


@router.get("/templates")
def list_templates(request: Request, kind: str = Query("message"), tenant_id: str = Depends(require_tenant)):
    store = _kind(kind)
    rows = repo.docs_list(tenant_id, store)
    # First visit: give the workspace starter templates in the language in use.
    if not rows and not repo.docs_count(tenant_id, store + "_seeded"):
        repo.doc_add(tenant_id, store + "_seeded", {}, _now())
        _seed(tenant_id, kind, _lang(request))
        rows = repo.docs_list(tenant_id, store)
    rows = [_starter_in(r, kind, _lang(request)) for r in rows]
    return {"templates": rows, "variables": sorted(_values(tenant_id, {}, {}, {}).keys())
            + (["deliverables", "start_date", "end_date"] if kind == "contract" else [])}


@router.post("/templates")
def create_template(req: TemplateIn, tenant_id: str = Depends(require_tenant)):
    store = _kind(req.kind)
    if repo.docs_count(tenant_id, store) >= 100:
        raise HTTPException(status_code=409, detail="Template limit reached — delete some you no longer use.")
    return repo.doc_add(tenant_id, store, {"name": req.name.strip(), "body": req.body}, _now())


@router.put("/templates/{template_id}")
def update_template(template_id: int, req: TemplateIn, tenant_id: str = Depends(require_tenant)):
    saved = repo.doc_put(tenant_id, _kind(req.kind), template_id, {"name": req.name.strip(), "body": req.body})
    if not saved:
        raise HTTPException(status_code=404, detail="Template not found")
    return saved


@router.delete("/templates/{template_id}")
def delete_template(template_id: int, kind: str = Query("message"), tenant_id: str = Depends(require_tenant)):
    if not repo.doc_delete(tenant_id, _kind(kind), template_id):
        raise HTTPException(status_code=404, detail="Template not found")
    return {"deleted": True}


class RenderIn(BaseModel):
    kind: str = Field("message", max_length=12)
    campaign_id: int
    kol_id: int


@router.post("/templates/{template_id}/render")
def render_template(template_id: int, req: RenderIn, request: Request, tenant_id: str = Depends(require_tenant)):
    lang = _lang(request)
    t = _starter_in(repo.doc_get(tenant_id, _kind(req.kind), template_id), req.kind, lang)
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    c, k, entry = _collab(tenant_id, req.campaign_id, req.kol_id)
    return {"text": _fill(t["body"], _values(tenant_id, c, k, entry, lang=lang)), "contact": entry.get("contact") or ""}


# ── Message log ───────────────────────────────────────────────────
class MessageIn(BaseModel):
    campaign_id: int
    kol_id: int
    channel: str = Field("copy", max_length=12)
    body: str = Field(..., min_length=1, max_length=8000)
    template_name: str = Field("", max_length=80)


@router.post("/messages")
def log_message(req: MessageIn, tenant_id: str = Depends(require_tenant)):
    """Record that a message was sent (from the merchant's own app). A first
    message to a shortlisted creator moves them to "invited"."""
    if req.channel not in CHANNELS:
        raise HTTPException(status_code=422, detail=f"channel must be one of: {', '.join(CHANNELS)}")
    ratelimit.enforce("message-log", tenant_id, limit=120, window=3600)
    c, k, entry = _collab(tenant_id, req.campaign_id, req.kol_id)
    me = current_user()
    saved = repo.doc_add(tenant_id, "message", {
        "campaign_id": req.campaign_id, "campaign_name": c.get("name", ""),
        "kol_id": req.kol_id, "creator_name": k.get("name", ""),
        "channel": req.channel, "body": req.body, "template_name": req.template_name.strip(),
        "contact": entry.get("contact") or "", "sender": me["email"] if me else "",
    }, _now())
    activity.record(tenant_id, "message.send", k.get("name", ""), f"{req.channel} · {c.get('name', '')}")

    advanced = False
    if (entry.get("status") or "shortlisted") == "shortlisted":
        pipeline = dict(c.get("pipeline") or {})
        e = dict(pipeline.get(str(req.kol_id)) or {"history": []})
        e["status"] = "invited"
        e["history"] = (e.get("history") or [])[-19:] + [{"at": _now(), "status": "invited"}]
        e["updated_at"] = _now()
        pipeline[str(req.kol_id)] = e
        c["pipeline"] = pipeline
        repo.campaigns_put(tenant_id, req.campaign_id, c)
        advanced = True
    return {"message": saved, "advanced_to_invited": advanced}


@router.get("/messages")
def list_messages(campaign_id: Optional[int] = Query(None), q: Optional[str] = Query(None, max_length=120),
                  tenant_id: str = Depends(require_tenant)):
    rows = repo.docs_list(tenant_id, "message", limit=1000)
    if campaign_id is not None:
        rows = [r for r in rows if r.get("campaign_id") == campaign_id]
    if q:
        needle = q.strip().lower()
        rows = [r for r in rows if needle in (r.get("creator_name", "") + " " + r.get("body", "") + " "
                                              + r.get("campaign_name", "")).lower()]
    return {"messages": rows[:300], "total": len(rows)}


# ── Contracts ─────────────────────────────────────────────────────
class ContractIn(BaseModel):
    campaign_id: int
    kol_id: int
    template_id: int
    deliverables: str = Field("", max_length=1500)
    start_date: str = Field("", max_length=20)
    end_date: str = Field("", max_length=20)


class ContractUpdate(BaseModel):
    status: Optional[str] = None
    body: Optional[str] = Field(None, min_length=1, max_length=12000)


@router.post("/contracts")
def create_contract(req: ContractIn, request: Request, tenant_id: str = Depends(require_tenant)):
    lang = _lang(request)
    zh = lang == "zh"
    t = _starter_in(repo.doc_get(tenant_id, "contract_template", req.template_id), "contract", lang)
    if not t:
        raise HTTPException(status_code=404, detail="Contract template not found")
    if repo.docs_count(tenant_id, "contract") >= 2000:
        raise HTTPException(status_code=409, detail="Contract limit reached.")
    c, k, entry = _collab(tenant_id, req.campaign_id, req.kol_id)
    values = _values(tenant_id, c, k, entry, {
        "deliverables": req.deliverables.strip() or ("[交付内容]" if zh else "[deliverables]"),
        "start_date": req.start_date.strip() or ("[开始日期]" if zh else "[start date]"),
        "end_date": req.end_date.strip() or ("[结束日期]" if zh else "[end date]"),
    }, lang=lang)
    me = current_user()
    number = "C-" + datetime.now(timezone.utc).strftime("%y%m") + f"-{repo.docs_count(tenant_id, 'contract') + 1:03d}"
    saved = repo.doc_add(tenant_id, "contract", {
        "number": number, "status": "draft", "template_name": t["name"],
        "campaign_id": req.campaign_id, "campaign_name": c.get("name", ""),
        "kol_id": req.kol_id, "creator_name": k.get("name", ""),
        "fee": entry.get("fee") if entry.get("fee") is not None else k.get("cost"),
        "body": _fill(t["body"], values), "created_by": me["email"] if me else "",
        "updated_at": _now(),
    }, _now())
    activity.record(tenant_id, "contract.create", k.get("name", ""), number)
    return saved


@router.get("/contracts")
def list_contracts(tenant_id: str = Depends(require_tenant)):
    rows = repo.docs_list(tenant_id, "contract", limit=1000)
    return {"contracts": [{k: v for k, v in r.items() if k != "body"} for r in rows],
            "statuses": list(CONTRACT_STATUSES)}


@router.get("/contracts/{contract_id}")
def get_contract(contract_id: int, tenant_id: str = Depends(require_tenant)):
    doc = repo.doc_get(tenant_id, "contract", contract_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Contract not found")
    return doc


@router.put("/contracts/{contract_id}")
def update_contract(contract_id: int, req: ContractUpdate, tenant_id: str = Depends(require_tenant)):
    doc = repo.doc_get(tenant_id, "contract", contract_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Contract not found")
    if req.status is not None and req.status not in CONTRACT_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of: {', '.join(CONTRACT_STATUSES)}")
    if req.body is not None:
        if doc["status"] == "signed":
            raise HTTPException(status_code=409, detail="A signed contract can't be edited.")
        doc["body"] = req.body
    if req.status and req.status != doc["status"]:
        activity.record(tenant_id, "contract.status", doc.get("creator_name", ""),
                        f"{doc['number']}: {doc['status']} -> {req.status}")
        doc["status"] = req.status
    doc["updated_at"] = _now()
    return repo.doc_put(tenant_id, "contract", contract_id, doc)


@router.delete("/contracts/{contract_id}")
def delete_contract(contract_id: int, tenant_id: str = Depends(require_tenant)):
    doc = repo.doc_get(tenant_id, "contract", contract_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Contract not found")
    if doc["status"] == "signed":
        raise HTTPException(status_code=409, detail="A signed contract can't be deleted — cancel it instead.")
    repo.doc_delete(tenant_id, "contract", contract_id)
    activity.record(tenant_id, "contract.delete", doc.get("creator_name", ""), doc["number"])
    return {"deleted": True}


# ── Activity & onboarding ─────────────────────────────────────────
@router.get("/activity")
def workspace_activity(limit: int = Query(200, ge=1, le=500), tenant_id: str = Depends(require_tenant)):
    return {"entries": activity.recent(tenant_id, limit)}


@router.get("/onboarding")
def onboarding(tenant_id: str = Depends(require_tenant)):
    """What a new workspace should do next, derived from what it has done."""
    campaigns = repo.campaigns_all(tenant_id)
    contacted = any((e.get("status") or "shortlisted") != "shortlisted"
                    for c in campaigns for e in (c.get("pipeline") or {}).values())
    steps = [
        ("creators", repo.kols_count(tenant_id) > 0),
        ("optimize", repo.optimize_runs(tenant_id, datetime.now(timezone.utc).strftime("%Y-%m")) > 0
         or bool(campaigns)),
        ("campaign", bool(campaigns)),
        ("outreach", contacted or repo.docs_count(tenant_id, "message") > 0),
        ("contract", repo.docs_count(tenant_id, "contract") > 0),
        ("results", any(c.get("actual_total_gmv") is not None for c in campaigns)),
        ("team", len(repo.users_for_tenant(tenant_id)) > 1),
    ]
    done = sum(1 for _, ok in steps if ok)
    return {"steps": [{"id": sid, "done": ok} for sid, ok in steps], "done": done, "total": len(steps)}
