from typing import Any
from app.config import settings

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
    "orders": ("Purchases (7d)", "count"),
    "sales": ("Sales (7d)", "money"),
    "acos": ("ACOS", "percent"),
    "roas": ("ROAS", "ratio"),
    "spend": ("Spend", "money"),
    "cpc": ("CPC", "money"),
    "ctr": ("CTR", "percent"),
    "cvr": ("CVR", "percent"),
    "acos_vs_target": ("ACOS vs Target ACOS", "percent"),
    "out_of_budget_days": ("Out of Budget Days", "count"),
    # Derived metrics (Q9)
    "target_spend_share": ("Target Spend Share", "percent"),
    "target_sales_share": ("Target Sales Share", "percent"),
    # Trend metrics (Q10)
    "spend_change_pct": ("Spend Change", "percent"),
    "sales_change_pct": ("Sales Change", "percent"),
    "orders_change_pct": ("Purchases Change", "percent"),
    "acos_change_pct": ("ACOS Change", "percent"),
    "ctr_change_pct": ("CTR Change", "percent"),
    "cvr_change_pct": ("CVR Change", "percent"),
    "cpc_change_pct": ("CPC Change", "percent"),
    # Entity age (Q11)
    "entity_age_days": ("Entity Age", "count"),
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
            "params": {
                "mode": {"type": "enum", "options": ["percent", "amount"], "default": "percent", "label": "Unit"},
                "value": {"type": "percent", "default": 10, "min": 1, "max": 500, "label": "Value"},
            },
        },
        "DECREASE_BID": {
            "label": "Decrease bid",
            "params": {
                "mode": {"type": "enum", "options": ["percent", "amount"], "default": "percent", "label": "Unit"},
                "value": {"type": "percent", "default": 10, "min": 1, "max": 90, "label": "Value"},
            },
        },
        "PAUSE_TARGET": {"label": "Pause target", "params": {}},
    },
    "CAMPAIGN": {
        "INCREASE_BUDGET": {
            "label": "Increase daily budget",
            "params": {
                "mode": {"type": "enum", "options": ["percent", "amount"], "default": "percent", "label": "Unit"},
                "value": {"type": "percent", "default": 10, "min": 1, "max": 500, "label": "Value"},
            },
        },
        "DECREASE_BUDGET": {
            "label": "Decrease daily budget",
            "params": {
                "mode": {"type": "enum", "options": ["percent", "amount"], "default": "percent", "label": "Unit"},
                "value": {"type": "percent", "default": 10, "min": 1, "max": 90, "label": "Value"},
            },
        },
        "SET_BUDGET": {
            "label": "Set daily budget",
            "params": {
                "mode": {"type": "enum", "options": ["amount"], "default": "amount", "label": "Unit"},
                "value": {"type": "money", "min": 100, "default": 100, "label": "Daily budget"},
            },
        },
        "ENABLE_CAMPAIGN": {
            "label": "Enable campaign",
            "params": {},
            "warning": "This will start spending",
        },
        "PAUSE_CAMPAIGN": {"label": "Pause campaign", "params": {}},
    },
    "PLACEMENT": {  # Gated behind settings.ENABLE_PLACEMENT_RULES (F8, Q1)
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
    {"INCREASE_BUDGET", "SET_BUDGET"},
    {"DECREASE_BUDGET", "SET_BUDGET"},
    {"INCREASE_BUDGET", "PAUSE_CAMPAIGN"},
    {"DECREASE_BUDGET", "PAUSE_CAMPAIGN"},
    {"SET_BUDGET", "PAUSE_CAMPAIGN"},
    {"ENABLE_CAMPAIGN", "PAUSE_CAMPAIGN"},
    {"INCREASE_PLACEMENT", "DECREASE_PLACEMENT"},
    {"ADD_AS_TARGETING", "ADD_AS_NEGATIVE"},
]

