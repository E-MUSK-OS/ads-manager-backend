from typing import Literal, Optional, Any
from pydantic import BaseModel, Field, ConfigDict
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from fastapi import HTTPException

from app.recommendations.registry import (
    DATE_FILTERS,
    METRICS,
    OPERATORS,
    AFFECTED_PARAMETERS,
    ACTIONS,
    CONFLICTS,
    LIMITS,
)
from app.core.exceptions import ServiceValidationError
from app.core.marketplace_rules import marketplace_or_in
from app.models.campaign import Campaign
from app.models.ads_account import AdsAccount
from app.models.recommendation_rule import RecommendationRule


class ConditionIn(BaseModel):
    metric: str
    op: str
    value: float


class ActionIn(BaseModel):
    type: str
    params: dict[str, Any] = Field(default_factory=dict)


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    affected_parameter: Literal["CAMPAIGN", "PLACEMENT", "TARGETING", "SEARCH_TERM"]
    date_filter: str = "LAST_30_DAYS"
    min_clicks: int = Field(0, ge=0, le=100000)
    conditions: list[ConditionIn] = Field(min_length=1, max_length=6)
    actions: list[ActionIn] = Field(default_factory=list, min_length=0, max_length=4)
    is_active: bool = True
    scope_mode: Literal["ALL", "INCLUDE", "EXCLUDE"] = "ALL"
    campaign_ids: list[int] = Field(default_factory=list)
    template_key: Optional[str] = None
    ads_account_id: Optional[int] = None


class RuleResponse(BaseModel):
    id: int
    ads_account_id: int
    name: str
    affected_parameter: str
    date_filter: str
    min_clicks: int
    conditions: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    scope_mode: str
    campaign_ids: list[int]
    is_active: bool
    template_key: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    last_run_at: Optional[datetime] = None
    last_match_count: int = 0
    open_count: int = 0
    scope_label: str
    last_funnel: Optional[dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)


class CreateRuleResponse(BaseModel):
    rule: RuleResponse
    run: Optional[dict[str, Any]] = None


class ActiveUpdate(BaseModel):
    is_active: bool


class ScopeUpdate(BaseModel):
    scope_mode: Literal["ALL", "INCLUDE", "EXCLUDE"]
    campaign_ids: list[int] = Field(default_factory=list)


class BulkRulesRequest(BaseModel):
    ids: list[int]
    action: Literal["ENABLE", "DISABLE", "DELETE"]


class PreviewRequest(BaseModel):
    ads_account_id: int
    rule: RuleIn


