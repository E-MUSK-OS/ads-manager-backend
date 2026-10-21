from typing import Any

DATE_FILTERS = {
    "TODAY": "Today",
    "YESTERDAY": "Yesterday",
    "LAST_7_DAYS": "Last 7 Days",
    "THIS_WEEK": "This Week",
    "LAST_WEEK": "Last Week",
    "LAST_30_DAYS": "Last 30 Days",
    "THIS_MONTH": "This Month",
    "LAST_MONTH": "Last Month",
    "YEAR_TO_DATE": "Year to Date",
    "LIFETIME": "Lifetime",
}

METRICS = {  # key: (label, unit)  unit in count | money | percent | ratio
    "impressions": ("Impressions", "count"),
    "clicks": ("Clicks", "count"),
    "orders": ("Orders", "count"),
    "sales": ("Sales", "money"),
    "acos": ("ACOS", "percent"),
    "roas": ("ROAS", "ratio"),
    "spend": ("Spend", "money"),
    "cpc": ("CPC", "money"),
    "ctr": ("CTR", "percent"),
    "cvr": ("CVR", "percent"),
}

OPERATORS = {
    "LT": ("Less Than", "<"),
    "LTE": ("Less Than or Equal To", "<="),
    "GTE": ("Greater Than or Equal To", ">="),
    "GT": ("Greater Than", ">"),
    "EQ": ("Equal To", "=="),
}

AFFECTED_PARAMETERS = {
    "CAMPAIGN": "Campaign",
    "PLACEMENT": "Placement",
    "TARGETING": "Targeting",
    "SEARCH_TERM": "Search Term",
}

LIMITS = {
    "max_conditions": 6,
    "max_actions": 4,
    "max_rules_per_account": 50,
    "name_max": 60,
    "max_open_per_rule": 200,
    "cooldown_days": 7,
}

# Per affected parameter: allowed actions and their parameter schema (UI form + validation are generated from this).
ACTIONS = {
    "SEARCH_TERM": {
        "ADD_AS_TARGETING": {
            "label": "Add as Targeting",
            "params": {
                "match_type": {"type": "enum", "options": ["EXACT", "PHRASE", "BROAD"], "default": "EXACT", "label": "Match type"},
                "bid": {"type": "money", "optional": True, "label": "Bid (blank = ad group default)"},
            },
        },
        "ADD_AS_NEGATIVE": {
            "label": "Add as Negative Targeting",
            "params": {
                "match_type": {"type": "enum", "options": ["NEGATIVE_EXACT", "NEGATIVE_PHRASE"], "default": "NEGATIVE_EXACT", "label": "Match type"},
                "level": {"type": "enum", "options": ["AD_GROUP", "CAMPAIGN"], "default": "AD_GROUP", "label": "Level"},
            },
        },
    },
    "TARGETING": {
        "INCREASE_BID": {
            "label": "Increase bid",
            "params": {"pct": {"type": "percent", "min": 1, "max": 500, "default": 10, "label": "By %"}},
        },
        "DECREASE_BID": {
            "label": "Decrease bid",
            "params": {"pct": {"type": "percent", "min": 1, "max": 90, "default": 10, "label": "By %"}},
        },
        "PAUSE_TARGET": {"label": "Pause target", "params": {}},
    },
    "CAMPAIGN": {
        "INCREASE_BUDGET": {
            "label": "Increase daily budget",
            "params": {"pct": {"type": "percent", "min": 1, "max": 500, "default": 10, "label": "By %"}},
        },
        "DECREASE_BUDGET": {
            "label": "Decrease daily budget",
            "params": {"pct": {"type": "percent", "min": 1, "max": 90, "default": 10, "label": "By %"}},
        },
        "PAUSE_CAMPAIGN": {"label": "Pause campaign", "params": {}},
    },
    "PLACEMENT": {  # Phase 5
        "INCREASE_PLACEMENT": {
            "label": "Increase placement adjustment",
            "params": {"points": {"type": "int", "min": 1, "max": 900, "default": 10, "label": "By percentage points"}},
        },
        "DECREASE_PLACEMENT": {
            "label": "Decrease placement adjustment",
            "params": {"points": {"type": "int", "min": 1, "max": 900, "default": 10, "label": "By percentage points"}},
        },
    },
}

CONFLICTS = [
    {"INCREASE_BID", "DECREASE_BID"},
    {"INCREASE_BID", "PAUSE_TARGET"},
    {"DECREASE_BID", "PAUSE_TARGET"},
    {"INCREASE_BUDGET", "DECREASE_BUDGET"},
    {"INCREASE_BUDGET", "PAUSE_CAMPAIGN"},
    {"DECREASE_BUDGET", "PAUSE_CAMPAIGN"},
    {"INCREASE_PLACEMENT", "DECREASE_PLACEMENT"},
    {"ADD_AS_TARGETING", "ADD_AS_NEGATIVE"},
]

