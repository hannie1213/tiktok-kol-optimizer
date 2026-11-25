"""
Workspace: team seats and data portability.

A workspace (tenant) has one owner — the account that registered and pays —
and, up to the plan's seat count, invited members who share the same creator
library, campaigns and quotas. Members can do everything with the data but
cannot buy plans, manage the team, or delete the workspace.

Joining is by invitation only: the owner invites an email address, which
produces a signed 7-day link (emailed when SMTP is on, and always returned to
the owner so it can be passed on by hand). The invitee opens it, chooses a
password, and gets an account inside the owner's workspace. An address that
already has a Lumio account cannot be invited — one account, one workspace.

If a paid term lapses and the workspace then holds more accounts than its
plan has seats, members are locked out (backend/auth.py) until the owner
renews or removes people; the owner and the data are untouched.

Endpoints
---------
  GET  /team                    Members, pending invites, seat usage
  POST /team/invites            Owner: invite an email address
  POST /team/invites/{id}/revoke  Owner: withdraw an invitation
  POST /team/remove             Owner: remove a member
  POST /team/invite-info        Public: what an invitation link is for
  POST /team/accept             Public: join with the link + a new password
  GET  /account/export          Owner: download everything in the workspace
"""
import json
import logging
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from backend import activity, mailer, ratelimit, repo, settings, tenancy
from backend.auth import (
    DEMO_SUFFIX, _EMAIL_RE, _JWT_ALG, _check_password_rules, _lang, _make_token, _pwd,
    current_user, require_owner, require_tenant,
)
from backend.billing import PLANS

router = APIRouter()
log = logging.getLogger("kol.workspace")

_INVITE_TTL = timedelta(days=7)


def _seats(tenant_id: str) -> int:
    return PLANS.get(repo.plan_get(tenant_id), PLANS["free"])["seats"]


def _invite_token(invite: dict) -> str:
    payload = {"sub": invite["tenant_id"], "purpose": "invite", "inv": invite["id"],
               "email": invite["email"],
               "exp": datetime.fromisoformat(invite["created_at"]) + _INVITE_TTL}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=_JWT_ALG)


def _read_invite(token: str) -> dict:
    invalid = HTTPException(status_code=400,
                            detail="This invitation is invalid, expired or was withdrawn — "
                                   "ask the workspace owner for a new one.")
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[_JWT_ALG])
    except jwt.PyJWTError:
        raise invalid
    if payload.get("purpose") != "invite":
        raise invalid
    invite = repo.invite_get(int(payload.get("inv") or 0))
    if (not invite or invite["status"] != "pending"
            or invite["tenant_id"] != payload.get("sub") or invite["email"] != payload.get("email")):
        raise invalid
    return invite


def _link(invite: dict, request: Request) -> str:
    return f"{mailer.base_url(request)}/#invite={_invite_token(invite)}"


class InviteRequest(BaseModel):
    email: str = Field(..., max_length=254)


class MemberRef(BaseModel):
    email: str = Field(..., max_length=254)


class TokenOnly(BaseModel):
    token: str = Field(..., max_length=2000)


class AcceptRequest(BaseModel):
    token: str = Field(..., max_length=2000)
    password: str


# ── Team ──────────────────────────────────────────────────────────
@router.get("/team")
def team(request: Request, tenant_id: str = Depends(require_tenant)):
    me = current_user()
    is_owner = not me or me["role"] != "member"
    members = repo.users_for_tenant(tenant_id)
    invites = repo.invites_pending(tenant_id)
    return {
        "seats": _seats(tenant_id),
        "used": len(members) + len(invites),
        "can_manage": is_owner and not (me and me["email"].endswith(DEMO_SUFFIX)),
        "members": [{"email": u["email"], "role": u["role"], "joined_at": u["created_at"],
                     "you": bool(me and u["email"] == me["email"])} for u in members],
        # Only the owner sees (and can re-copy) invitation links.
        "invites": [{"id": i["id"], "email": i["email"], "created_at": i["created_at"],
                     "link": _link(i, request)} for i in invites] if is_owner else [],
    }


@router.post("/team/invites")
def invite(req: InviteRequest, request: Request, background: BackgroundTasks,
           tenant_id: str = Depends(require_owner)):
    owner = current_user()
    if not owner or owner["email"].endswith(DEMO_SUFFIX):
        raise HTTPException(status_code=403,
                            detail="Create a free account first — demo sessions can't invite a team.")
    ratelimit.enforce("team-invite", tenant_id, limit=20, window=3600)
    email = req.email.strip().lower()
    if not _EMAIL_RE.match(email) or email.endswith(DEMO_SUFFIX):
        raise HTTPException(status_code=422, detail="Please enter a valid email address")

    members = repo.users_for_tenant(tenant_id)
    invites = repo.invites_pending(tenant_id)
    if any(u["email"] == email for u in members):
        raise HTTPException(status_code=409, detail="That person is already in this workspace.")
    for i in invites:
        if i["email"] == email:                       # asking again just returns the link
            return {"invite": {"id": i["id"], "email": email, "link": _link(i, request)},
                    "emailed": False}
    if repo.user_find(email):
        raise HTTPException(status_code=409,
                            detail="This email already has its own Lumio account, so it can't join another workspace.")
    seats = _seats(tenant_id)
    if len(members) + len(invites) >= seats:
        raise HTTPException(status_code=402,
                            detail=f"Your plan includes {seats} seat{'s' if seats != 1 else ''} "
                                   "and they are all taken — upgrade to add more people.")

    inv = repo.invite_create(tenant_id, email, owner["email"],
                             datetime.now(timezone.utc).isoformat())
    link = _link(inv, request)
    emailed = mailer.enabled()
    if emailed:
        background.add_task(mailer.send_team_invite, email, owner["email"], link, _lang(request))
    log.info("team invite created tenant=%s", tenant_id)
    activity.record(tenant_id, "team.invite", email)
    return {"invite": {"id": inv["id"], "email": email, "link": link}, "emailed": emailed}


