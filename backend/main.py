"""
Lumio KOL Matrix Optimizer — FastAPI backend.

Endpoints
---------
  GET  /health           Health check
  POST /optimize         Run all 6 algorithms, return best KOL matrix
  GET  /kols             Filtered & paginated KOL list
  GET  /kol/{id}         Single KOL detail with scores & reasons
  GET  /top-kols         Top 10 KOLs by creator score
  POST /simulate-scale   Synthetic pool-size simulator (achievable GMV vs creator count)

Run:  uvicorn backend.main:app --reload
Docs: http://localhost:8000/docs
"""

import contextlib
import logging
import math
import os
import re
import sys
import time
import uuid
import contextvars
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException, Query, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, Field

# ── Make project root importable ──────────────────────────────────
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.models import KOL
from engine.fitness import summarize_state
from engine.optimization.simulated_annealing import simulated_annealing
from engine.optimization.hill_climber import hill_climber
from engine.optimization.random_search import random_search
from engine.optimization.genetic_algorithm import genetic_algorithm
from engine.optimization.tabu_search import tabu_search
from engine.optimization.greedy_ranking import greedy_ranking
from engine.scoring.creator_score import compute_creator_score
from engine.scoring.explainer import generate_reasons, get_tier
from backend import calibration, categories, i18n, mailer, ratelimit, repo, settings, tenancy

# Refuse to boot an unsafe production configuration (see backend/settings.py).
settings.validate()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("kol.api")

from backend.auth import router as auth_router, require_tenant
from backend.billing import router as billing_router, check_and_consume_optimize
from backend.crud import router as crud_router
from backend.campaigns import router as campaigns_router
from backend.admin import router as admin_router, record_interest
from backend.agent import router as agent_router
from backend.growth import router as growth_router
from backend.guide import router as guide_router
from backend.payments import router as payments_router
from backend.workspace import router as workspace_router
from backend.pipeline import router as pipeline_router
from backend.collab import router as collab_router
from backend.pool import router as pool_router
from backend.studio import router as studio_router
from backend.news import router as news_router, admin_router as news_admin_router
from backend.aivideo import router as aivideo_router
from backend import jobs

@contextlib.asynccontextmanager
async def _lifespan(_app):
    # Merchants only see real creators: drop generated placeholders left over
    # from older seed libraries. (Skipped for the test suite's fixture tenant.)
    if not tenancy.DEFAULT_TENANT:
        try:
            n = repo.purge_synthetic_creators()
            if n:
                logging.getLogger("kol").info("removed %d synthetic placeholder creators", n)
        except Exception:                      # never block startup on hygiene
            logging.getLogger("kol").exception("synthetic-creator purge failed")
        try:
            from backend import pool as _pool
            n = _pool.seed_from_library()
            _pool.migrate_capture_tags()       # raw workspace ids → keyed hashes
            if n:
                logging.getLogger("kol").info("added %d curated creators to the Explore pool", n)
        except Exception:
            logging.getLogger("kol").exception("Explore pool seeding failed")
    jobs.start()            # renewal reminders + cleanup (backend/jobs.py)
    yield


app = FastAPI(
    lifespan=_lifespan,
    title="Lumio KOL Optimizer API",
    description="Local search optimizer for selecting a KOL portfolio under a marketing budget.",
    version="2.0",
    # The interactive API docs are a developer tool; keep them off the public site.
    docs_url=None if settings.IS_PRODUCTION else "/docs",
    redoc_url=None if settings.IS_PRODUCTION else "/redoc",
    openapi_url=None if settings.IS_PRODUCTION else "/openapi.json",
)

# The bundled frontend is served from this same origin, so production needs no
# CORS at all; set CORS_ORIGINS only if a frontend on another origin calls the
# API. Auth is a Bearer header (no cookies), so credentials are never allowed.
if settings.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
app.include_router(auth_router)
app.include_router(billing_router)
app.include_router(crud_router)
app.include_router(campaigns_router)
app.include_router(admin_router)
app.include_router(agent_router)
app.include_router(guide_router)
app.include_router(payments_router)
app.include_router(workspace_router)
app.include_router(pipeline_router)
app.include_router(collab_router)
app.include_router(pool_router)
app.include_router(aivideo_router)
app.include_router(studio_router)
app.include_router(growth_router)
app.include_router(news_router)
app.include_router(news_admin_router)


@app.exception_handler(StarletteHTTPException)
async def _localised_http_error(request: Request, exc: StarletteHTTPException):
    """Errors are raised in English everywhere; a console running in Chinese
    (X-Lang: zh) gets the Chinese wording here, in one place."""
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": i18n.localise(exc.detail, request.headers.get("x-lang"))},
                        headers=getattr(exc, "headers", None))

@app.exception_handler(RequestValidationError)
async def _localised_validation_error(request: Request, exc: RequestValidationError):
    """Malformed input gets one readable sentence (in the console's language)
    instead of pydantic's English error list, which the UI would otherwise show
    verbatim — or as "[object Object]"."""
    errs = exc.errors()
    first = errs[0] if errs else {}
    field = next((str(p) for p in reversed(first.get("loc") or []) if isinstance(p, str) and p != "body"), "")
    msg = str(first.get("msg") or "invalid value")
    text = f"Please check {field}: {msg}" if field else f"Please check your input: {msg}"
    return JSONResponse(status_code=422, content={"detail": i18n.localise(text, request.headers.get("x-lang")),
                                                  "errors": jsonable_encoder(errs)})


# UI language of the calling client (X-Lang header, set by the frontend).
# Server-generated text — e.g. the "why this creator" reasons — follows it.
_LANG_VAR = contextvars.ContextVar("ui_lang", default="en")


@app.middleware("http")
async def _capture_ui_lang(request, call_next):
    lang = (request.headers.get("x-lang") or "en").lower()
    _LANG_VAR.set("zh" if lang.startswith("zh") else "en")
    return await call_next(request)


# The frontend is inline-script heavy, so the CSP allows inline code; it still
# pins where scripts/styles/fonts may load from and blocks framing and
# cross-origin form posts.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "img-src 'self' data: blob: https:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)
_API_DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


