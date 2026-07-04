"""
Campaign Attribution — save optimization predictions, record actual GMV, compare.

Endpoints
---------
  POST   /campaigns                   Save a new campaign from optimization results
  GET    /campaigns                   List all campaigns (newest first)
  GET    /campaigns/{id}              Get single campaign detail
  PUT    /campaigns/{id}/actual       Record actual GMV after campaign ends
  DELETE /campaigns/{id}              Delete a campaign

Campaigns persist to the `campaigns` table, scoped per-tenant (backend/repo.py).
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from backend import calibration, repo, tenancy
from backend.auth import require_tenant

router = APIRouter()


# ════════════════════════════════════════════════════════════════
#  Pydantic models
# ════════════════════════════════════════════════════════════════
class KOLPrediction(BaseModel):
    id: int
    name: str
    country: str
    category: str
    followers: int
    cost: float
    predicted_gmv: float
    tier: str = ""


class CampaignCreate(BaseModel):
    name: str
    countries: List[str] = []
    category: str
    budget: float
    best_algorithm: str
    selected_kols: List[KOLPrediction]
    total_cost: float
    predicted_gmv: float


class KOLActual(BaseModel):
    id: int
    actual_gmv: float


class ActualResultsUpdate(BaseModel):
    actual_total_gmv: float
    kol_actuals: Optional[List[KOLActual]] = None   # per-KOL breakdown (optional)
    notes: Optional[str] = None


# ════════════════════════════════════════════════════════════════
#  Endpoints
# ════════════════════════════════════════════════════════════════
@router.post("/campaigns")
def save_campaign(req: CampaignCreate, _t: str = Depends(require_tenant)):
    """Persist an optimization result as a trackable campaign."""
    campaign = {
        "name":           req.name,
        "created_at":     datetime.utcnow().isoformat(),
        "countries":      req.countries,
        "category":       req.category,
        "budget":         req.budget,
        "best_algorithm": req.best_algorithm,
        "selected_kols":  [k.model_dump() for k in req.selected_kols],
        "total_cost":     req.total_cost,
        "predicted_gmv":  req.predicted_gmv,
        # filled after campaign ends:
        "actual_total_gmv": None,
        "kol_actuals":      None,
        "accuracy_pct":     None,
        "notes":            None,
        "completed_at":     None,
        "status":           "active",   # active | completed
    }
    return repo.campaigns_add(tenancy.current_tenant(), campaign)


@router.get("/campaigns")
def list_campaigns(_t: str = Depends(require_tenant)):
    """Return all campaigns sorted newest-first."""
    data = repo.campaigns_all(tenancy.current_tenant())
    data.sort(key=lambda d: d["created_at"], reverse=True)
    return data


@router.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: int, _t: str = Depends(require_tenant)):
    c = repo.campaigns_get(tenancy.current_tenant(), campaign_id)
    if not c:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found")
    return c


@router.put("/campaigns/{campaign_id}/actual")
def record_actual(campaign_id: int, req: ActualResultsUpdate, _t: str = Depends(require_tenant)):
    """
    Record actual GMV after the campaign finishes.
    Computes accuracy_pct = actual / predicted × 100.
    """
    tenant = tenancy.current_tenant()
    c = repo.campaigns_get(tenant, campaign_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Campaign not found")

    c["actual_total_gmv"] = req.actual_total_gmv
    if req.kol_actuals:
        c["kol_actuals"] = [k.model_dump() for k in req.kol_actuals]
    if req.notes:
        c["notes"] = req.notes
    if c["predicted_gmv"] > 0:
        c["accuracy_pct"] = round(req.actual_total_gmv / c["predicted_gmv"] * 100, 1)
    c["status"] = "completed"
    c["completed_at"] = datetime.utcnow().isoformat()

    repo.campaigns_put(tenant, campaign_id, c)

    # Close the loop: fold this outcome into the tenant's GMV calibration so
    # future predictions self-correct — at the campaign level (segment) and,
    # when a per-creator actual breakdown is given, per creator.
    creator_ratios = {}
    if req.kol_actuals:
        predicted_by_id = {k.get("id"): k.get("predicted_gmv") for k in c.get("selected_kols", [])}
        for ka in req.kol_actuals:
            p = predicted_by_id.get(ka.id)
            if p and p > 0 and ka.actual_gmv is not None:
                creator_ratios[ka.id] = ka.actual_gmv / p

    calibration.record_outcome(
        c.get("category"), c.get("countries", []),
        c.get("predicted_gmv"), req.actual_total_gmv,
        creator_ratios=creator_ratios,
    )
    return c


@router.delete("/campaigns/{campaign_id}")
def delete_campaign(campaign_id: int, _t: str = Depends(require_tenant)):
    if not repo.campaigns_delete(tenancy.current_tenant(), campaign_id):
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found")
    return {"deleted": True, "id": campaign_id}
