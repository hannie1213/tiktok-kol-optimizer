from dataclasses import dataclass
import math

_CTR = {
    "beauty":  {"MY": 0.035, "ID": 0.032, "TH": 0.040, "PH": 0.030, "SG": 0.038, "VN": 0.038},
    "fashion": {"MY": 0.033, "ID": 0.030, "TH": 0.036, "PH": 0.028, "SG": 0.036, "VN": 0.034},
    "home":    {"MY": 0.028, "ID": 0.025, "TH": 0.030, "PH": 0.023, "SG": 0.030, "VN": 0.027},
    "fmcg":    {"MY": 0.040, "ID": 0.037, "TH": 0.044, "PH": 0.035, "SG": 0.042, "VN": 0.041},
}

_CVR = {
    "beauty":  {"MY": 0.085, "ID": 0.075, "TH": 0.090, "PH": 0.070, "SG": 0.095, "VN": 0.072},
    "fashion": {"MY": 0.075, "ID": 0.065, "TH": 0.080, "PH": 0.060, "SG": 0.085, "VN": 0.062},
    "home":    {"MY": 0.060, "ID": 0.050, "TH": 0.065, "PH": 0.045, "SG": 0.068, "VN": 0.046},
    "fmcg":    {"MY": 0.095, "ID": 0.085, "TH": 0.105, "PH": 0.078, "SG": 0.108, "VN": 0.082},
}

_AOV = {
    "beauty":  {"MY": 42,  "ID": 35,  "TH": 38,  "PH": 32,  "SG": 60,  "VN": 28},
    "fashion": {"MY": 55,  "ID": 45,  "TH": 50,  "PH": 40,  "SG": 72,  "VN": 34},
    "home":    {"MY": 45,  "ID": 38,  "TH": 42,  "PH": 32,  "SG": 65,  "VN": 28},
    "fmcg":    {"MY": 22,  "ID": 18,  "TH": 20,  "PH": 16,  "SG": 32,  "VN": 11},
}

# ── Extended categories (EchoTik-style breadth) ──────────────────────
# The four tables above stay authoritative and unchanged. Additional
# verticals are generated from a Malaysia-anchored average order value
# scaled by a per-market factor, plus a category-level CTR/CVR. These are
# planning estimates grounded in SEA market research; the campaign
# calibration layer corrects them against each merchant's real results.
_MARKET_AOV_FACTOR = {"MY": 1.0, "TH": 0.9, "ID": 0.83, "PH": 0.76, "SG": 1.45, "VN": 0.66}
_EXT_CATEGORIES = {
    # category:        (ctr,   cvr,   aov_my)
    "accessories":     (0.034, 0.072, 35),   # 时尚配件 / 箱包 / 鞋靴
    "muslim_fashion":  (0.033, 0.074, 45),   # 穆斯林时尚 (MY/ID festival demand)
    "kitchen":         (0.029, 0.063, 30),   # 厨房用品
    "appliances":      (0.031, 0.058, 85),   # 家电 / 小家电
    "electronics":     (0.030, 0.055, 120),  # 手机数码 / 电脑办公 / 3C
    "mother_baby":     (0.038, 0.092, 30),   # 母婴用品 ("得母婴者得东南亚")
    "health":          (0.032, 0.070, 55),   # 保健 / 健康
    "sports_outdoor":  (0.030, 0.062, 50),   # 运动与户外
    "pet":             (0.039, 0.078, 32),   # 宠物用品
    "toys":            (0.035, 0.072, 28),   # 玩具和爱好
    "automotive":      (0.028, 0.050, 45),   # 汽车与运输 (VN motorbike / TH car)
}
for _cat, (_ctr, _cvr, _aov_my) in _EXT_CATEGORIES.items():
    _CTR[_cat] = {c: _ctr for c in _MARKET_AOV_FACTOR}
    _CVR[_cat] = {c: _cvr for c in _MARKET_AOV_FACTOR}
    _AOV[_cat] = {c: round(_aov_my * f) for c, f in _MARKET_AOV_FACTOR.items()}

_PURCHASING_POWER = {
    "MY": 1.0,   
    "TH": 0.9,   
    "ID": 0.7,   
    "PH": 0.6,   
    "SG": 1.3,   
    "VN": 0.55,  
}

@dataclass
class KOL:
    id: int
    name: str
    country: str          # MY, ID, TH, PH, SG, VN
    category: str         # beauty, fashion, home, fmcg
    followers: int
    engagement_rate: float   # 0~1, historical engagement rate
    fit_score: float         # 0~1, cultural fit score
    commission_rate: float = 0.15  # 0~1, commission % of GMV (e.g. 0.15 = 15%)
    cost: float = 0.0             # Cost in USD (= commission_rate × expected_gmv)
    avg_views:    int   = 0
    avg_likes:    int   = 0
    gender_ratio: float = 0.5
    age_group:    str   = "18-24"

    @property
    def tier(self) -> str:
        if self.followers >= 1_000_000:
            return "Mega"
        if self.followers >= 100_000:
            return "Macro"
        if self.followers >= 10_000:
            return "Micro"
        return "Nano"

    def expected_gmv(self) -> float:
        cat = self.category if self.category in _CTR else "beauty"
        cty = self.country  if self.country  in _PURCHASING_POWER else "MY"

        ctr = _CTR.get(cat, {}).get(cty, 0.030)
        cvr = _CVR.get(cat, {}).get(cty, 0.060)
        aov = _AOV.get(cat, {}).get(cty, 50)
        pp  = _PURCHASING_POWER.get(cty, 1.0)

        traction    = math.sqrt(max(1, self.followers))
        funnel_rate = ctr * cvr

        _ENG_BASE = 0.132
        _FIT_BASE = 0.650

        engagement_factor = math.sqrt(max(1e-4, self.engagement_rate) / _ENG_BASE)
        fit_factor        = math.sqrt(max(1e-4, self.fit_score)        / _FIT_BASE)

        SCALE = 200

        return round(
            traction * funnel_rate * aov * pp * engagement_factor * fit_factor * SCALE,
            2
        )
