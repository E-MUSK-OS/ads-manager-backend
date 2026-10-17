from datetime import datetime, date
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

KEYWORD_MAX_LEN, KEYWORD_MAX_WORDS = 80, 10
KEYWORD_FORBIDDEN = set('!@%^()={};~`<>?\\|')      # ASSUMPTIONS for MockAdsProvider
ASIN_RE = r"^[A-Z0-9]{10}$"

MARKETPLACE_RULES = {
  "US": dict(currency="USD", symbol="$", locale="en-US", tz="America/Los_Angeles", min_budget=1.0,  recommended_budget=10.0,  min_bid=0.02, max_bid=49.0),
  "UK": dict(currency="GBP", symbol="£", locale="en-GB", tz="Europe/London",       min_budget=1.0,  recommended_budget=10.0,  min_bid=0.02, max_bid=49.0),
  "DE": dict(currency="EUR", symbol="€", locale="de-DE", tz="Europe/Berlin",       min_budget=1.0,  recommended_budget=10.0,  min_bid=0.02, max_bid=49.0),
  "IN": dict(currency="INR", symbol="₹", locale="en-IN", tz="Asia/Kolkata",        min_budget=50.0, recommended_budget=500.0, min_bid=2.0,  max_bid=5000.0),
}

def marketplace_or_us(marketplace: str | None) -> dict:
    if marketplace and marketplace in MARKETPLACE_RULES:
        return MARKETPLACE_RULES[marketplace]
    return MARKETPLACE_RULES["US"]


def today_for(marketplace: str) -> date:
    rules = marketplace_or_us(marketplace)
    try:
        return datetime.now(ZoneInfo(rules["tz"])).date()
    except Exception:
        # Windows without tzdata used to raise ZoneInfoNotFoundError here and 500 the report endpoint.
        return datetime.now().date()