@router.post("/team/invites/{invite_id}/revoke")
def revoke(invite_id: int, tenant_id: str = Depends(require_owner)):
    inv = repo.invite_get(invite_id)
    if not inv or inv["tenant_id"] != tenant_id or not repo.invite_close(invite_id, "revoked"):
        raise HTTPException(status_code=404, detail="Invitation not found")
    return {"status": "ok"}


@router.post("/team/remove")
def remove_member(req: MemberRef, tenant_id: str = Depends(require_owner)):
    """Remove a member. Their account is deleted, which signs them out at once;
    the workspace data they worked on stays."""
    target = repo.user_find(req.email.strip().lower())
    if not target or target["tenant_id"] != tenant_id:
        raise HTTPException(status_code=404, detail="No such member in this workspace")
    if target["role"] != "member":
        raise HTTPException(status_code=422, detail="The workspace owner can't be removed.")
    repo.user_delete(target["email"])
    activity.record(tenant_id, "team.remove", target["email"])
    log.info("team member removed tenant=%s", tenant_id)
    return {"status": "ok"}


@router.post("/team/invite-info")
def invite_info(req: TokenOnly, request: Request):
    ratelimit.enforce("invite-ip", ratelimit.client_ip(request), limit=30, window=900)
    inv = _read_invite(req.token)
    return {"email": inv["email"], "invited_by": inv["invited_by"]}


@router.post("/team/accept")
def accept(req: AcceptRequest, request: Request):
    """Join the workspace: creates the invitee's account and signs them in."""
    ratelimit.enforce("invite-ip", ratelimit.client_ip(request), limit=30, window=900)
    inv = _read_invite(req.token)
    _check_password_rules(req.password)
    if repo.user_find(inv["email"]):
        raise HTTPException(status_code=409,
                            detail="This email already has a Lumio account — sign in instead.")
    if not tenancy.tenant_exists(inv["tenant_id"]):
        raise HTTPException(status_code=400, detail="This workspace no longer exists.")
    # The plan may have changed since the invitation was sent.
    if len(repo.users_for_tenant(inv["tenant_id"])) >= _seats(inv["tenant_id"]):
        raise HTTPException(status_code=402,
                            detail="This workspace has no free seat right now — "
                                   "ask the owner to upgrade or free one up.")
    if not repo.invite_close(inv["id"], "accepted"):   # claimed first: a link works once
        raise HTTPException(status_code=400, detail="This invitation was already used.")

    password_hash = _pwd.hash(req.password)
    repo.user_create(email=inv["email"], password_hash=password_hash,
                     tenant_id=inv["tenant_id"],
                     created_at=datetime.now(timezone.utc).isoformat(),
                     lang=_lang(request), role="member",
                     # The link only proves the address when we emailed it ourselves.
                     email_verified=mailer.enabled())
    log.info("team invite accepted tenant=%s", inv["tenant_id"])
    return {"token": _make_token(inv["tenant_id"], inv["email"], password_hash),
            "email": inv["email"], "invited_by": inv["invited_by"]}


# ── Data portability ──────────────────────────────────────────────
@router.get("/account/export")
def export_workspace(tenant_id: str = Depends(require_owner)):
    """Everything the workspace holds, as one JSON file — the merchant's data
    is theirs to take away (and it doubles as a personal backup)."""
    ratelimit.enforce("export", tenant_id, limit=10, window=3600)
    plan = repo.plan_info(tenant_id)
    doc = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "workspace": {"plan": plan["plan"], "plan_expires_at": plan["expires_at"]},
        "members": [{"email": u["email"], "role": u["role"], "joined_at": u["created_at"]}
                    for u in repo.users_for_tenant(tenant_id)],
        "creators": repo.kols_all(tenant_id),
        "campaigns": repo.campaigns_all(tenant_id),
        "calibration": repo.calibration_get(tenant_id),
        "message_templates": repo.docs_list(tenant_id, "msg_template"),
        "contract_templates": repo.docs_list(tenant_id, "contract_template"),
        "contracts": repo.docs_list(tenant_id, "contract"),
        "messages": repo.docs_list(tenant_id, "message", limit=2000),
        "orders": [{k: o[k] for k in ("order_no", "plan", "months", "amount", "currency",
                                      "status", "reference", "created_at", "decided_at")}
                   for o in repo.orders_for_tenant(tenant_id, limit=500)],
    }
    name = f"lumio-export-{datetime.now(timezone.utc):%Y%m%d}.json"
    return Response(json.dumps(doc, ensure_ascii=False, indent=2, default=str),
                    media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