@app.middleware("http")
async def _harden(request: Request, call_next):
    """Request-size cap, request id, access log, and security headers."""
    rid = uuid.uuid4().hex[:12]
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > settings.MAX_UPLOAD_BYTES:
        return JSONResponse(status_code=413, content={
            "detail": i18n.localise(f"File too large (limit {settings.MAX_UPLOAD_BYTES // (1024 * 1024)} MB)",
                                    request.headers.get("x-lang"))})

    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        # Anything a route didn't handle: log the traceback, never leak it.
        log.exception("unhandled error rid=%s %s %s", rid, request.method, request.url.path)
        return JSONResponse(status_code=500, headers={"X-Request-ID": rid}, content={
            "detail": i18n.localise("Something went wrong on our side. Please try again.",
                                    request.headers.get("x-lang")), "request_id": rid})

    ms = (time.perf_counter() - started) * 1000
    if response.status_code >= 500 or ms > 5000:
        log.warning("rid=%s %s %s -> %s in %.0f ms", rid, request.method,
                    request.url.path, response.status_code, ms)

    h = response.headers
    h["X-Request-ID"] = rid
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if not request.url.path.startswith(_API_DOC_PATHS):     # Swagger UI needs its own CDN
        h.setdefault("Content-Security-Policy", _CSP)
    if settings.IS_PRODUCTION:
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


async def _throttle_compute(request: Request, tenant_id: str = Depends(require_tenant)) -> str:
    """Per-tenant ceiling on the CPU-bound solver routes (each call occupies a
    worker for its whole duration). Monthly plan quotas are separate."""
    ratelimit.enforce("compute", tenant_id, limit=30, window=60,
                      detail="You're running optimizations very quickly — give it a few seconds.")
    return tenant_id

# ── Enum constants ────────────────────────────────────────────────
VALID_COUNTRIES = ["MY", "ID", "TH", "PH", "SG", "VN"]
VALID_CATEGORIES = categories.VALID_CATEGORIES      # see backend/categories.py
VALID_TIERS = ["Mega", "Macro", "Micro", "Nano"]

# The active KOL library is resolved per-tenant at call time (backend/tenancy.py),
# so every request reads only the current merchant's isolated library.


# ════════════════════════════════════════════════════════════════
#  Pydantic models
# ════════════════════════════════════════════════════════════════
# Upper bound on any budget input: keeps solver runtime and the numbers sane.
MAX_BUDGET = 10_000_000


class OptimizeRequest(BaseModel):
    budget: float = Field(..., le=MAX_BUDGET, allow_inf_nan=False)
    countries: List[str] = Field(default=[], max_length=12)
    category: str = "beauty"
    content_type: str = "all"   # all | live (livestream sellers) | video (short-video creators)
    seed: int = Field(42, ge=0, le=2**31 - 1)


class SimulateScaleRequest(BaseModel):
    """Inputs for the synthetic Creator-Pool-Size simulator."""
    budget: float = Field(..., le=MAX_BUDGET, allow_inf_nan=False)
    seed: int = Field(42, ge=0, le=2**31 - 1)
    sizes: List[int] = Field(default=[], max_length=16)   # pool sizes to sweep; empty = default sweep
    target_gmv: Optional[float] = None     # optional GMV goal → server returns needed_n


class KOLResult(BaseModel):
    id: int
    name: str
    country: str
    category: str
    followers: int
    engagement_rate: float
    fit_score: float
    commission_rate: float = 0.15
    cost: float
    expected_gmv: float
    tier: str = ""
    creator_score: float = 0.0
    reasons: List[str] = []
    # Both languages, so a console that switches language after the run can
    # re-render the "why this creator" lines without asking the server again.
    reasons_i18n: Dict[str, List[str]] = {}
    age_group: str = ""
    gender_ratio: float = 0.5
    source: str = "fastmoss"          # "fastmoss" (real import) | "synthetic" (demo fill)
    cost_estimated: bool = False      # True when cost was inferred, not a real quote
    content_type: str = "both"        # "live" (livestream seller) | "video" (short-video) | "both"
    rating: Optional[int] = None      # merchant's own collaboration rating (1–5), if rated


class KOLDetail(KOLResult):
    avg_views: Optional[int] = None
    avg_likes: Optional[int] = None
    predicted_gmv_solo: float = 0.0
    audience_overlap_risk: float = 0.0
    cost_effectiveness_rank: int = 0


class AlgorithmResult(BaseModel):
    algorithm: str
    selected_kols: List[KOLResult]
    selected_count: int
    total_cost: float
    total_gmv: float
    roi: float
    # GMV net of the audience-overlap penalty — the true objective the solvers
    # optimise. Algorithms are RANKED by this, while total_gmv stays the headline
    # display number. (See summarize_state in engine/fitness.py.)
    objective: float
    history: List[float]


class OverlapWarning(BaseModel):
    kol_id_1: int
    kol_name_1: str
    kol_id_2: int
    kol_name_2: str
    overlap_score: float
    reason: str


class TierBreakdown(BaseModel):
    Mega: int = 0
    Macro: int = 0
    Micro: int = 0
    Nano: int = 0


class OptimizeResponse(BaseModel):
    budget: float
    countries: List[str]
    category: str
    candidates: int
    best_algorithm: str
    selected_kols: List[KOLResult]
    total_cost: float
    total_gmv: float
    roi: float
    results: Dict[str, AlgorithmResult]
    tier_breakdown: TierBreakdown = TierBreakdown()
    overlap_warnings: List[OverlapWarning] = []


class PlanResult(BaseModel):
    plan_name: str
    description: str
    budget_limit: float
    selected_kols: List[KOLResult]
    total_cost: float
    total_gmv: float
    roi: float
    tier_breakdown: TierBreakdown = TierBreakdown()
    overlap_warnings: List[OverlapWarning] = []


class OptimizePlansResponse(BaseModel):
    budget: float
    countries: List[str]
    category: str
    candidates: int
    plans: List[PlanResult]


class KOLListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    kols: List[KOLResult]


class TopKOLResponse(BaseModel):
    country: Optional[str]
    category: Optional[str]
    top_kols: List[KOLResult]


class ScalePoint(BaseModel):
    n: int                  # pool size (number of synthetic creators)
    gmv: float              # best achievable GMV at this size (optimized)
    baseline_gmv: float     # random-search baseline at this size (for contrast)
    roi: float
    selected_count: int


class SimulateScaleResponse(BaseModel):
    budget: float
    seed: int
    target_gmv: Optional[float] = None
    needed_n: Optional[int] = None   # smallest swept size whose GMV ≥ target (None = not reached)
    points: List[ScalePoint]


# ════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════
# Presentation-only metadata (source, cost_estimated) that the engine KOL
# dataclass deliberately doesn't carry. Held in a request-scoped contextvar so
# concurrent tenants never share each other's badge metadata.
_KOL_META_VAR: contextvars.ContextVar = contextvars.ContextVar("kol_meta", default=None)


def _refresh_meta(raw_list: List[dict]):
    _KOL_META_VAR.set({
        d["id"]: {
            "source": d.get("source", "fastmoss"),
            "cost_estimated": bool(d.get("cost_estimated", False)),
            "content_type": d.get("content_type") or "both",
            "rating": d.get("rating"),
        }
        for d in raw_list
    })