async def validate_rule_in(
    db: AsyncSession,
    account: AdsAccount,
    rule: RuleIn,
    rule_id: Optional[int] = None,
) -> None:
    errors: list[dict[str, str]] = []
    marketplace_rules = marketplace_or_in(account.marketplace)

    # 1. metric/op/date_filter known
    if rule.date_filter not in DATE_FILTERS:
        errors.append({
            "field": "date_filter",
            "message": f"Unknown date filter '{rule.date_filter}'",
        })

    seen_cond_pairs = set()
    for idx, cond in enumerate(rule.conditions):
        if cond.metric not in METRICS:
            errors.append({
                "field": f"conditions.{idx}.metric",
                "message": f"Unknown metric '{cond.metric}'",
            })
        if cond.op not in OPERATORS:
            errors.append({
                "field": f"conditions.{idx}.op",
                "message": f"Unknown operator '{cond.op}'",
            })

        # 2. value numeric validation
        if cond.metric == "acos_vs_target":
            if cond.value < -100 or cond.value > 100:
                errors.append({
                    "field": f"conditions.{idx}.value",
                    "message": "Offset must be between -100 and 100 percentage points",
                })
        elif cond.metric.endswith("_change_pct"):
            if cond.value < -100 or cond.value > 1000:
                errors.append({
                    "field": f"conditions.{idx}.value",
                    "message": "Percentage change must be between -100% and 1000%",
                })
        elif cond.metric in ("target_spend_share", "target_sales_share"):
            if rule.affected_parameter not in ("TARGETING", "SEARCH_TERM"):
                errors.append({
                    "field": f"conditions.{idx}.metric",
                    "message": f"{cond.metric} condition is only allowed for TARGETING and SEARCH_TERM rules",
                })
            if cond.value < 0 or cond.value > 100:
                errors.append({
                    "field": f"conditions.{idx}.value",
                    "message": "Share percentage must be between 0 and 100%",
                })
        elif cond.metric == "out_of_budget_days":
            if rule.affected_parameter != "CAMPAIGN":
                errors.append({
                    "field": f"conditions.{idx}.metric",
                    "message": "out_of_budget_days condition is only allowed for CAMPAIGN rules",
                })
            if cond.value < 1 or cond.value > 14:
                errors.append({
                    "field": f"conditions.{idx}.value",
                    "message": "Out of budget days must be between 1 and 14",
                })
        elif cond.value < 0:
            errors.append({
                "field": f"conditions.{idx}.value",
                "message": "Value must be greater than or equal to 0",
            })
        elif cond.metric in METRICS:
            unit = METRICS[cond.metric][1]
            if unit in ("percent", "ratio") and cond.value > 1000:
                errors.append({
                    "field": f"conditions.{idx}.value",
                    "message": "Value must be less than or equal to 1000",
                })

        pair = (cond.metric, cond.op)
        if pair in seen_cond_pairs:
            errors.append({
                "field": f"conditions.{idx}",
                "message": f"Duplicate condition for metric '{cond.metric}' with operator '{cond.op}'",
            })
        else:
            seen_cond_pairs.add(pair)

    # 3. One-action validator for TARGETING and CAMPAIGN, and Out-of-Budget display-only validator
    has_oob_cond = any(c.metric == "out_of_budget_days" for c in rule.conditions)
    if has_oob_cond:
        if rule.affected_parameter != "CAMPAIGN":
            errors.append({
                "field": "conditions",
                "message": "out_of_budget_days condition is only allowed for CAMPAIGN rules",
            })
        if len(rule.actions) > 0:
            errors.append({
                "field": "actions",
                "message": "Rules with Out of Budget condition are display-only and cannot have actions",
            })
    elif rule.affected_parameter in ("TARGETING", "CAMPAIGN"):
        if len(rule.actions) != 1:
            errors.append({
                "field": "actions",
                "message": f"Rules for {rule.affected_parameter} must define exactly one action",
            })
    elif rule.affected_parameter == "SEARCH_TERM":
        if len(rule.actions) == 0:
            errors.append({
                "field": "actions",
                "message": "At least one action is required for SEARCH_TERM rules",
            })

    # 4. Action allowed for the affected parameter and param validations
    param_actions = ACTIONS.get(rule.affected_parameter, {})
    for idx, action in enumerate(rule.actions):
        if action.type not in param_actions:
            errors.append({
                "field": f"actions.{idx}.type",
                "message": f"Action '{action.type}' is not allowed for affected parameter '{rule.affected_parameter}'",
            })
            continue

        # Normalise legacy pct param
        has_orig_pct = ("pct" in action.params and "value" not in action.params)
        has_orig_percentage = ("percentage" in action.params and "value" not in action.params)
        if has_orig_pct:
            action.params["mode"] = "percent"
            action.params["value"] = action.params["pct"]
        elif has_orig_percentage:
            action.params["mode"] = "percent"
            action.params["value"] = action.params["percentage"]

        val_key = "pct" if has_orig_pct else ("percentage" if has_orig_percentage else "value")

        if action.type in ("INCREASE_BID", "DECREASE_BID", "INCREASE_BUDGET", "DECREASE_BUDGET"):
            mode = action.params.get("mode", "percent")
            action.params["mode"] = mode
            if mode not in ("percent", "amount"):
                errors.append({
                    "field": f"actions.{idx}.params.mode",
                    "message": "Unit must be 'percent' or 'amount'",
                })
            val = action.params.get("value")
            if val is None or val == "":
                errors.append({
                    "field": f"actions.{idx}.params.{val_key}",
                    "message": "Value is required",
                })
            else:
                try:
                    num_val = float(val)
                    if mode == "percent":
                        if "INCREASE" in action.type:
                            if num_val < 1 or num_val > 500:
                                errors.append({
                                    "field": f"actions.{idx}.params.{val_key}",
                                    "message": "Percentage increase must be between 1 and 500",
                                })
                        else:
                            if num_val < 1 or num_val > 90:
                                errors.append({
                                    "field": f"actions.{idx}.params.{val_key}",
                                    "message": "Percentage decrease must be between 1 and 90",
                                })
                    elif mode == "amount":
                        if num_val <= 0:
                            errors.append({
                                "field": f"actions.{idx}.params.{val_key}",
                                "message": "Amount must be greater than 0",
                            })
                    action.params["value"] = num_val
                except (ValueError, TypeError):
                    errors.append({
                        "field": f"actions.{idx}.params.{val_key}",
                        "message": "Value must be a valid number",
                    })

        elif action.type == "SET_BUDGET":
            action.params["mode"] = "amount"
            val = action.params.get("value")
            if val is None or val == "":
                errors.append({
                    "field": f"actions.{idx}.params.value",
                    "message": "Daily budget is required",
                })
            else:
                try:
                    num_val = float(val)
                    min_budget = float(marketplace_rules["min_budget"])
                    if num_val < min_budget:
                        errors.append({
                            "field": f"actions.{idx}.params.value",
                            "message": f"Daily budget must be at least ₹{min_budget:g}",
                        })
                    action.params["value"] = num_val
                except (ValueError, TypeError):
                    errors.append({
                        "field": f"actions.{idx}.params.value",
                        "message": "Daily budget must be a valid number",
                    })

        elif action.type in ("ENABLE_CAMPAIGN", "PAUSE_CAMPAIGN", "PAUSE_TARGET"):
            # No parameters required
            pass

        elif action.type == "ADD_AS_TARGETING":
            m_type = action.params.get("match_type", "EXACT")
            if m_type not in ("EXACT", "PHRASE", "BROAD"):
                errors.append({
                    "field": f"actions.{idx}.params.match_type",
                    "message": "Match type must be EXACT, PHRASE, or BROAD",
                })
            action.params["match_type"] = m_type
            bid_val = action.params.get("bid")
            if bid_val is not None and bid_val != "":
                try:
                    num_bid = float(bid_val)
                    max_bid = float(marketplace_rules["max_bid"])
                    if num_bid < 0:
                        errors.append({
                            "field": f"actions.{idx}.params.bid",
                            "message": "Bid must be greater than or equal to 0",
                        })
                    elif num_bid > max_bid:
                        errors.append({
                            "field": f"actions.{idx}.params.bid",
                            "message": f"Bid must be less than or equal to {max_bid}",
                        })
                    action.params["bid"] = num_bid
                except (ValueError, TypeError):
                    errors.append({
                        "field": f"actions.{idx}.params.bid",
                        "message": "Must be a valid money value",
                    })

        elif action.type == "ADD_AS_NEGATIVE":
            m_type = action.params.get("match_type", "NEGATIVE_EXACT")
            if m_type not in ("NEGATIVE_EXACT", "NEGATIVE_PHRASE"):
                errors.append({
                    "field": f"actions.{idx}.params.match_type",
                    "message": "Match type must be NEGATIVE_EXACT or NEGATIVE_PHRASE",
                })
            action.params["match_type"] = m_type
            lvl = action.params.get("level", "AD_GROUP")
            if lvl not in ("AD_GROUP", "CAMPAIGN"):
                errors.append({
                    "field": f"actions.{idx}.params.level",
                    "message": "Level must be AD_GROUP or CAMPAIGN",
                })
            action.params["level"] = lvl

        elif action.type in ("INCREASE_PLACEMENT", "DECREASE_PLACEMENT"):
            pts = action.params.get("points")
            if pts is None or pts == "":
                action.params["points"] = 10
            else:
                try:
                    pts_num = int(pts)
                    if pts_num < 1 or pts_num > 900:
                        errors.append({
                            "field": f"actions.{idx}.params.points",
                            "message": "Points must be between 1 and 900",
                        })
                    action.params["points"] = pts_num
                except (ValueError, TypeError):
                    errors.append({
                        "field": f"actions.{idx}.params.points",
                        "message": "Points must be an integer",
                    })

    # 5. no two actions with the same type unless they differ in match_type
    seen_types: dict[str, Any] = {}
    for idx, action in enumerate(rule.actions):
        m_type = action.params.get("match_type")
        if action.type in seen_types:
            prev_m_type = seen_types[action.type]
            if not m_type or not prev_m_type or m_type == prev_m_type:
                errors.append({
                    "field": f"actions.{idx}",
                    "message": f"Duplicate action type '{action.type}'",
                })
        else:
            seen_types[action.type] = m_type

    # 6. no pair from CONFLICTS
    action_type_set = {a.type for a in rule.actions}
    for conflict_pair in CONFLICTS:
        if conflict_pair.issubset(action_type_set):
            errors.append({
                "field": "actions",
                "message": f"Conflicting actions: {', '.join(sorted(conflict_pair))}",
            })

    # 7. INCLUDE/EXCLUDE need >= 1 campaign and every campaign must belong to the account
    if rule.scope_mode in ("INCLUDE", "EXCLUDE"):
        if not rule.campaign_ids:
            errors.append({
                "field": "campaign_ids",
                "message": f"Select at least one campaign for scope mode {rule.scope_mode}",
            })
        else:
            c_res = await db.execute(
                select(Campaign.id).where(
                    Campaign.ads_account_id == account.id,
                    Campaign.id.in_(rule.campaign_ids),
                )
            )
            owned_ids = set(c_res.scalars().all())
            for cid in rule.campaign_ids:
                if cid not in owned_ids:
                    errors.append({
                        "field": "campaign_ids",
                        "message": f"Campaign {cid} does not belong to this account",
                    })
                    break

    # If any validation errors accumulated, raise ServiceValidationError
    if errors:
        raise ServiceValidationError(errors)

    # 8. Name unique per account (case-insensitive) -> 409
    clean_name = rule.name.strip()
    name_key = clean_name.lower()
    name_query = select(RecommendationRule.id).where(
        RecommendationRule.ads_account_id == account.id,
        RecommendationRule.name_key == name_key,
    )
    if rule_id is not None:
        name_query = name_query.where(RecommendationRule.id != rule_id)
    name_exists = (await db.execute(name_query)).scalars().first()
    if name_exists:
        raise HTTPException(status_code=409, detail=f"A rule named '{clean_name}' already exists")

    # 9. max_rules_per_account -> 422
    if rule_id is None:
        count_query = select(func.count(RecommendationRule.id)).where(
            RecommendationRule.ads_account_id == account.id
        )
        total_rules = (await db.execute(count_query)).scalar() or 0
        if total_rules >= LIMITS["max_rules_per_account"]:
            raise ServiceValidationError([{
                "field": "name",
                "message": f"Maximum of {LIMITS['max_rules_per_account']} rules reached for this account",
            }])
