"""
Creator CRUD & CSV import — data management endpoints.

Endpoints
---------
  POST   /kols/add                   Add a single KOL
  POST   /kols/import-csv            Bulk import from CSV
  PUT    /kols/{id}                  Update a KOL (auto-snapshots old values)
  DELETE /kols/{id}                  Delete a KOL
  GET    /kols/template              Download CSV template
  GET    /kols/export                Export all KOLs as CSV
  GET    /kols/{id}/history          Metric history snapshots for a KOL
  POST   /kols/{id}/simulate-update  Simulate a platform API refresh (random drift)

All writes go to the tenant-scoped `kols` / `kol_snapshots` tables
(backend/repo.py) as row-level operations, so concurrent merchants never
contend on each other's data.
"""
import csv
import io
import os
import random
import re
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends
from pydantic import BaseModel, field_validator

from backend import billing, repo, settings, tenancy
from backend.auth import require_tenant

router = APIRouter()


async def _read_upload(file: UploadFile) -> bytes:
    """Read an uploaded file, refusing anything over the configured size cap
    (the middleware only sees the declared Content-Length)."""
    content = await file.read(settings.MAX_UPLOAD_BYTES + 1)
    if len(content) > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (limit {settings.MAX_UPLOAD_BYTES // (1024 * 1024)} MB)")
    return content

DATA_PATH = os.path.join(tenancy.DATA_DIR, "sample_kols.json")
SEED_PATH = os.path.join(tenancy.DATA_DIR, "seed_kols.json")


# ── KOL history helpers ───────────────────────────────────────
_SNAPSHOT_FIELDS = ("engagement_rate", "fit_score", "followers", "commission_rate", "avg_views", "avg_likes")


def _record_snapshot(kol_record: dict):
    """Append a timestamped snapshot of the KOL's current metrics to history."""
    metrics = {f: kol_record[f] for f in _SNAPSHOT_FIELDS
               if f in kol_record and kol_record[f] is not None}
    repo.history_append(tenancy.current_tenant(), kol_record["id"],
                        datetime.utcnow().isoformat(), metrics)

VALID_COUNTRIES = ["MY", "ID", "TH", "PH", "SG", "VN"]
VALID_CATEGORIES = ["beauty", "fashion", "accessories", "muslim_fashion", "home", "kitchen",
                    "appliances", "electronics", "mother_baby", "fmcg", "health",
                    "sports_outdoor", "pet", "toys", "automotive"]

CSV_TEMPLATE_HEADER = [
    "name", "country", "category", "followers",
    "engagement_rate", "fit_score", "commission_rate", "cost",
    "avg_views", "avg_likes", "gender_ratio", "age_group",
    "tiktok_url", "content_type",
]

# How the creator sells: live (livestream host), video (short-video creator),
# or both. Optional everywhere — unmarked creators default to "both".
VALID_CONTENT_TYPES = ("live", "video", "both")


# ════════════════════════════════════════════════════════════════
#  Pydantic models
# ════════════════════════════════════════════════════════════════
class KOLCreate(BaseModel):
    """Fields required to add a new KOL."""
    name: str
    country: str = "MY"
    category: str = "beauty"
    followers: int
    engagement_rate: float
    fit_score: float = 0.75
    commission_rate: float = 0.15
    cost: float = 0.0
    avg_views: Optional[int] = None
    avg_likes: Optional[int] = None
    gender_ratio: Optional[float] = None
    age_group: Optional[str] = None
    tiktok_url: Optional[str] = None
    content_type: str = "both"

    @field_validator("country")
    @classmethod
    def validate_country(cls, v):
        if v not in VALID_COUNTRIES:
            raise ValueError(f"country must be one of: {', '.join(VALID_COUNTRIES)}")
        return v

    @field_validator("content_type")
    @classmethod
    def validate_content_type(cls, v):
        v = (v or "both").lower()
        if v not in VALID_CONTENT_TYPES:
            raise ValueError(f"content_type must be one of: {', '.join(VALID_CONTENT_TYPES)}")
        return v

    @field_validator("category")
    @classmethod
    def validate_category(cls, v):
        v = v.lower()
        if v not in VALID_CATEGORIES:
            raise ValueError(f"category must be one of: {', '.join(VALID_CATEGORIES)}")
        return v

    @field_validator("followers")
    @classmethod
    def validate_followers(cls, v):
        if v < 0:
            raise ValueError("followers must be non-negative")
        return v

    @field_validator("cost")
    @classmethod
    def validate_cost(cls, v):
        if v < 0:
            raise ValueError("cost must be non-negative")
        return v

    @field_validator("engagement_rate")
    @classmethod
    def validate_engagement(cls, v):
        if not (0 <= v <= 1):
            raise ValueError("engagement_rate must be between 0 and 1")
        return round(v, 4)

    @field_validator("fit_score")
    @classmethod
    def validate_fit(cls, v):
        if not (0 <= v <= 1):
            raise ValueError("fit_score must be between 0 and 1")
        return round(v, 4)


class KOLUpdate(BaseModel):
    """All fields optional for partial update."""
    name: Optional[str] = None
    country: Optional[str] = None
    category: Optional[str] = None
    followers: Optional[int] = None
    engagement_rate: Optional[float] = None
    fit_score: Optional[float] = None
    commission_rate: Optional[float] = None
    cost: Optional[float] = None
    avg_views: Optional[int] = None
    avg_likes: Optional[int] = None
    gender_ratio: Optional[float] = None
    age_group: Optional[str] = None
    tiktok_url: Optional[str] = None
    content_type: Optional[str] = None


# ════════════════════════════════════════════════════════════════
#  Flexible import helpers (CSV + Excel, FastMoss-friendly)
# ════════════════════════════════════════════════════════════════
def _norm_header(h) -> str:
    """Normalise a column header to snake_case ascii for alias matching."""
    return re.sub(r"[^a-z0-9]+", "_", str(h).strip().lower()).strip("_")


# Map many real-world / FastMoss header variants onto our canonical fields.
_FIELD_ALIASES = {
    "name":            {"name", "creator", "creator_name", "nickname", "username", "user_name", "handle", "account", "kol", "kol_name", "influencer", "display_name", "creator_nickname"},
    "country":         {"country", "region", "market", "location", "geo", "nation"},
    "category":        {"category", "niche", "industry", "vertical", "type", "content_category", "main_category", "tag", "tags"},
    "followers":       {"followers", "follower", "follower_count", "followers_count", "fans", "fans_count", "fans_number"},
    "engagement_rate": {"engagement_rate", "engagement", "er", "eng_rate", "avg_engagement_rate", "engagement_rate_percent", "interaction_rate"},
    "cost":            {"cost", "price", "quote", "fee", "rate_card", "collaboration_price", "collab_price", "unit_price", "price_usd", "cooperation_price", "ad_price"},
    "fit_score":       {"fit_score", "fit", "relevance", "relevance_score", "match_score", "brand_fit"},
    "commission_rate": {"commission_rate", "commission", "commission_pct"},
    "avg_views":       {"avg_views", "average_views", "views", "avg_view", "median_views", "avg_video_views"},
    "avg_likes":       {"avg_likes", "average_likes", "likes", "avg_like", "median_likes"},
    "gender_ratio":    {"gender_ratio", "female_ratio", "female", "female_percent", "female_pct", "pct_female", "audience_female"},
    "age_group":       {"age_group", "age", "age_range", "main_age", "age_bracket"},
    "tiktok_url":      {"tiktok_url", "url", "link", "profile", "profile_url", "tiktok", "tiktok_link", "homepage"},
    "content_type":    {"content_type", "content_format", "format", "channel", "sell_channel", "promotion_type", "promo_type", "creator_type", "sales_channel", "live_or_video"},
}
_ALIAS_TO_FIELD = {alias: field for field, aliases in _FIELD_ALIASES.items() for alias in aliases}

_COUNTRY_NAME_TO_CODE = {
    "malaysia": "MY", "my": "MY",
    "indonesia": "ID", "id": "ID",
    "thailand": "TH", "th": "TH",
    "philippines": "PH", "the_philippines": "PH", "ph": "PH",
    "singapore": "SG", "sg": "SG",
    "vietnam": "VN", "viet_nam": "VN", "vn": "VN",
}
_CATEGORY_SYNONYMS = {
    "beauty": "beauty", "cosmetics": "beauty", "makeup": "beauty", "skincare": "beauty",
    "personal_care": "beauty", "beauty_personal_care": "beauty", "beauty_care": "beauty",
    "fashion": "fashion", "apparel": "fashion", "clothing": "fashion", "lifestyle": "fashion",
    "womenswear": "fashion", "menswear": "fashion", "womens_clothing": "fashion", "mens_clothing": "fashion",
    "underwear": "fashion", "lingerie": "fashion",
    "accessories": "accessories", "fashion_accessories": "accessories", "bags": "accessories", "luggage": "accessories",
    "bags_luggage": "accessories", "shoes": "accessories", "footwear": "accessories", "jewelry": "accessories", "jewellery": "accessories", "watches": "accessories",
    "muslim_fashion": "muslim_fashion", "muslim": "muslim_fashion", "modest_fashion": "muslim_fashion", "muslim_wear": "muslim_fashion", "islamic_fashion": "muslim_fashion",
    "home": "home", "home_living": "home", "home_garden": "home", "household": "home", "furniture": "home", "home_supplies": "home", "daily_use": "home",
    "kitchen": "kitchen", "kitchenware": "kitchen", "home_kitchen": "kitchen", "kitchen_supplies": "kitchen", "cookware": "kitchen", "tableware": "kitchen",
    "appliances": "appliances", "home_appliances": "appliances", "small_appliances": "appliances", "household_appliances": "appliances", "appliance": "appliances",
    "electronics": "electronics", "consumer_electronics": "electronics", "phones": "electronics", "phones_electronics": "electronics",
    "mobile": "electronics", "digital": "electronics", "3c": "electronics", "computers": "electronics", "computer_office": "electronics", "gadgets": "electronics", "tech": "electronics",
    "mother_baby": "mother_baby", "mom_baby": "mother_baby", "baby": "mother_baby", "maternity": "mother_baby", "baby_kids": "mother_baby", "maternal_child": "mother_baby", "kids": "mother_baby",
    "fmcg": "fmcg", "food": "fmcg", "grocery": "fmcg", "beverage": "fmcg", "snacks": "fmcg", "food_beverage": "fmcg", "food_drink": "fmcg", "food_and_beverages": "fmcg",
    "health": "health", "healthcare": "health", "supplements": "health", "wellness": "health", "nutrition": "health", "health_supplements": "health",
    "personal_care_health": "health", "health_personal_care": "health",
    "sports_outdoor": "sports_outdoor", "sports": "sports_outdoor", "outdoor": "sports_outdoor", "sports_outdoors": "sports_outdoor", "fitness": "sports_outdoor", "camping": "sports_outdoor",
    "pet": "pet", "pets": "pet", "pet_supplies": "pet", "pet_care": "pet", "pet_products": "pet",
    "toys": "toys", "toys_hobbies": "toys", "hobbies": "toys", "toy": "toys", "games": "toys", "toys_and_hobbies": "toys",
    "automotive": "automotive", "auto": "automotive", "car": "automotive", "motorcycle": "automotive", "vehicle": "automotive", "car_accessories": "automotive", "auto_parts": "automotive", "motor": "automotive",
}
_CONTENT_TYPE_SYNONYMS = {
    "live": "live", "livestream": "live", "live_stream": "live", "live_streaming": "live",
    "livestreaming": "live", "live_commerce": "live", "host": "live", "anchor": "live",
    "video": "video", "short_video": "video", "shorts": "video", "shortvideo": "video",
    "clip": "video", "clips": "video", "post": "video", "posts": "video",
    "both": "both", "all": "both", "mixed": "both", "hybrid": "both", "live_video": "both",
}


def _parse_number(v):
    """Parse '250,000', '$1,200', '250K', '1.2M' → float. Returns None if blank."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("$", "").replace("USD", "").strip()
    if s == "" or s.lower() in ("nan", "none", "null", "-", "n/a"):
        return None
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*([kKmMbB]?)$", s)
    if m:
        mult = {"k": 1e3, "m": 1e6, "b": 1e9}.get(m.group(2).lower(), 1.0)
        return float(m.group(1)) * mult
    s2 = re.sub(r"[^0-9.\-]", "", s)
    try:
        return float(s2) if s2 not in ("", "-", ".") else None
    except ValueError:
        return None


def _parse_rate(v):
    """Parse a 0-1 rate from '8%', '8', or '0.08'. Values > 1 are treated as percentages."""
    if v is None or str(v).strip() == "":
        return None
    raw = str(v)
    n = _parse_number(raw.replace("%", ""))
    if n is None:
        return None
    if "%" in raw or n > 1.0:
        n = n / 100.0
    return n


def _estimate_cost(followers: int) -> float:
    """Rough collaboration-fee estimate when the source has no price (e.g. FastMoss).

    Tier-based per-follower rate card; flagged to the user so they can replace
    it with real negotiated quotes.
    """
    f = followers or 0
    rate = 0.003 if f >= 1_000_000 else 0.006 if f >= 100_000 else 0.012 if f >= 10_000 else 0.02
    return round(max(50.0, f * rate), 2)


def _read_csv_rows(content: bytes):
    try:
        text = content.decode("utf-8-sig")          # handle Excel BOM
    except UnicodeDecodeError:
        text = content.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    return list(reader.fieldnames or []), [dict(r) for r in reader]


def _read_excel_rows(content: bytes):
    """Read the first worksheet of an .xlsx into (headers, list-of-dicts)."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.active
    headers, rows = None, []
    for r in ws.iter_rows(values_only=True):
        is_blank = r is None or all(c is None or str(c).strip() == "" for c in r)
        if headers is None:
            if is_blank:
                continue
            headers = ["" if c is None else str(c) for c in r]
            continue
        if is_blank:
            continue
        rows.append({headers[i]: r[i] for i in range(min(len(headers), len(r)))})
    wb.close()
    return headers or [], rows


def _canonicalize_rows(headers, rows):
    """Remap each raw header to our canonical field name via the alias table."""
    mapping = {h: _ALIAS_TO_FIELD.get(_norm_header(h), _norm_header(h)) for h in headers}
    out = []
    for r in rows:
        out.append({mapping.get(k, _norm_header(k)): v for k, v in r.items()})
    return out


def _rows_to_records(rows, default_country=None, default_category=None):
    """Validate canonicalised rows into KOL records without persisting them.
    Returns (records, estimated_cost, errors)."""
    dc = (default_country or "").strip().upper() or None
    dcat = (default_category or "").strip().lower() or None
    records, estimated, errors = [], 0, []

    for i, row in enumerate(rows, start=2):
        try:
            name = str(row.get("name") or "").strip()
            if not name:
                errors.append(f"Row {i}: missing creator name")
                continue

            craw = str(row.get("country") or "").strip()
            country = _COUNTRY_NAME_TO_CODE.get(_norm_header(craw), craw.upper()) if craw else (dc or "")
            if country not in VALID_COUNTRIES:
                errors.append(f"Row {i} ({name}): unknown country '{craw or '—'}' (need one of {', '.join(VALID_COUNTRIES)})")
                continue

            catraw = str(row.get("category") or "").strip()
            category = _CATEGORY_SYNONYMS.get(_norm_header(catraw), catraw.lower()) if catraw else (dcat or "")
            if category not in VALID_CATEGORIES:
                errors.append(f"Row {i} ({name}): unknown category '{catraw or '—'}' (need one of {', '.join(VALID_CATEGORIES)})")
                continue

            followers = _parse_number(row.get("followers"))
            if followers is None:
                errors.append(f"Row {i} ({name}): missing followers")
                continue
            followers = int(followers)

            eng = _parse_rate(row.get("engagement_rate"))
            if eng is None:
                errors.append(f"Row {i} ({name}): missing engagement_rate")
                continue

            cost = _parse_number(row.get("cost"))
            cost_estimated = False
            if cost is None or cost <= 0:
                cost = _estimate_cost(followers)
                estimated += 1
                cost_estimated = True

            avg_views = _parse_number(row.get("avg_views"))
            avg_likes = _parse_number(row.get("avg_likes"))
            gender = _parse_rate(row.get("gender_ratio"))

            record = {
                "name":            name,
                "country":         country,
                "category":        category,
                "followers":       followers,
                "engagement_rate": round(eng, 4),
                "fit_score":       round(_parse_rate(row.get("fit_score")) or 0.75, 4),
                "commission_rate": round(_parse_rate(row.get("commission_rate")) or 0.15, 4),
                "cost":            round(float(cost), 2),
                "avg_views":       int(avg_views) if avg_views else None,
                "avg_likes":       int(avg_likes) if avg_likes else None,
                "gender_ratio":    round(gender, 2) if gender is not None else None,
                "age_group":       str(row.get("age_group") or "").strip() or None,
                "tiktok_url":      str(row.get("tiktok_url") or "").strip() or None,
                "content_type":    _CONTENT_TYPE_SYNONYMS.get(
                                       _norm_header(str(row.get("content_type") or "")), "both"),
                "cost_estimated":  cost_estimated,
            }
            records.append(_auto_fill(record))
        except (ValueError, KeyError, TypeError) as e:
            errors.append(f"Row {i}: {e}")

    return records, estimated, errors


def _ingest_rows(rows, default_country=None, default_category=None):
    """Validate canonicalised rows and bulk-insert the valid ones.
    Returns (added, estimated_cost, errors, total)."""
    records, estimated, errors = _rows_to_records(rows, default_country, default_category)
    tenant = tenancy.current_tenant()
    if records:
        billing.check_creator_capacity(tenant, adding=len(records))
    added = repo.kols_add_many(tenant, records) if records else 0
    return added, estimated, errors, repo.kols_count(tenant)


def _auto_fill(d: dict) -> dict:
    """Fill computed fields if not provided."""
    if d.get("avg_views") is None:
        d["avg_views"] = int(d["followers"] * 0.3)
    if d.get("avg_likes") is None:
        d["avg_likes"] = int(d["avg_views"] * d["engagement_rate"])
    if d.get("gender_ratio") is None:
        d["gender_ratio"] = 0.5
    if d.get("age_group") is None:
        d["age_group"] = "18-24"
    return d


# ════════════════════════════════════════════════════════════════
#  Endpoints
# ════════════════════════════════════════════════════════════════
@router.post("/kols/add")
def add_kol(kol: KOLCreate, _t: str = Depends(require_tenant)):
    """Add a single KOL to the database."""
    billing.check_creator_capacity(tenancy.current_tenant(), adding=1)
    record = _auto_fill(kol.model_dump())
    saved = repo.kols_add(tenancy.current_tenant(), record)
    return {"message": f"KOL '{kol.name}' added with id {saved['id']}", "id": saved["id"], "kol": saved}


@router.put("/kols/{kol_id}")
def update_kol(kol_id: int, updates: KOLUpdate, _t: str = Depends(require_tenant)):
    """Update an existing KOL. Snapshots current metrics before applying changes."""
    tenant = tenancy.current_tenant()
    current = repo.kols_get(tenant, kol_id)
    if current is None:
        raise HTTPException(status_code=404, detail=f"KOL with id {kol_id} not found")

    changes = updates.model_dump(exclude_none=True)
    if "country" in changes and changes["country"] not in VALID_COUNTRIES:
        raise HTTPException(status_code=422, detail=f"country must be one of: {', '.join(VALID_COUNTRIES)}")
    if "category" in changes:
        changes["category"] = changes["category"].lower()
        if changes["category"] not in VALID_CATEGORIES:
            raise HTTPException(status_code=422, detail=f"category must be one of: {', '.join(VALID_CATEGORIES)}")
    if "content_type" in changes:
        changes["content_type"] = changes["content_type"].lower()
        if changes["content_type"] not in VALID_CONTENT_TYPES:
            raise HTTPException(status_code=422, detail=f"content_type must be one of: {', '.join(VALID_CONTENT_TYPES)}")

    # A manually entered cost is a real quote → clear the "estimated" flag.
    if "cost" in changes and changes["cost"] and changes["cost"] > 0:
        changes["cost_estimated"] = False

    # Auto-snapshot BEFORE applying changes (track history)
    _record_snapshot(current)

    updated = repo.kols_update(tenant, kol_id, changes)
    return {"message": f"KOL {kol_id} updated", "kol": updated}


@router.get("/kols/{kol_id}/history")
def get_kol_history(kol_id: int, _t: str = Depends(require_tenant)):
    """Return all metric snapshots for a KOL, newest first."""
    tenant = tenancy.current_tenant()
    if not repo.kols_exists(tenant, kol_id):
        raise HTTPException(status_code=404, detail=f"KOL with id {kol_id} not found")

    snapshots = list(reversed(repo.history_for(tenant, kol_id)))
    return {"kol_id": kol_id, "snapshots": snapshots, "count": len(snapshots)}


@router.post("/kols/{kol_id}/simulate-update")
def simulate_kol_update(kol_id: int, _t: str = Depends(require_tenant)):
    """
    Simulate a platform API metric refresh: apply realistic random drift to
    engagement_rate, fit_score, followers, and record a new snapshot.
    Useful for demos and testing trend visualisation without a real API.
    """
    tenant = tenancy.current_tenant()
    kol = repo.kols_get(tenant, kol_id)
    if kol is None:
        raise HTTPException(status_code=404, detail=f"KOL with id {kol_id} not found")

    rng = random.Random()   # fresh RNG each call → different values

    def drift(val: float, lo: float, hi: float, min_v: float, max_v: float) -> float:
        """Apply multiplicative drift within [min_v, max_v]."""
        factor = 1 + rng.uniform(lo, hi)
        return round(max(min_v, min(max_v, val * factor)), 4)

    old_eng = kol.get("engagement_rate") or 0.08
    old_fit = kol.get("fit_score") or 0.75
    old_fol = kol.get("followers") or 100000

    # Snapshot BEFORE applying drift (consistent with update_kol behavior)
    _record_snapshot(kol)

    changes = {
        "engagement_rate": drift(old_eng, -0.08, +0.12, 0.01, 0.50),
        "fit_score":       drift(old_fit, -0.05, +0.08, 0.10, 1.00),
        "followers":       int(drift(old_fol, -0.02, +0.06, 1000, 50_000_000)),
    }
    changes["avg_views"] = int(changes["followers"] * rng.uniform(0.20, 0.40))
    changes["avg_likes"] = int(changes["avg_views"] * changes["engagement_rate"])
    repo.kols_update(tenant, kol_id, changes)

    return {
        "message": "Simulated metric refresh applied",
        "kol_id": kol_id,
        "changes": {
            "engagement_rate": {"before": old_eng, "after": changes["engagement_rate"]},
            "fit_score":        {"before": old_fit, "after": changes["fit_score"]},
            "followers":        {"before": old_fol, "after": changes["followers"]},
        },
    }


@router.delete("/kols/{kol_id}")
def delete_kol(kol_id: int, _t: str = Depends(require_tenant)):
    """Delete a KOL from the database."""
    remaining = repo.kols_delete(tenancy.current_tenant(), kol_id)
    if remaining is None:
        raise HTTPException(status_code=404, detail=f"KOL with id {kol_id} not found")
    return {"message": f"KOL {kol_id} deleted", "remaining": remaining}


# ── Merchant collaboration ratings ────────────────────────────────
class RatingIn(BaseModel):
    """A merchant's own verdict on how a past collaboration went (1–5 stars)."""
    score: int
    note: Optional[str] = None

    @field_validator("score")
    @classmethod
    def validate_score(cls, v):
        if not (1 <= v <= 5):
            raise ValueError("score must be between 1 and 5")
        return v


@router.post("/kols/{kol_id}/rate")
def rate_kol(kol_id: int, body: RatingIn, _t: str = Depends(require_tenant)):
    """Record the merchant's collaboration rating for a creator.

    The latest score is stored on the creator (Kol.extra) and kept as a log.
    Rated creators are boosted/deprioritised in future optimizations
    (see the rating→fit adjustment in backend/main.py).
    """
    tenant = tenancy.current_tenant()
    kol = repo.kols_get(tenant, kol_id)
    if kol is None:
        raise HTTPException(status_code=404, detail=f"KOL with id {kol_id} not found")
    log = list(kol.get("rating_log") or [])
    log.append({
        "score": body.score,
        "note": (body.note or "").strip() or None,
        "at": datetime.utcnow().isoformat(),
    })
    repo.kols_update(tenant, kol_id, {
        "rating": body.score,
        "rating_log": log[-20:],          # keep the last 20 entries
    })
    return {"message": f"Rated '{kol.get('name')}' {body.score}★", "kol_id": kol_id, "rating": body.score}


@router.get("/kols/{kol_id}/collabs")
def kol_collabs(kol_id: int, _t: str = Depends(require_tenant)):
    """Collaboration history for one creator: every saved campaign that
    included them (with predicted vs actual results) plus the merchant's
    rating log — the creator's track record with THIS merchant."""
    tenant = tenancy.current_tenant()
    kol = repo.kols_get(tenant, kol_id)
    if kol is None:
        raise HTTPException(status_code=404, detail=f"KOL with id {kol_id} not found")

    collabs = []
    for c in repo.campaigns_all(tenant):
        sel = next((s for s in (c.get("selected_kols") or []) if s.get("id") == kol_id), None)
        if sel is None:
            continue
        collabs.append({
            "campaign_id": c.get("id"),
            "name": c.get("name"),
            "created_at": c.get("created_at"),
            "status": c.get("status"),
            "budget": c.get("budget"),
            "kol_cost": sel.get("cost"),
            "kol_predicted_gmv": sel.get("predicted_gmv"),
            "campaign_actual_gmv": c.get("actual_total_gmv"),
            "campaign_accuracy_pct": c.get("accuracy_pct"),
        })
    collabs.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {
        "kol_id": kol_id,
        "rating": kol.get("rating"),
        "rating_log": list(reversed(kol.get("rating_log") or [])),
        "collab_count": len(collabs),
        "collabs": collabs[:20],
    }


@router.post("/kols/import-csv")
@router.post("/kols/import")
async def import_file(
    file: UploadFile = File(...),
    default_country: Optional[str] = Form(None),
    default_category: Optional[str] = Form(None),
    _t: str = Depends(require_tenant),
):
    """
    Bulk import creators from a CSV or Excel (.xlsx) file.

    Column headers are matched flexibly (FastMoss / spreadsheet variants like
    "Follower Count", "Engagement Rate (%)", "Region" are understood). Numbers
    may be formatted ("250K", "1.2M", "$1,200", "8%").

    Required (per row, or supplied as a default): name, country, category,
    followers, engagement_rate. `cost` is estimated from followers when absent.

    Defaults: `default_country` / `default_category` fill rows that lack those
    columns. If not supplied, they are inferred from the file name
    (e.g. "Beauty_MY.xlsx" → category=beauty, country=MY).
    """
    filename = (file.filename or "").strip()
    lower = filename.lower()
    content = await _read_upload(file)

    if lower.endswith(".csv"):
        headers, raw_rows = _read_csv_rows(content)
    elif lower.endswith(".xlsx"):
        try:
            headers, raw_rows = _read_excel_rows(content)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Could not read Excel file: {e}")
    elif lower.endswith(".xls"):
        raise HTTPException(status_code=422, detail="Legacy .xls is not supported — please re-save as .xlsx or .csv.")
    else:
        raise HTTPException(status_code=422, detail="File must be a .csv or .xlsx file")

    if not raw_rows:
        raise HTTPException(status_code=422, detail="No data rows found in the file.")

    rows = _canonicalize_rows(headers, rows=raw_rows)

    # Infer per-file defaults from the file name when not explicitly provided.
    stem_tokens = set(_norm_header(os.path.splitext(filename)[0]).split("_"))
    if not default_category:
        default_category = next((_CATEGORY_SYNONYMS[t] for t in stem_tokens if t in _CATEGORY_SYNONYMS), None)
    if not default_country:
        default_country = next((_COUNTRY_NAME_TO_CODE[t] for t in stem_tokens if t in _COUNTRY_NAME_TO_CODE), None)

    added, estimated, errors, total = _ingest_rows(rows, default_country, default_category)

    msg = f"Imported {added} creators"
    if estimated:
        msg += f" · {estimated} had no price (cost estimated from followers)"
    return {
        "message": msg,
        "added": added,
        "estimated_cost": estimated,
        "applied_defaults": {"country": default_country, "category": default_category},
        "errors": errors,
        "total": total,
    }


@router.post("/kols/backfill-costs")
async def backfill_costs(file: UploadFile = File(...), _t: str = Depends(require_tenant)):
    """
    Bulk-update real collaboration prices from a CSV/Excel file.

    Each row identifies an existing creator — by `id`, `tiktok_url`, or `name`
    (matched in that priority) — and supplies a real `cost`/`price`. Matched
    creators get the real price and are marked `cost_estimated = false`, clearing
    the "est." badge. Rows with no price or no match are reported back.
    """
    filename = (file.filename or "").lower()
    content = await _read_upload(file)
    if filename.endswith(".csv"):
        headers, raw_rows = _read_csv_rows(content)
    elif filename.endswith(".xlsx"):
        try:
            headers, raw_rows = _read_excel_rows(content)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Could not read Excel file: {e}")
    else:
        raise HTTPException(status_code=422, detail="File must be a .csv or .xlsx file")

    if not raw_rows:
        raise HTTPException(status_code=422, detail="No data rows found in the file.")

    rows = _canonicalize_rows(headers, raw_rows)
    tenant = tenancy.current_tenant()
    data = repo.kols_all(tenant)
    by_id = {d["id"]: d for d in data}
    by_url, by_name = {}, {}
    for d in data:
        url = (d.get("tiktok_url") or "").strip().lower()
        if url:
            by_url.setdefault(url, d)
        by_name.setdefault((d.get("name") or "").strip().lower(), d)

    changes_by_id, no_price, unmatched = {}, 0, []
    for i, row in enumerate(rows, start=2):
        cost = _parse_number(row.get("cost"))
        if cost is None or cost <= 0:
            no_price += 1
            continue

        target = None
        rid = _parse_number(row.get("id")) or _parse_number(row.get("creator_id"))
        if rid is not None:
            target = by_id.get(int(rid))
        if target is None:
            target = by_url.get((row.get("tiktok_url") or "").strip().lower())
        if target is None:
            target = by_name.get((row.get("name") or "").strip().lower())
        if target is None:
            unmatched.append(str(row.get("name") or row.get("tiktok_url") or f"row {i}"))
            continue

        changes_by_id[target["id"]] = {"cost": round(float(cost), 2), "cost_estimated": False}

    updated = repo.kols_update_many(tenant, changes_by_id) if changes_by_id else 0
    msg = f"Updated {updated} creator prices"
    if unmatched:
        msg += f" · {len(unmatched)} rows didn't match any creator"
    return {
        "message": msg,
        "updated": updated,
        "no_price_rows": no_price,
        "unmatched": unmatched[:50],
        "unmatched_count": len(unmatched),
    }


@router.get("/kols/template")
def download_template():
    """Return a CSV template with headers and one example row."""
    from fastapi.responses import StreamingResponse

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(CSV_TEMPLATE_HEADER)
    writer.writerow([
        "Lisa Beauty", "MY", "beauty", 250000,
        0.08, 0.85, 0.15, 1200,
        75000, 6000, 0.78, "18-24",
        "https://example.com/@lisa_beauty", "both",
    ])
    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=kol_template.csv"},
    )


@router.get("/kols/template-excel")
def download_template_excel():
    """Return an .xlsx template with headers and one example row."""
    import openpyxl
    from fastapi.responses import StreamingResponse

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "creators"
    ws.append(CSV_TEMPLATE_HEADER)
    ws.append([
        "Lisa Beauty", "MY", "beauty", 250000,
        0.08, 0.85, 0.15, 1200,
        75000, 6000, 0.78, "18-24",
        "https://example.com/@lisa_beauty", "both",
    ])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=kol_template.xlsx"},
    )