# Gallery templates: purely examples with thresholds left empty (None) for user to enter (F6, Q2)
TEMPLATES = [
    {
        "key": "negate_wasted_terms",
        "category": "Search terms",
        "name": "Negate search terms that spend with no orders",
        "description": "Example template — edit thresholds before saving. Flags non-converting search terms.",
        "affected_parameter": "SEARCH_TERM",
        "date_filter": "LAST_30_DAYS",
        "min_clicks": 0,
        "conditions": [
            {"metric": "spend", "op": "GTE", "value": None},
            {"metric": "orders", "op": "LTE", "value": None},
        ],
        "actions": [{"type": "ADD_AS_NEGATIVE", "params": {"match_type": "NEGATIVE_EXACT", "level": "AD_GROUP"}}],
    },
    {
        "key": "pause_bleeding_targets",
        "category": "Targeting",
        "name": "Pause targets with many clicks and no orders",
        "description": "Example template — edit thresholds before saving. Flags non-converting keywords.",
        "affected_parameter": "TARGETING",
        "date_filter": "LAST_30_DAYS",
        "min_clicks": 0,
        "conditions": [
            {"metric": "clicks", "op": "GTE", "value": None},
            {"metric": "orders", "op": "LTE", "value": None},
        ],
        "actions": [{"type": "PAUSE_TARGET", "params": {}}],
    },
    {
        "key": "lower_bids_high_acos",
        "category": "Targeting",
        "name": "Lower bids on high-ACOS targets",
        "description": "Example template — edit thresholds before saving. Trims bids on converting targets above target ACOS.",
        "affected_parameter": "TARGETING",
        "date_filter": "LAST_7_DAYS",
        "min_clicks": 0,
        "conditions": [
            {"metric": "acos", "op": "GTE", "value": None},
            {"metric": "orders", "op": "GTE", "value": None},
        ],
        "actions": [{"type": "DECREASE_BID", "params": {"mode": "percent", "value": 10}}],
    },
    {
        "key": "raise_bids_efficient",
        "category": "Targeting",
        "name": "Raise bids on efficient targets",
        "description": "Example template — edit thresholds before saving. Boosts bids for high-efficiency keywords.",
        "affected_parameter": "TARGETING",
        "date_filter": "LAST_30_DAYS",
        "min_clicks": 0,
        "conditions": [
            {"metric": "acos", "op": "LTE", "value": None},
            {"metric": "orders", "op": "GTE", "value": None},
        ],
        "actions": [{"type": "INCREASE_BID", "params": {"mode": "percent", "value": 10}}],
    },
    {
        "key": "scale_winning_campaigns",
        "category": "Campaigns",
        "name": "Scale winning campaigns",
        "description": "Example template — edit thresholds before saving. Expands budget on high-performing campaigns.",
        "affected_parameter": "CAMPAIGN",
        "date_filter": "LAST_7_DAYS",
        "min_clicks": 0,
        "conditions": [
            {"metric": "roas", "op": "GTE", "value": None},
            {"metric": "orders", "op": "GTE", "value": None},
        ],
        "actions": [{"type": "INCREASE_BUDGET", "params": {"mode": "percent", "value": 10}}],
    },
    {
        "key": "cut_leaking_campaigns",
        "category": "Campaigns",
        "name": "Cut budget on leaking campaigns",
        "description": "Example template — edit thresholds before saving. Scales back budget on unprofitable campaigns.",
        "affected_parameter": "CAMPAIGN",
        "date_filter": "LAST_7_DAYS",
        "min_clicks": 0,
        "conditions": [
            {"metric": "acos", "op": "GTE", "value": None},
            {"metric": "spend", "op": "GTE", "value": None},
        ],
        "actions": [{"type": "DECREASE_BUDGET", "params": {"mode": "percent", "value": 10}}],
    },
    {
        "key": "harvest_converting_terms",
        "category": "Search terms",
        "name": "Harvest converting search terms",
        "description": "Example template — edit thresholds before saving. Promotes converting search terms.",
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
        "key": "boost_top_of_search",
        "category": "Placements",
        "name": "Boost placements that perform",
        "requires": "placement_metrics",
        "description": "Example template — edit thresholds before saving. Raises adjustment on high-performing placements.",
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

def get_visible_affected_parameters() -> dict[str, str]:
    params = dict(AFFECTED_PARAMETERS)
    if not getattr(settings, "ENABLE_PLACEMENT_RULES", False):
        params.pop("PLACEMENT", None)
    return params

def get_visible_actions() -> dict[str, dict[str, Any]]:
    actions = {k: dict(v) for k, v in ACTIONS.items()}
    if not getattr(settings, "ENABLE_PLACEMENT_RULES", False):
        actions.pop("PLACEMENT", None)
    # ADD_AS_TARGETING stays hidden until approved per Q1
    if "SEARCH_TERM" in actions and not getattr(settings, "ENABLE_ADD_AS_TARGETING", False):
        actions["SEARCH_TERM"].pop("ADD_AS_TARGETING", None)
    return actions

def get_visible_templates() -> list[dict[str, Any]]:
    templates = []
    vis_actions = get_visible_actions()
    for t in TEMPLATES:
        if t["affected_parameter"] == "PLACEMENT" and not getattr(settings, "ENABLE_PLACEMENT_RULES", False):
            continue
        param = t["affected_parameter"]
        allowed = vis_actions.get(param, {})
        if any(a.get("type") not in allowed for a in t.get("actions", [])):
            continue
        templates.append(t)
    return templates


def resolve_value(v: Any, marketplace: str) -> Any:
    """Resolve a value that may be marketplace-dependent (e.g. {'default': 10, 'IN': 20})."""
    if v is None:
        return None
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