# Merchant collaboration ratings nudge future selections: a merchant's own
# experience is first-hand brand-fit evidence, so it scales fit_score (which
# flows into predicted GMV and every solver's objective). Unrated → unchanged.
_RATING_FIT = {5: 1.15, 4: 1.08, 3: 1.0, 2: 0.85, 1: 0.65}


def _apply_rating_fit(d: dict) -> dict:
    try:
        factor = _RATING_FIT.get(int(d.get("rating") or 0))
    except (TypeError, ValueError):
        factor = None
    if factor and factor != 1.0 and d.get("fit_score") is not None:
        d["fit_score"] = round(min(1.0, max(0.05, d["fit_score"] * factor)), 4)
    return d


def _kol_meta() -> dict:
    return _KOL_META_VAR.get() or {}


def load_kols(countries: Optional[List[str]] = None,
              category: Optional[str] = None,
              content_type: Optional[str] = None, related: bool = False) -> List[KOL]:
    """Current tenant's creators, with any country/category filter pushed down
    to an indexed SQL query instead of loading the whole library. `related`
    widens a specific category to its parent (womenswear -> fashion) — used by
    the optimizer, not by the library's own filter."""
    wanted = categories.pool(category) if (category and related) else category
    raw = repo.kols_all(tenancy.current_tenant(), countries=countries, category=wanted,
                        content_type=content_type)
    _refresh_meta(raw)
    return [_dict_to_kol(_apply_rating_fit(d)) for d in raw]


def load_raw_kols() -> List[dict]:
    raw = repo.kols_all(tenancy.current_tenant())
    _refresh_meta(raw)
    # Same rating→fit adjustment as load_kols, so the /kol/{id} detail view
    # shows the exact numbers the optimizer works with.
    return [_apply_rating_fit(d) for d in raw]


_KOL_FIELDS = {"id", "name", "country", "category",
               "followers", "engagement_rate", "fit_score",
               "commission_rate", "cost",
               "avg_views", "avg_likes", "gender_ratio", "age_group"}


def _dict_to_kol(d: dict) -> KOL:
    return KOL(**{k: v for k, v in d.items() if k in _KOL_FIELDS})


def _rating_reason(rating: Optional[int], lang: Optional[str] = None) -> Optional[str]:
    """Localised 'you've worked with them before' line for the reasons list."""
    if not rating:
        return None
    zh = (lang or _LANG_VAR.get()) == "zh"
    if rating >= 4:
        return (f"你对 TA 的历史合作评分为 {rating}★ —— 已优先复用" if zh
                else f"You rated past collaborations {rating}★ — prioritised for re-booking")
    if rating <= 2:
        return (f"历史合作评分 {rating}★ —— 本次已降低优先级" if zh
                else f"Past collaboration rated {rating}★ — deprioritised this run")
    return (f"历史合作评分 {rating}★" if zh else f"Past collaboration rated {rating}★")


def to_kol_result(k: KOL, score: float, all_kols: List[KOL]) -> KOLResult:
    meta = _kol_meta().get(k.id, {})
    both = {}
    for lang in ("en", "zh"):
        rs = generate_reasons(k, all_kols, lang=lang)
        rr = _rating_reason(meta.get("rating"), lang)
        if rr:
            rs.insert(0, rr)
        both[lang] = rs
    reasons = both[_LANG_VAR.get()]
    return KOLResult(
        id=k.id,
        name=k.name,
        country=k.country,
        category=k.category,
        followers=k.followers,
        engagement_rate=k.engagement_rate,
        fit_score=k.fit_score,
        commission_rate=k.commission_rate,
        cost=k.cost,
        # Calibrated by past actual-vs-predicted outcomes — this creator's own
        # track record, falling back to its (category, country) segment, then
        # global (factor = 1.0 until the tenant has completed campaigns).
        expected_gmv=round(k.expected_gmv() * calibration.factor(k.category, k.country, k.id), 2),
        tier=get_tier(k),
        age_group=k.age_group,
        gender_ratio=k.gender_ratio,
        creator_score=score,
        reasons=reasons,
        reasons_i18n=both,
        source=meta.get("source", "fastmoss"),
        cost_estimated=meta.get("cost_estimated", False),
        content_type=meta.get("content_type", "both"),
        rating=meta.get("rating"),
    )


def build_algorithm_result(
    name: str,
    state: List[int],
    history: List[float],
    kols: List[KOL],
    scores: List[float],
) -> AlgorithmResult:
    # Displayed GMV/ROI are summed from the per-creator *calibrated* predictions
    # (to_kol_result applies the calibration factor), so the cards always reconcile
    # with the total. The solver's `objective` (its ranking metric) stays raw, so
    # which creators/algorithm win is unaffected. No calibration history → factor
    # 1.0 → numbers match the raw model.
    selected_idx = [i for i, x in enumerate(state) if x == 1]
    selected = [to_kol_result(kols[i], scores[i], kols) for i in selected_idx]
    summary = summarize_state(state, kols)
    total_cost = round(summary["total_cost"], 2)
    total_gmv = round(sum(s.expected_gmv for s in selected), 2)
    return AlgorithmResult(
        algorithm=name,
        selected_kols=selected,
        selected_count=int(summary["selected_count"]),
        total_cost=total_cost,
        total_gmv=total_gmv,
        roi=round(total_gmv / total_cost, 4) if total_cost > 0 else 0.0,
        objective=round(summary["objective"], 2),
        history=history,
    )


# Key of the reference baseline in the results dict. Random Search is a
# yardstick the real optimisers are measured against — never a recommendation.
BASELINE_ALGORITHM = "random_search"


def pick_recommended_key(results: Dict[str, AlgorithmResult]) -> str:
    """Choose the recommended algorithm: the highest objective (GMV net of the
    audience-overlap penalty) AMONG the real optimisers, excluding the Random
    Search baseline. Falls back to all results only if no optimiser ran."""
    solvers = [k for k in results if k != BASELINE_ALGORITHM]
    pool = solvers or list(results)
    return max(pool, key=lambda k: results[k].objective)


def compute_tier_breakdown(kols: List[KOLResult]) -> TierBreakdown:
    counts = {"Mega": 0, "Macro": 0, "Micro": 0, "Nano": 0}
    for k in kols:
        t = k.tier or get_tier_from_followers(k.followers)
        if t in counts:
            counts[t] += 1
    return TierBreakdown(**counts)


def get_tier_from_followers(followers: int) -> str:
    if followers >= 1_000_000:
        return "Mega"
    if followers >= 100_000:
        return "Macro"
    if followers >= 10_000:
        return "Micro"
    return "Nano"