@router.get("/kols/export")
def export_csv(_t: str = Depends(require_tenant)):
    """Export all KOLs as a downloadable CSV file."""
    from fastapi.responses import StreamingResponse

    data = repo.kols_all(tenancy.current_tenant())
    output = io.StringIO()
    if data:
        all_keys = []
        seen = set()
        for d in data:
            for k in d.keys():
                if k not in seen:
                    all_keys.append(k)
                    seen.add(k)
        writer = csv.DictWriter(output, fieldnames=all_keys, extrasaction="ignore")
        writer.writeheader()
        for row in data:
            writer.writerow({k: row.get(k, "") for k in all_keys})
    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=kol_export.csv"},
    )


@router.post("/kols/reset")
def reset_database(_t: str = Depends(require_tenant)):
    """
    Restore the library to this tenant's seed baseline — the initialization
    creator library every merchant starts from.

    A merchant can freely add / edit / delete / import while experimenting,
    then reset to snap back to the curated seed. If no seed baseline exists,
    this clears the library to empty instead.

    Also clears metric history so reset state is clean (snapshots that
    referenced edited/removed creators do not linger).
    """
    tenant = tenancy.current_tenant()
    seed = repo.tenant_seed(tenant)
    repo.kols_replace(tenant, seed)
    repo.history_clear(tenant)
    if seed:
        return {"message": "Library restored to seed baseline", "total": len(seed)}
    return {"message": "Database cleared successfully", "total": 0}
