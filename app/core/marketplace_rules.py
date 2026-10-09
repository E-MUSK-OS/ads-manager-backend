from datetime import datetime, date
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

KEYWORD_MAX_LEN, KEYWORD_MAX_WORDS = 80, 10
KEYWORD_FORBIDDEN = set('!@%^()={};~`<>?\\|')      # ASSUMPTIONS for MockAdsProvider

MARKETPLACE_RULES = {
  "US": dict(currency="USD", symbol="$", locale="en-US", tz="America/Los_Angeles", min_budget=1.0,  recommended_budget=10.0,  min_bid=0.02, max_bid=49.0),
  "UK": dict(currency="GBP", symbol="£", locale="en-GB", tz="Europe/London",       min_budget=1.0,  recommended_budget=10.0,  min_bid=0.02, max_bid=49.0),
  "DE": dict(currency="EUR", symbol="€", locale="de-DE", tz="Europe/Berlin",       min_budget=1.0,  recommended_budget=10.0,  min_bid=0.02, max_bid=49.0),
  "IN": dict(currency="INR", symbol="₹", locale="en-IN", tz="Asia/Kolkata",        min_budget=50.0, recommended_budget=500.0, min_bid=2.0,  max_bid=5000.0),
}

def today_for(marketplace: str) -> date: 
    rules = MARKETPLACE_RULES.get(marketplace, MARKETPLACE_RULES["US"])
    return datetime.now(ZoneInfo(rules["tz"])).date()