def detect_audience_overlap(selected_kols: List[KOLResult], all_kols: List[KOL]) -> List[OverlapWarning]:
    """Warn when two selected KOLs target demographically near-identical audiences.

    IMPORTANT: /optimize already filters by country + category, so we skip
    those dimensions (every pair would trigger). Instead we check follower
    range, age group, and gender skew — the same dimensions used by the
    fitness overlap penalty.
    """
    warnings: List[OverlapWarning] = []
    n = len(selected_kols)
    if n < 2:
        return warnings

    follower_map = {k.id: k.followers for k in all_kols}

    for i in range(n):
        for j in range(i + 1, n):
            a = selected_kols[i]
            b = selected_kols[j]
            score = 0.0
            reasons: List[str] = []

            fa = follower_map.get(a.id, a.followers)
            fb = follower_map.get(b.id, b.followers)
            if fa > 0 and fb > 0:
                ratio = min(fa, fb) / max(fa, fb)
                if ratio > 0.5:
                    score += 0.5
                    reasons.append("similar follower range")

            if hasattr(a, 'age_group') and hasattr(b, 'age_group') and a.age_group and b.age_group and a.age_group == b.age_group:
                score += 0.3
                reasons.append("same age group")

            if hasattr(a, 'gender_ratio') and hasattr(b, 'gender_ratio'):
                if a.gender_ratio >= 0.7 and b.gender_ratio >= 0.7:
                    score += 0.2
                    reasons.append("both heavily female")
                elif a.gender_ratio <= 0.3 and b.gender_ratio <= 0.3:
                    score += 0.2
                    reasons.append("both heavily male")

            if score > 0.5:
                warnings.append(OverlapWarning(
                    kol_id_1=a.id,
                    kol_name_1=a.name,
                    kol_id_2=b.id,
                    kol_name_2=b.name,
                    overlap_score=round(score, 2),
                    reason=", ".join(reasons),
                ))

    warnings.sort(key=lambda w: w.overlap_score, reverse=True)
    return warnings


