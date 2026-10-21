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
from app.core.marketplace_rules import marketplace_or_us
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
    actions: list[ActionIn] = Field(min_length=1, max_length=4)
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


class ExportRulesRequest(BaseModel):
    ids: list[int]


class ImportRulesRequest(BaseModel):
    ads_account_id: int
    rules: list[dict[str, Any]]


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
    marketplace_rules = marketplace_or_us(account.marketplace)

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

        # 2. value numeric >= 0 (percent/ratio metrics <= 1000; no duplicate metric+op pair)
        if cond.value < 0:
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

    # 3. action allowed for the affected parameter
    param_actions = ACTIONS.get(rule.affected_parameter, {})
    for idx, action in enumerate(rule.actions):
        if action.type not in param_actions:
            errors.append({
                "field": f"actions.{idx}.type",
                "message": f"Action '{action.type}' is not allowed for affected parameter '{rule.affected_parameter}'",
            })
            continue

        # 4. each param present/in range (use the registry; fill defaults)
        action_def = param_actions[action.type]
        params_schema = action_def.get("params", {})
        for p_name, p_schema in params_schema.items():
            val = action.params.get(p_name)
            if val is None or val == "":
                if "default" in p_schema:
                    action.params[p_name] = p_schema["default"]
                elif not p_schema.get("optional", False):
                    errors.append({
                        "field": f"actions.{idx}.params.{p_name}",
                        "message": f"Parameter '{p_name}' is required",
                    })
            else:
                p_type = p_schema["type"]
                if p_type == "enum":
                    if val not in p_schema["options"]:
                        errors.append({
                            "field": f"actions.{idx}.params.{p_name}",
                            "message": f"Value must be one of {p_schema['options']}",
                        })
                elif p_type in ("percent", "int"):
                    try:
                        val_num = int(val) if p_type == "int" else float(val)
                        if val_num < p_schema["min"] or val_num > p_schema["max"]:
                            errors.append({
                                "field": f"actions.{idx}.params.{p_name}",
                                "message": f"Value must be between {p_schema['min']} and {p_schema['max']}",
                            })
                        action.params[p_name] = val_num
                    except (ValueError, TypeError):
                        errors.append({
                            "field": f"actions.{idx}.params.{p_name}",
                            "message": "Must be a valid number",
                        })
                elif p_type == "money":
                    try:
                        val_num = float(val)
                        max_bid = marketplace_rules["max_bid"]
                        if val_num < 0:
                            errors.append({
                                "field": f"actions.{idx}.params.{p_name}",
                                "message": "Bid must be greater than or equal to 0",
                            })
                        elif val_num > max_bid:
                            errors.append({
                                "field": f"actions.{idx}.params.{p_name}",
                                "message": f"Bid must be less than or equal to {max_bid}",
                            })
                        action.params[p_name] = val_num
                    except (ValueError, TypeError):
                        errors.append({
                            "field": f"actions.{idx}.params.{p_name}",
                            "message": "Must be a valid money value",
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
