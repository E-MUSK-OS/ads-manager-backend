from datetime import datetime, date
from typing import Any
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

KEYWORD_MAX_LEN, KEYWORD_MAX_WORDS = 80, 10
KEYWORD_FORBIDDEN = set('!@%^()={};~`<>?\\|')      # ASSUMPTIONS for MockAdsProvider
ASIN_RE = r"^[A-Z0-9]{10}$"

MARKETPLACE_RULES: dict[str, dict[str, Any]] = {
    "IN": {
        "currency_code": "INR",
        "currency_symbol": "₹",
        "currency": "INR",
        "symbol": "₹",
        "locale": "en-IN",
        "timezone": "Asia/Kolkata",
        "tz": "Asia/Kolkata",
        "min_daily_budget": 100.0,
        "max_daily_budget": 20000000.0,
        "min_budget": 50.0,
        "recommended_budget": 500.0,
        "min_bid": 1.0,
        "max_bid": 5000.0,
        "default_bid": 15.0,
    },
    "US": {
        "currency_code": "USD",
        "currency_symbol": "$",
        "currency": "USD",
        "symbol": "$",
        "locale": "en-US",
        "timezone": "America/Los_Angeles",
        "tz": "America/Los_Angeles",
        "min_daily_budget": 1.0,
        "max_daily_budget": 1000000.0,
        "min_budget": 1.0,
        "recommended_budget": 10.0,
        "min_bid": 0.02,
        "max_bid": 49.0,
        "default_bid": 0.75,
    },
    "UK": {
        "currency_code": "GBP",
        "currency_symbol": "£",
        "currency": "GBP",
        "symbol": "£",
        "locale": "en-GB",
        "timezone": "Europe/London",
        "tz": "Europe/London",
        "min_daily_budget": 1.0,
        "max_daily_budget": 1000000.0,
        "min_budget": 1.0,
        "recommended_budget": 10.0,
        "min_bid": 0.02,
        "max_bid": 49.0,
        "default_bid": 0.75,
    },
    "DE": {
        "currency_code": "EUR",
        "currency_symbol": "€",
        "currency": "EUR",
        "symbol": "€",
        "locale": "de-DE",
        "timezone": "Europe/Berlin",
        "tz": "Europe/Berlin",
        "min_daily_budget": 1.0,
        "max_daily_budget": 1000000.0,
        "min_budget": 1.0,
        "recommended_budget": 10.0,
        "min_bid": 0.02,
        "max_bid": 49.0,
        "default_bid": 0.75,
    },
}

def marketplace_or_in(marketplace: str | None) -> dict[str, Any]:
    if marketplace and marketplace in MARKETPLACE_RULES:
        return MARKETPLACE_RULES[marketplace]
    return MARKETPLACE_RULES["IN"]

def marketplace_or_us(marketplace: str | None) -> dict[str, Any]:
    if marketplace and marketplace in MARKETPLACE_RULES:
        return MARKETPLACE_RULES[marketplace]
    return MARKETPLACE_RULES["IN"]

def today_for(marketplace: str | None) -> date:
    rules = marketplace_or_in(marketplace)
    try:
        return datetime.now(ZoneInfo(rules["tz"])).date()
    except Exception:
        # Windows without tzdata fallback
        return datetime.now().date()

def compute_max_effective_bid(
    base_bid: float,
    placement_pct: int,
    bidding_strategy: str,
    placement: str,
) -> float:
    """
    Amazon Ads compound auction bid formula:
    $$\\text{MaxBid} = \\text{BaseBid} \\times \\left(1 + \\frac{\\text{ clamp}(\\text{PlacementPct}, 0, 900)}{100}\\right) \\times \\text{DynamicFactor}$$
    Where DynamicFactor for 'UP_AND_DOWN' is 2.0 for 'TOP_OF_SEARCH' (+100%)
    and 1.5 for 'REST_OF_SEARCH' or 'PRODUCT_PAGES' (+50%).
    For 'DOWN_ONLY' and 'FIXED', DynamicFactor = 1.0.
    Example: ₹15.00 base bid with +100% Top of Search and UP_AND_DOWN = ₹15.00 * 2.0 * 2.0 = ₹60.00.
    """
    clamped_pct = max(0, min(900, int(placement_pct or 0)))
    with_placement = float(base_bid or 0.0) * (1.0 + clamped_pct / 100.0)
    strategy_key = (bidding_strategy or "DOWN_ONLY").upper()
    placement_key = (placement or "REST_OF_SEARCH").upper()

    if strategy_key in ("UP_AND_DOWN", "AUTO_FOR_SALES"):
        dynamic_factor = 2.0 if placement_key in ("TOP_OF_SEARCH", "TOP_PCT", "PLACEMENT_TOP") else 1.5
    else:
        dynamic_factor = 1.0

    return round(with_placement * dynamic_factor, 2)