TEMPLATES = [
    {
        "key": "negate_wasted_terms",
        "category": "Search terms",
        "name": "Negate search terms that spend with no orders",
        "description": "Stops paying for search terms that cost money and never convert.",
        "affected_parameter": "SEARCH_TERM",
        "date_filter": "LAST_30_DAYS",
        "min_clicks": 0,
        "conditions": [
            {"metric": "spend", "op": "GTE", "value": {"default": 10, "IN": 20}},
            {"metric": "orders", "op": "LTE", "value": 0},
        ],
        "actions": [{"type": "ADD_AS_NEGATIVE", "params": {"match_type": "NEGATIVE_EXACT", "level": "AD_GROUP"}}],
    },
    {
        "key": "harvest_converting_terms",
        "category": "Search terms",
        "name": "Harvest converting search terms",
        "description": "Promotes search terms that already convert efficiently to their own exact-match keyword.",
        "affected_parameter": "SEARCH_TERM",
        "date_filter": "LAST_30_DAYS",
        "min_clicks": 0,
        "conditions": [
            {"metric": "orders", "op": "GTE", "value": 2},
            {"metric": "acos", "op": "LTE", "value": 30},
        ],
        "actions": [{"type": "ADD_AS_TARGETING", "params": {"match_type": "EXACT"}}],
    },
    {
        "key": "pause_bleeding_targets",
        "category": "Targeting",
        "name": "Pause targets with many clicks and no orders",
        "description": "Pauses keywords that keep getting clicks without a single sale.",
        "affected_parameter": "TARGETING",
        "date_filter": "LAST_30_DAYS",
        "min_clicks": 20,
        "conditions": [
            {"metric": "clicks", "op": "GTE", "value": 20},
            {"metric": "orders", "op": "LTE", "value": 0},
        ],
        "actions": [{"type": "PAUSE_TARGET", "params": {}}],
    },
    {
        "key": "lower_bids_high_acos",
        "category": "Targeting",
        "name": "Lower bids on high-ACOS targets",
        "description": "Reduces bids where ACOS is too high but the target still converts.",
        "affected_parameter": "TARGETING",
        "date_filter": "LAST_7_DAYS",
        "min_clicks": 10,
        "conditions": [
            {"metric": "acos", "op": "GTE", "value": 50},
            {"metric": "orders", "op": "GTE", "value": 1},
        ],
        "actions": [{"type": "DECREASE_BID", "params": {"pct": 20}}],
    },
    {
        "key": "raise_bids_efficient",
        "category": "Targeting",
        "name": "Raise bids on efficient targets",
        "description": "Wins more traffic on targets with low ACOS and steady orders.",
        "affected_parameter": "TARGETING",
        "date_filter": "LAST_30_DAYS",
        "min_clicks": 10,
        "conditions": [
            {"metric": "acos", "op": "LTE", "value": 15},
            {"metric": "orders", "op": "GTE", "value": 3},
        ],
        "actions": [{"type": "INCREASE_BID", "params": {"pct": 15}}],
    },
    {
        "key": "scale_winning_campaigns",
        "category": "Campaigns",
        "name": "Scale winning campaigns",
        "description": "Adds budget to campaigns with strong return.",
        "affected_parameter": "CAMPAIGN",
        "date_filter": "LAST_7_DAYS",
        "min_clicks": 0,
        "conditions": [
            {"metric": "roas", "op": "GTE", "value": 4},
            {"metric": "orders", "op": "GTE", "value": 5},
        ],
        "actions": [{"type": "INCREASE_BUDGET", "params": {"pct": 20}}],
    },
    {
        "key": "cut_leaking_campaigns",
        "category": "Campaigns",
        "name": "Cut budget on leaking campaigns",
        "description": "Trims budget where ACOS is very high and spend is meaningful.",
        "affected_parameter": "CAMPAIGN",
        "date_filter": "LAST_7_DAYS",
        "min_clicks": 0,
        "conditions": [
            {"metric": "acos", "op": "GTE", "value": 80},
            {"metric": "spend", "op": "GTE", "value": {"default": 50, "IN": 300}},
        ],
        "actions": [{"type": "DECREASE_BUDGET", "params": {"pct": 25}}],
    },
    {
        "key": "boost_top_of_search",
        "category": "Placements",
        "name": "Boost placements that perform",
        "requires": "placement_metrics",
        "description": "Raises the adjustment on placements with low ACOS.",
        "affected_parameter": "PLACEMENT",
        "date_filter": "LAST_30_DAYS",
        "min_clicks": 10,
        "conditions": [
            {"metric": "acos", "op": "LTE", "value": 20},
            {"metric": "orders", "op": "GTE", "value": 3},
        ],
        "actions": [{"type": "INCREASE_PLACEMENT", "params": {"points": 25}}],
    },
]


def resolve_value(v: Any, marketplace: str) -> Any:
    """Resolve a value that may be marketplace-dependent (e.g. {'default': 10, 'IN': 20})."""
    if isinstance(v, dict):
        if marketplace in v:
            return v[marketplace]
        return v.get("default", v)
    return v


def resolve_template(template: dict, marketplace: str) -> dict:
    t = template.copy()
    resolved_conditions = []
    for c in t.get("conditions", []):
        c_copy = c.copy()
        c_copy["value"] = resolve_value(c_copy.get("value"), marketplace)
        resolved_conditions.append(c_copy)
    t["conditions"] = resolved_conditions

    resolved_actions = []
    for a in t.get("actions", []):
        a_copy = a.copy()
        if "params" in a_copy:
            a_copy["params"] = {k: resolve_value(val, marketplace) for k, val in a_copy["params"].items()}
        resolved_actions.append(a_copy)
    t["actions"] = resolved_actions
    return t