def compute_audience_overlap_risk(kol: KOL, all_kols: List[KOL]) -> float:
    if len(all_kols) <= 1:
        return 0.0

    sorted_by_followers = sorted(all_kols, key=lambda k: k.followers, reverse=True)
    top_n = max(1, len(all_kols) // 5)
    top_kols = sorted_by_followers[:top_n]

    total_overlap = 0.0
    count = 0
    for other in top_kols:
        if other.id == kol.id:
            continue
        overlap = 0.0
        if kol.country == other.country:
            overlap += 0.3
        if kol.category == other.category:
            overlap += 0.2
        if kol.followers > 0 and other.followers > 0:
            ratio = min(kol.followers, other.followers) / max(kol.followers, other.followers)
            if ratio > 0.5:
                overlap += 0.3
        if kol.age_group and other.age_group and kol.age_group == other.age_group:
            overlap += 0.1
        if kol.gender_ratio >= 0.7 and other.gender_ratio >= 0.7:
            overlap += 0.05
        elif kol.gender_ratio <= 0.3 and other.gender_ratio <= 0.3:
            overlap += 0.05
        total_overlap += overlap
        count += 1

    if count == 0:
        return 0.0
    return round(min(total_overlap / count, 1.0), 2)


def compute_cost_effectiveness_rank(kol: KOL, all_kols: List[KOL]) -> int:
    if kol.cost <= 0:
        return len(all_kols)

    target_ratio = kol.expected_gmv() / kol.cost
    better_count = sum(
        1 for k in all_kols
        if k.cost > 0 and (k.expected_gmv() / k.cost) > target_ratio
    )
    return better_count + 1


def _downsample(history: List[float], target: int = 400) -> List[float]:
    """Downsample history to at most `target` evenly-spaced points."""
    n = len(history)
    if n <= target:
        return history
    step = n / target
    return [history[min(int(i * step), n - 1)] for i in range(target)]


def run_all_optimizations(
    filtered: List[KOL],
    budget: float,
    seed: int,
    scores: List[float],
    sa_T0: float = 15000.0,
    sa_max_iter: int = 150,
) -> Dict[str, AlgorithmResult]:
    """Run all 6 algorithms and return the full results dict."""
    sa_state, _, sa_hist = simulated_annealing(filtered, budget, seed=seed, T0=sa_T0, max_iter=sa_max_iter)
    hc_state, _, hc_hist = hill_climber(filtered, budget, seed=seed)
    rs_state, _, rs_hist = random_search(filtered, budget, seed=seed)
    ga_state, _, ga_hist = genetic_algorithm(filtered, budget, seed=seed)
    ts_state, _, ts_hist = tabu_search(filtered, budget, seed=seed)
    gr_state, _, gr_hist = greedy_ranking(filtered, budget, seed=seed)

    return {
        "simulated_annealing": build_algorithm_result("Simulated Annealing", sa_state, _downsample(sa_hist), filtered, scores),
        "hill_climber":        build_algorithm_result("Hill Climber",        hc_state, _downsample(hc_hist), filtered, scores),
        "random_search":       build_algorithm_result("Random Search",       rs_state, _downsample(rs_hist), filtered, scores),
        "genetic_algorithm":   build_algorithm_result("Genetic Algorithm",   ga_state, _downsample(ga_hist), filtered, scores),
        "tabu_search":         build_algorithm_result("Tabu Search",         ts_state, _downsample(ts_hist), filtered, scores),
        "greedy_ranking":      build_algorithm_result("Greedy Ranking",      gr_state, _downsample(gr_hist), filtered, scores),
    }


def run_single_optimization(
    filtered: List[KOL],
    budget: float,
    seed: int,
    scores: List[float],
    sa_T0: float = 15000.0,
    sa_max_iter: int = 150,
) -> AlgorithmResult:
    """Run all 6 algorithms and return the recommended optimiser — the highest
    true objective (GMV net of audience-overlap penalty) among the real solvers,
    excluding the Random Search baseline."""
    results = run_all_optimizations(filtered, budget, seed, scores, sa_T0, sa_max_iter)
    return results[pick_recommended_key(results)]


def run_greedy_only(filtered: List[KOL], budget: float, seed: int, scores: List[float]) -> AlgorithmResult:
    """Greedy Ranking only — used by the Safe plan, which always picks Greedy.

    Running all six algorithms just to discard five of them is wasted work that
    grows with pool size and budget, so the Safe plan calls this directly.
    """
    gr_state, _, gr_hist = greedy_ranking(filtered, budget, seed=seed)
    return build_algorithm_result("Greedy Ranking", gr_state, _downsample(gr_hist), filtered, scores)


def run_plan_optimizers(
    filtered: List[KOL],
    budget: float,
    seed: int,
    scores: List[float],
    sa_T0: float = 15000.0,
    sa_max_iter: int = 150,
) -> Dict[str, AlgorithmResult]:
    """Run only the consistently strong solvers (SA, GA, Greedy) for the plans.

    The Aggressive/Balanced plans just need the best portfolio under a criterion;
    the weak baselines (Random Search, Hill Climber) never win, and Tabu Search's
    O(N^2) cost dominates runtime — so they're skipped here. The full six-way
    comparison still runs in /optimize for the Technical benchmark view.
    """
    sa_state, _, sa_hist = simulated_annealing(filtered, budget, seed=seed, T0=sa_T0, max_iter=sa_max_iter)
    ga_state, _, ga_hist = genetic_algorithm(filtered, budget, seed=seed)
    gr_state, _, gr_hist = greedy_ranking(filtered, budget, seed=seed)
    return {
        "simulated_annealing": build_algorithm_result("Simulated Annealing", sa_state, _downsample(sa_hist), filtered, scores),
        "genetic_algorithm":   build_algorithm_result("Genetic Algorithm",   ga_state, _downsample(ga_hist), filtered, scores),
        "greedy_ranking":      build_algorithm_result("Greedy Ranking",       gr_state, _downsample(gr_hist), filtered, scores),
    }


# ════════════════════════════════════════════════════════════════
#  Endpoints
# ════════════════════════════════════════════════════════════════
@app.get("/health")
def health():
    """Liveness + database reachability (used by the container healthcheck
    and load balancers). 503 when the database can't be queried."""
    from sqlalchemy import text
    from backend.db import engine
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        log.exception("health check: database unreachable")
        return JSONResponse(status_code=503, content={"status": "degraded", "database": "down"})
    return {"status": "ok", "database": "ok"}


@app.get("/app-config")
def app_config():
    """Public feature switches the frontend needs before anyone signs in."""
    notice = repo.system_get("announcement")
    from backend import agent
    return {"email_enabled": mailer.enabled(),
            "ai_enabled": bool(agent._API_KEY),
            "billing_self_serve": settings.BILLING_SELF_SERVE,
            # Operator's site-wide notice (maintenance, new features); null when none.
            "announcement": {"id": notice["created_at"], "text": notice.get("text", ""),
                             "text_zh": notice.get("text_zh", ""), "level": notice.get("level", "info")}
            if notice else None}


@app.get("/calibration")
def get_calibration(_t: str = Depends(require_tenant)):
    """How much past actual-vs-predicted outcomes are nudging this tenant's GMV
    predictions (1.0 = no adjustment yet)."""
    return calibration.summary()



# ── API Connections (commercial expansion stubs) ──────────────────
# Phase 1 (current): manual import only
# Phase 2 (planned): creator marketplace, shop partner API
# Phase 3 (planned): Third-party analytics (AsiaKOL, Cube Asia, Nox)

class ApiConnectionRequest(BaseModel):
    integration_type: str = Field(..., max_length=64)
    # Accepted for forward compatibility but never read, logged or stored
    # while the integrations are not live.
    credentials: Dict[str, str] = {}

class ApiInterestRequest(BaseModel):
    email: str = Field(..., max_length=254)
    integration_type: str = Field(..., max_length=64)

_INTEREST_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+$")

@app.get("/api/connections")
def list_connections():
    """Return status of all available data source integrations."""
    return {
        "connections": [
            {"id": "manual", "name": "Manual Import", "status": "active",
             "description": "CSV upload and manual KOL entry"},
            {"id": "tiktok_creator", "name": "Creator Marketplace API", "status": "coming_soon",
             "description": "Auto-sync KOL profiles and audience demographics"},
            {"id": "tiktok_shop", "name": "Shop Partner API", "status": "coming_soon",
             "description": "Real GMV and commission data from affiliate campaigns"},
            {"id": "third_party", "name": "Third-Party Analytics Platforms", "status": "coming_soon",
             "description": "AsiaKOL, Cube Asia, Nox Influencer and similar"},
        ]
    }

@app.post("/api/connections/interest")
def register_interest(req: ApiInterestRequest, request: Request):
    """Register merchant interest in a coming-soon integration."""
    ratelimit.enforce("interest", ratelimit.client_ip(request), limit=10, window=3600)
    if not _INTEREST_EMAIL_RE.match(req.email.strip()):
        raise HTTPException(status_code=422, detail="Invalid email address")
    valid_types = {"tiktok_creator", "tiktok_shop", "third_party"}
    if req.integration_type not in valid_types:
        raise HTTPException(status_code=422, detail=f"Unknown integration type: {req.integration_type}")
    email = req.email.strip().lower()
    record_interest(email, req.integration_type)   # persist for the operator console
    return {"status": "registered", "email": email, "integration_type": req.integration_type}

@app.post("/api/connections/connect")
def connect_integration(req: ApiConnectionRequest):
    """Stub — integrations not yet live."""
    return {
        "status": "coming_soon",
        "message": f"Integration '{req.integration_type}' is not yet available."
    }

# Cap how many candidates the metaheuristics actually search over. A large
# connected creator library can hold hundreds of creators in a single
# market+category slice; running six algorithms (and ×3 for the plan variants)
# over all of them is slow and unnecessary. We pre-screen to the top-K by
# CreatorScore — a realistic shortlisting step that keeps /optimize responsive
# no matter how big the library grows, while leaving the search space large
# enough that the algorithms still produce meaningfully different solutions.
CANDIDATE_CAP = 80


def shortlist_candidates(kols: List[KOL], scores: List[float], cap: int = CANDIDATE_CAP):
    """Return the top-`cap` creators by CreatorScore, with their aligned scores."""
    if len(kols) <= cap:
        return kols, scores
    top = sorted(range(len(kols)), key=lambda i: scores[i], reverse=True)[:cap]
    top.sort()
    return [kols[i] for i in top], [scores[i] for i in top]


@app.post("/optimize", response_model=OptimizeResponse)
def optimize(req: OptimizeRequest, _t: str = Depends(_throttle_compute)):
    """Run all six algorithms and return the best KOL matrix."""
    if req.budget <= 0:
        raise HTTPException(status_code=422, detail="budget must be a positive number")
    invalid_countries = [c for c in req.countries if c not in VALID_COUNTRIES]
    if invalid_countries:
        raise HTTPException(status_code=422, detail=f"invalid countries: {invalid_countries}")
    if req.category not in VALID_CATEGORIES:
        raise HTTPException(status_code=422, detail=f"category must be one of: {', '.join(VALID_CATEGORIES)}")

    if req.content_type not in ("all", "live", "video"):
        raise HTTPException(status_code=422, detail="content_type must be one of: all, live, video")

    active_countries = set(req.countries) if req.countries else set(VALID_COUNTRIES)
    filtered = load_kols(countries=list(active_countries), category=req.category, related=True,
                         content_type=None if req.content_type == "all" else req.content_type)

    if not filtered:
        return OptimizeResponse(
            budget=req.budget,
            countries=list(active_countries),
            category=req.category,
            candidates=0,
            best_algorithm="none",
            selected_kols=[],
            total_cost=0.0,
            total_gmv=0.0,
            roi=0.0,
            results={},
        )

    # Meter the run against the tenant's plan (402 → frontend shows upgrade).
    # Only /optimize is metered: the UI fires /optimize-plans alongside it, and
    # double-charging one user action would be wrong.
    check_and_consume_optimize(_t)

    scores = compute_creator_score(filtered)
    filtered, scores = shortlist_candidates(filtered, scores)

    sa_state, _, sa_hist = simulated_annealing(filtered, req.budget, seed=req.seed)
    hc_state, _, hc_hist = hill_climber(filtered, req.budget, seed=req.seed)
    rs_state, _, rs_hist = random_search(filtered, req.budget, seed=req.seed)
    ga_state, _, ga_hist = genetic_algorithm(filtered, req.budget, seed=req.seed)
    ts_state, _, ts_hist = tabu_search(filtered, req.budget, seed=req.seed)
    gr_state, _, gr_hist = greedy_ranking(filtered, req.budget, seed=req.seed)

    results = {
        "simulated_annealing": build_algorithm_result("Simulated Annealing", sa_state, _downsample(sa_hist), filtered, scores),
        "hill_climber":        build_algorithm_result("Hill Climber",        hc_state, _downsample(hc_hist), filtered, scores),
        "random_search":       build_algorithm_result("Random Search",       rs_state, _downsample(rs_hist), filtered, scores),
        "genetic_algorithm":   build_algorithm_result("Genetic Algorithm",   ga_state, _downsample(ga_hist), filtered, scores),
        "tabu_search":         build_algorithm_result("Tabu Search",         ts_state, _downsample(ts_hist), filtered, scores),
        "greedy_ranking":      build_algorithm_result("Greedy Ranking",      gr_state, _downsample(gr_hist), filtered, scores),
    }

    # Recommend the best real optimiser by true objective (GMV net of overlap),
    # excluding the Random Search baseline.
    best_key = pick_recommended_key(results)
    best = results[best_key]

    tier_bd = compute_tier_breakdown(best.selected_kols)
    overlap_warnings = detect_audience_overlap(best.selected_kols, filtered)

    return OptimizeResponse(
        budget=req.budget,
        countries=list(active_countries),
        category=req.category,
        candidates=len(filtered),
        best_algorithm=best.algorithm,
        selected_kols=best.selected_kols,
        total_cost=best.total_cost,
        total_gmv=best.total_gmv,
        roi=best.roi,
        results=results,
        tier_breakdown=tier_bd,
        overlap_warnings=overlap_warnings,
    )


@app.post("/optimize-plans", response_model=OptimizePlansResponse)
def optimize_plans(req: OptimizeRequest, _t: str = Depends(_throttle_compute)):
    """Return 3 plans with genuinely different selection objectives.

    Aggressive  — 120% budget, winner = algorithm with highest total GMV.
                  "Spend more, maximise reach."

    Balanced    — 100% budget, winner = algorithm with highest sqrt(GMV × ROI).
                  Geometric mean penalises both low-GMV and low-ROI extremes.
                  "Best trade-off between scale and efficiency."

    Safe        — 80% budget, winner = Greedy Ranking.
                  Greedy Ranking keeps the stronger of GMV-descending and
                  GMV/cost-ratio fills, then applies local improvements under
                  a smaller budget cap.
                  "Protect budget, maximise ROI."
    """
    if req.budget <= 0:
        raise HTTPException(status_code=422, detail="budget must be a positive number")
    invalid_countries = [c for c in req.countries if c not in VALID_COUNTRIES]
    if invalid_countries:
        raise HTTPException(status_code=422, detail=f"invalid countries: {invalid_countries}")
    if req.category not in VALID_CATEGORIES:
        raise HTTPException(status_code=422, detail=f"category must be one of: {', '.join(VALID_CATEGORIES)}")

    if req.content_type not in ("all", "live", "video"):
        raise HTTPException(status_code=422, detail="content_type must be one of: all, live, video")

    active_countries = set(req.countries) if req.countries else set(VALID_COUNTRIES)
    filtered = load_kols(countries=list(active_countries), category=req.category, related=True,
                         content_type=None if req.content_type == "all" else req.content_type)

    if not filtered:
        return OptimizePlansResponse(
            budget=req.budget,
            countries=list(active_countries),
            category=req.category,
            candidates=0,
            plans=[],
        )

    scores = compute_creator_score(filtered)
    filtered, scores = shortlist_candidates(filtered, scores)

    # ── Aggressive: 120% budget → pick algorithm with max GMV ──────────────
    aggressive_budget = req.budget * 1.2
    agg_results = run_plan_optimizers(filtered, aggressive_budget, req.seed, scores,
                                      sa_T0=20000.0, sa_max_iter=200)
    agg_key = max(agg_results, key=lambda k: agg_results[k].total_gmv)
    agg_result = agg_results[agg_key]
    agg_tier = compute_tier_breakdown(agg_result.selected_kols)
    agg_overlap = detect_audience_overlap(agg_result.selected_kols, filtered)

    # ── Balanced: 100% budget → pick algorithm with max sqrt(GMV × ROI) ────
    #    Geometric mean of GMV and ROI rewards both scale and efficiency;
    #    it differs from Aggressive whenever ROI varies meaningfully across algos.
    bal_results = run_plan_optimizers(filtered, req.budget, req.seed + 1, scores,
                                      sa_T0=15000.0, sa_max_iter=150)
    bal_key = max(bal_results,
                  key=lambda k: math.sqrt(bal_results[k].total_gmv * max(bal_results[k].roi, 0.01)))
    bal_result = bal_results[bal_key]
    bal_tier = compute_tier_breakdown(bal_result.selected_kols)
    bal_overlap = detect_audience_overlap(bal_result.selected_kols, filtered)

    # ── Safe: 80% budget → always use Greedy Ranking ───────────────────────
    #    Greedy Ranking is deterministic and strong under a smaller budget cap,
    #    making the plan conservative without relying on the weak baselines.
    safe_budget = req.budget * 0.8
    safe_result = run_greedy_only(filtered, safe_budget, req.seed + 2, scores)
    safe_tier = compute_tier_breakdown(safe_result.selected_kols)
    safe_overlap = detect_audience_overlap(safe_result.selected_kols, filtered)

    return OptimizePlansResponse(
        budget=req.budget,
        countries=list(active_countries),
        category=req.category,
        candidates=len(filtered),
        plans=[
            PlanResult(
                plan_name="Aggressive",
                description=("以最多 120% 预算冲刺最高 GMV，适合增长期投放。" if _LANG_VAR.get() == "zh"
                             else "Maximize GMV with up to 120% budget. Best for growth campaigns."),
                budget_limit=aggressive_budget,
                selected_kols=agg_result.selected_kols,
                total_cost=agg_result.total_cost,
                total_gmv=agg_result.total_gmv,
                roi=agg_result.roi,
                tier_breakdown=agg_tier,
                overlap_warnings=agg_overlap,
            ),
            PlanResult(
                plan_name="Balanced",
                description=("预算内 GMV 与 ROI 的最优平衡，推荐默认方案。" if _LANG_VAR.get() == "zh"
                             else "Optimal GMV–ROI trade-off within budget. Recommended default."),
                budget_limit=req.budget,
                selected_kols=bal_result.selected_kols,
                total_cost=bal_result.total_cost,
                total_gmv=bal_result.total_gmv,
                roi=bal_result.roi,
                tier_breakdown=bal_tier,
                overlap_warnings=bal_overlap,
            ),
            PlanResult(
                plan_name="Safe",
                description=("预算上限 80%，保守稳健、效率优先的阵容。" if _LANG_VAR.get() == "zh"
                             else "80% budget cap for a conservative, efficient portfolio."),
                budget_limit=safe_budget,
                selected_kols=safe_result.selected_kols,
                total_cost=safe_result.total_cost,
                total_gmv=safe_result.total_gmv,
                roi=safe_result.roi,
                tier_breakdown=safe_tier,
                overlap_warnings=safe_overlap,
            ),
        ],
    )


# ════════════════════════════════════════════════════════════════
#  Budget advisor — "how should I split this budget?"
# ════════════════════════════════════════════════════════════════
class BudgetAdvisorRequest(BaseModel):
    budget: float = Field(..., le=MAX_BUDGET, allow_inf_nan=False)
    countries: List[str] = Field(default=[], max_length=12)
    category: str = "beauty"
    content_type: str = "all"


def _dict_tier(d: dict) -> str:
    f = d.get("followers") or 0
    return "Mega" if f >= 1_000_000 else "Macro" if f >= 100_000 else "Micro" if f >= 10_000 else "Nano"


def compute_budget_plan(tenant_id: str, budget: float, countries: List[str],
                        category: str, content_type: str = "all") -> dict:
    """Rule-based allocation template grounded in the merchant's own library.

    Splits the budget into up to three roles — anchor (credibility & reach),
    core (conversion workhorses) and test (cheap experiments / ad material) —
    where each role maps to the highest creator tier the budget can actually
    afford *in this merchant's library* (median real quotes, not theory).
    """
    import statistics

    raw = repo.kols_all(tenant_id, countries=countries or None, category=categories.pool(category),
                        content_type=None if content_type in ("all", "", None) else content_type)
    tiers: Dict[str, dict] = {}
    for t in VALID_TIERS:
        rows = [d for d in raw if _dict_tier(d) == t]
        costs = [d["cost"] for d in rows if (d.get("cost") or 0) > 0]
        tiers[t] = {
            "available": len(rows),
            "median_cost": round(statistics.median(costs), 2) if costs else None,
        }

    # Tiers we can actually book, ordered Mega → Nano.
    usable = [t for t in VALID_TIERS if tiers[t]["available"] > 0 and tiers[t]["median_cost"]]
    flags: List[str] = []
    if not usable:
        return {"budget": budget, "category": category, "countries": countries,
                "content_type": content_type, "total_available": len(raw),
                "buckets": [], "rated_picks": [], "flags": ["empty_library"]}

    # Anchor = highest tier whose median quote fits in ~half the budget.
    anchor = next((t for t in usable if tiers[t]["median_cost"] <= budget * 0.5), None)
    if anchor is None:
        # Budget can't carry any tier as an anchor — go all-in on the cheapest tier.
        cheapest = min(usable, key=lambda t: tiers[t]["median_cost"])
        buckets_def = [(cheapest, "core", 1.0)]
        flags.append("budget_stretch")
    else:
        below = usable[usable.index(anchor) + 1:]
        if len(below) >= 2:
            buckets_def = [(anchor, "anchor", 0.45), (below[0], "core", 0.35), (below[1], "test", 0.20)]
        elif len(below) == 1:
            buckets_def = [(anchor, "anchor", 0.60), (below[0], "core", 0.40)]
        else:
            buckets_def = [(anchor, "core", 1.0)]

    buckets = []
    for t, role, pct in buckets_def:
        amount = round(budget * pct, 2)
        median = tiers[t]["median_cost"]
        suggested = max(1, int(amount // median)) if median and median <= amount else (1 if median else 0)
        suggested = min(suggested, tiers[t]["available"])
        if suggested >= tiers[t]["available"]:
            flags.append(f"thin:{t}")
        buckets.append({
            "tier": t, "role": role, "pct": round(pct * 100),
            "amount": amount, "median_cost": median,
            "suggested_count": suggested, "available": tiers[t]["available"],
        })

    # Creators this merchant already rated highly — suggest re-booking first.
    rated = sorted([d for d in raw if (d.get("rating") or 0) >= 4],
                   key=lambda d: (-(d.get("rating") or 0), -(d.get("followers") or 0)))
    rated_picks = [{"id": d["id"], "name": d.get("name"), "tier": _dict_tier(d),
                    "rating": d.get("rating"), "cost": d.get("cost")} for d in rated[:5]]

    return {"budget": budget, "category": category, "countries": countries,
            "content_type": content_type, "total_available": len(raw),
            "buckets": buckets, "rated_picks": rated_picks, "flags": flags}


@app.post("/budget-advisor")
def budget_advisor(req: BudgetAdvisorRequest, _t: str = Depends(require_tenant)):
    """Explain how to split a budget across creator tiers before optimizing."""
    if req.budget <= 0:
        raise HTTPException(status_code=422, detail="budget must be a positive number")
    invalid_countries = [c for c in req.countries if c not in VALID_COUNTRIES]
    if invalid_countries:
        raise HTTPException(status_code=422, detail=f"invalid countries: {invalid_countries}")
    if req.category not in VALID_CATEGORIES:
        raise HTTPException(status_code=422, detail=f"category must be one of: {', '.join(VALID_CATEGORIES)}")
    if req.content_type not in ("all", "live", "video"):
        raise HTTPException(status_code=422, detail="content_type must be one of: all, live, video")
    return compute_budget_plan(tenancy.current_tenant(), req.budget, req.countries,
                               req.category, req.content_type)


@app.get("/categories")
def list_categories():
    """Grouped product categories for the console's pickers."""
    return {"groups": categories.catalog()}


@app.get("/kols", response_model=KOLListResponse)
def get_kols(
    country: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    tier: Optional[str] = Query(None),
    content_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _t: str = Depends(require_tenant),
):
    """Return a filtered, paginated list of KOLs."""
    # An empty filter ("?country=") means "no filter", not an invalid value.
    country, category, tier, content_type = (v or None for v in (country, category, tier, content_type))
    if country is not None and country not in VALID_COUNTRIES:
        raise HTTPException(status_code=422, detail="country must be one of: MY, ID, TH, PH, SG, VN")
    if category is not None and category not in VALID_CATEGORIES:
        raise HTTPException(status_code=422, detail=f"category must be one of: {', '.join(VALID_CATEGORIES)}")
    if tier is not None and tier not in VALID_TIERS:
        raise HTTPException(status_code=422, detail="tier must be one of: Mega, Macro, Micro, Nano")
    if content_type is not None and content_type not in ("live", "video"):
        raise HTTPException(status_code=422, detail="content_type must be one of: live, video")

    all_kols = load_kols(countries=[country] if country else None, category=category,
                         content_type=content_type)

    filtered = [k for k in all_kols if tier is None or get_tier(k) == tier]

    scores = compute_creator_score(filtered)
    page = filtered[offset:offset + limit]
    page_scores = scores[offset:offset + limit]

    kols_out = [
        to_kol_result(k, s, filtered) for k, s in zip(page, page_scores)
    ]

    return KOLListResponse(
        total=len(filtered),
        limit=limit,
        offset=offset,
        kols=kols_out,
    )


@app.get("/kol/{kol_id}", response_model=KOLDetail)
def get_kol(kol_id: int, _t: str = Depends(require_tenant)):
    """Return full details for a single KOL, including extended fields."""
    raw_kols = load_raw_kols()
    raw = next((d for d in raw_kols if d["id"] == kol_id), None)
    if raw is None:
        raise HTTPException(status_code=404, detail=f"KOL with id {kol_id} not found")

    all_kols = [_dict_to_kol(d) for d in raw_kols]
    target = _dict_to_kol(raw)

    scores = compute_creator_score(all_kols)
    idx = next(i for i, k in enumerate(all_kols) if k.id == kol_id)
    score = scores[idx]

    base = to_kol_result(target, score, all_kols)

    return KOLDetail(
        **base.model_dump(),
        avg_views=raw.get("avg_views"),
        avg_likes=raw.get("avg_likes"),
        predicted_gmv_solo=round(target.expected_gmv(), 2),
        audience_overlap_risk=compute_audience_overlap_risk(target, all_kols),
        cost_effectiveness_rank=compute_cost_effectiveness_rank(target, all_kols),
    )


@app.get("/top-kols", response_model=TopKOLResponse)
def top_kols(
    country: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    _t: str = Depends(require_tenant),
):
    """Return the top 10 KOLs ranked by creator score."""
    if country is not None and country not in VALID_COUNTRIES:
        raise HTTPException(status_code=422, detail="country must be one of: MY, ID, TH, PH, SG, VN")
    if category is not None and category not in VALID_CATEGORIES:
        raise HTTPException(status_code=422, detail=f"category must be one of: {', '.join(VALID_CATEGORIES)}")

    filtered = load_kols(countries=[country] if country else None, category=category)

    if not filtered:
        return TopKOLResponse(country=country, category=category, top_kols=[])

    scores = compute_creator_score(filtered)
    paired = sorted(zip(filtered, scores), key=lambda p: p[1], reverse=True)[:10]
    top = [to_kol_result(k, s, filtered) for k, s in paired]

    return TopKOLResponse(country=country, category=category, top_kols=top)


_DEFAULT_SCALE_SIZES = [25, 50, 100, 200, 300, 500]


@app.post("/simulate-scale", response_model=SimulateScaleResponse)
def simulate_scale(req: SimulateScaleRequest, _t: str = Depends(_throttle_compute)):
    """
    Creator-Pool-Size simulator — answers "how big a creator library do I need
    to reach my target GMV?".

    Runs entirely on freshly generated **synthetic** creators (a generic mixed
    pool), independent of the user's real library. For each pool size it reports
    the achievable GMV (best of the strong solvers) plus a random-search baseline,
    so the frontend can plot achievable-GMV-vs-pool-size and read off the size
    needed to hit a target.
    """
    if req.budget <= 0:
        raise HTTPException(status_code=422, detail="budget must be a positive number")

    # Normalise the requested sweep: clamp to [10, 500], de-dup, cap count, sort.
    sizes = sorted({max(10, min(500, int(s))) for s in (req.sizes or _DEFAULT_SCALE_SIZES)})[:8]
    if not sizes:
        sizes = list(_DEFAULT_SCALE_SIZES)

    # Generate ONE synthetic pool at the largest size, then slice it for the
    # smaller sizes → a clean monotonic curve from a single generation pass.
    import tempfile
    from data.generator import generate_kols
    max_n = sizes[-1]
    with tempfile.TemporaryDirectory() as tmpdir:
        kol_dicts = generate_kols(
            num=max_n,
            json_output_path=os.path.join(tmpdir, f"scale_{max_n}_{req.seed}.json"),
            csv_output_path=os.path.join(tmpdir, f"scale_{max_n}_{req.seed}.csv"),
            seed=req.seed,
        )
    full_pool = [_dict_to_kol(d) for d in kol_dicts]

    points: List[ScalePoint] = []
    best_run = 0.0   # running max → monotonic achievable curve (pools are nested)
    base_run = 0.0
    for n in sizes:
        pool = full_pool[:n]
        # Achievable = best of the strong solvers AND the baseline (skip O(N^2) Tabu).
        ga = summarize_state(genetic_algorithm(pool, req.budget, seed=req.seed)[0], pool)
        gr = summarize_state(greedy_ranking(pool, req.budget, seed=req.seed)[0], pool)
        rs = summarize_state(random_search(pool, req.budget, seed=req.seed)[0], pool)
        cand = max((ga, gr, rs), key=lambda s: s["total_gmv"])
        best_run = max(best_run, cand["total_gmv"])
        base_run = max(base_run, rs["total_gmv"])
        points.append(ScalePoint(
            n=n,
            gmv=round(best_run, 2),
            baseline_gmv=round(base_run, 2),
            roi=round(cand["roi"], 4),
            selected_count=int(cand["selected_count"]),
        ))

    needed_n = None
    if req.target_gmv and req.target_gmv > 0:
        hit = next((p.n for p in points if p.gmv >= req.target_gmv), None)
        needed_n = hit

    return SimulateScaleResponse(
        budget=req.budget,
        seed=req.seed,
        target_gmv=req.target_gmv,
        needed_n=needed_n,
        points=points,
    )


# ── Serve frontend as static files (after all API routes) ──────────
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def index_page(request: Request):
    """The landing page / console, with the share-preview tags (og:image, og:url)
    pointing at this site's absolute origin — WhatsApp and Facebook need full URLs."""
    with open(os.path.join(FRONTEND_DIR, "index.html"), encoding="utf-8") as f:
        html = f.read()
    return HTMLResponse(html.replace("__SITE__", mailer.base_url(request)),
                        headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
