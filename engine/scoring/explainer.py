# engine/scoring/explainer.py
"""
Explainer — generates human-readable recommendation reasons for each KOL.

This is the "Explainable AI" feature that makes the system more
trustworthy: instead of showing a black-box score, the frontend
displays concrete, data-driven sentences explaining WHY a KOL was
recommended.

Each KOL receives >= 3 reasons, selected from the checks below.
Reasons are generated in the caller's locale ("en" or "zh") so the
dashboard reads consistently regardless of UI language.
"""
from typing import List

from engine.models import KOL

# ── Tier thresholds ────────────────────────────────────────────────
_TIER_MAP = {
    "Mega":  1_000_000,
    "Macro":   100_000,
    "Micro":    10_000,
    "Nano":          0,
}

# ── Country display names (by locale) ────────────────────────────────
_COUNTRY_NAME = {
    "en": {
        "MY": "Malaysia", "ID": "Indonesia", "TH": "Thailand",
        "PH": "Philippines", "SG": "Singapore", "VN": "Vietnam",
    },
    "zh": {
        "MY": "马来西亚", "ID": "印度尼西亚", "TH": "泰国",
        "PH": "菲律宾", "SG": "新加坡", "VN": "越南",
    },
}

# ── Category display names (by locale) ───────────────────────────────
_CATEGORY_NAME = {
    "en": {
        "beauty": "beauty & personal care", "fashion": "fashion & lifestyle",
        "home": "home & living", "fmcg": "FMCG",
    },
    "zh": {
        "beauty": "美妆个护", "fashion": "服饰生活",
        "home": "家居生活", "fmcg": "快消品",
    },
}

# ── Tier reach descriptions (by locale) ───────────────────────────────
_TIER_DESC = {
    "en": {
        "Mega":  "maximum brand awareness reach",
        "Macro": "broad coverage with strong credibility",
        "Micro": "high conversion rate and precise audience targeting",
        "Nano":  "highly engaged niche community",
    },
    "zh": {
        "Mega":  "曝光度最大化，适合打造品牌声量",
        "Macro": "覆盖面广，且具备较强的可信度",
        "Micro": "转化率高，受众定位精准",
        "Nano":  "垂直社群活跃度高",
    },
}


def get_tier(kol: KOL) -> str:
    """Return tier string — delegates to KOL.tier property."""
    return kol.tier


def generate_reasons(kol: KOL, all_kols: List[KOL], lang: str = "en") -> List[str]:
    """
    Generate a list of recommendation reasons for a single KOL.

    Args:
        kol:      The KOL to explain.
        all_kols: Full candidate pool, used to compute relative benchmarks.
        lang:     "en" or "zh" — anything else falls back to "en".

    Returns:
        List of >= 3 human-readable reason strings, in the requested locale.
    """
    lang = lang if lang in _COUNTRY_NAME else "en"
    reasons: List[str] = []
    country  = _COUNTRY_NAME[lang].get(kol.country, kol.country)
    category = _CATEGORY_NAME[lang].get(kol.category, kol.category)
    tier     = get_tier(kol)

    # ── 1. Audience market fit ────────────────────────────────────
    fit_pct = int(kol.fit_score * 100)
    if lang == "zh":
        if kol.fit_score >= 0.80:
            reasons.append(f"{country}受众匹配度达 {fit_pct}%，远超 80% 的门槛")
        elif kol.fit_score >= 0.60:
            reasons.append(f"{country}受众契合度良好，达到 {fit_pct}%")
        else:
            reasons.append(f"该达人在{country}市场的受众契合度为 {fit_pct}%")
    else:
        if kol.fit_score >= 0.80:
            reasons.append(
                f"{country} audience match is {fit_pct}%, well above the 80% threshold"
            )
        elif kol.fit_score >= 0.60:
            reasons.append(
                f"Solid {country} audience fit at {fit_pct}%"
            )
        else:
            reasons.append(
                f"Audience fit score is {fit_pct}% for {country} market"
            )

    # ── 2. Engagement rate ────────────────────────────────────────
    eng_pct = round(kol.engagement_rate * 100, 1)
    avg_eng = sum(k.engagement_rate for k in all_kols) / len(all_kols) if all_kols else 0
    if kol.engagement_rate >= avg_eng * 1.2:
        lift_pct = round((kol.engagement_rate / avg_eng - 1) * 100)
        if lang == "zh":
            reasons.append(f"互动率 {eng_pct}%，比候选池平均水平高出 {lift_pct}%")
        else:
            reasons.append(
                f"Engagement rate {eng_pct}% is {lift_pct}% "
                f"above the candidate-pool average"
            )
    else:
        if lang == "zh":
            reasons.append(f"在{category}内容上的互动率为 {eng_pct}%")
        else:
            reasons.append(
                f"Engagement rate of {eng_pct}% in {category} content"
            )

    # ── 3. Tier & reach ───────────────────────────────────────────
    followers_fmt = (
        f"{kol.followers / 1_000_000:.1f}M"
        if kol.followers >= 1_000_000
        else f"{kol.followers // 1_000}K"
    )
    tier_desc = _TIER_DESC[lang][tier]
    if lang == "zh":
        reasons.append(f"{tier} 达人，粉丝 {followers_fmt} —— {tier_desc}")
    else:
        reasons.append(f"{tier} KOL with {followers_fmt} followers — {tier_desc}")

    # ── 4. Cost-effectiveness (conditional) ──────────────────────
    gmv = kol.expected_gmv()
    cost_eff = gmv / kol.cost if kol.cost > 0 else 0
    positive_cost_kols = [k for k in all_kols if k.cost > 0]
    avg_ce = (
        sum(k.expected_gmv() / k.cost for k in positive_cost_kols)
        / len(positive_cost_kols)
        if positive_cost_kols
        else 0
    )
    if cost_eff > 0 and avg_ce > 0 and cost_eff >= avg_ce * 1.15:
        roi_pct = round((cost_eff / avg_ce - 1) * 100)
        if lang == "zh":
            reasons.append(f"预测投资回报率比同池平均水平高出 {roi_pct}% —— 性价比很高")
        else:
            reasons.append(
                f"Predicted ROI is {roi_pct}% "
                f"above the pool average — high cost-effectiveness"
            )

    # ── 5. High absolute GMV (conditional) ───────────────────────
    avg_gmv = sum(k.expected_gmv() for k in all_kols) / len(all_kols) if all_kols else 0
    if gmv >= avg_gmv * 1.3:
        if lang == "zh":
            reasons.append(f"预测 GMV 为 ${gmv:,.0f}，在候选池中名列前茅")
        else:
            reasons.append(
                f"Predicted GMV of ${gmv:,.0f} is among the top performers in this pool"
            )

    # Guarantee at least 3 reasons
    while len(reasons) < 3:
        if lang == "zh":
            reasons.append(f"适合在{country}投放{category}相关的推广活动")
        else:
            reasons.append(f"Recommended for {category} campaigns in {country}")

    return reasons
