from datetime import datetime, timezone, date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Any
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update, delete

from app.database import get_db
from app.dependencies import get_current_user, assert_owned_account
from app.models.user import User
from app.models.ads_account import AdsAccount
from app.models.campaign import Campaign
from app.models.recommendation_rule import RecommendationRule, RecommendationRuleCampaign
from app.models.recommendation import Recommendation
from app.models.recommendation_run import RecommendationRun
from app.models.metric_daily import MetricDaily
from app.recommendations.conflicts import check_rule_contradictions, check_rule_overlaps
from app.config import settings
from app.recommendations.registry import (
    DATE_FILTERS,
    METRICS,
    OPERATORS,
    AFFECTED_PARAMETERS,
    ACTIONS,
    TEMPLATES,
    LIMITS,
    resolve_template,
    get_visible_affected_parameters,
    get_visible_actions,
    get_visible_templates,
)
from app.recommendations.schemas import (
    RuleIn,
    RuleResponse,
    CreateRuleResponse,
    ActiveUpdate,
    ScopeUpdate,
    BulkRulesRequest,
    PreviewRequest,
    validate_rule_in,
)
from app.core.marketplace_rules import marketplace_or_in, today_for
from app.core.exceptions import ServiceValidationError

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


def format_rule_dict(rule: RecommendationRule, campaign_ids: list[int], open_count: int) -> dict:
    if rule.affected_parameter == "PLACEMENT" and not getattr(settings, "ENABLE_PLACEMENT_RULES", False):
        scope_label = "disabled (placements are read-only)"
    elif rule.scope_mode == "ALL":
        scope_label = "All Campaigns"
    elif rule.scope_mode == "INCLUDE":
        scope_label = f"Include {len(campaign_ids)}"
    else:
        scope_label = f"Exclude {len(campaign_ids)}"

    return {
        "id": rule.id,
        "ads_account_id": rule.ads_account_id,
        "name": rule.name,
        "affected_parameter": rule.affected_parameter,
        "date_filter": rule.date_filter,
        "min_clicks": rule.min_clicks,
        "conditions": rule.conditions,
        "actions": rule.actions,
        "scope_mode": rule.scope_mode,
        "campaign_ids": campaign_ids,
        "is_active": rule.is_active,
        "template_key": rule.template_key,
        "created_at": rule.created_at,
        "updated_at": rule.updated_at,
        "last_run_at": rule.last_run_at,
        "last_match_count": rule.last_match_count,
        "last_funnel": getattr(rule, "last_funnel", None),
        "open_count": open_count,
        "scope_label": scope_label,
    }


async def load_owned_rule(
    db: AsyncSession, rule_id: int, user: User
) -> tuple[RecommendationRule, AdsAccount]:
    res = await db.execute(
        select(RecommendationRule, AdsAccount)
        .join(AdsAccount, RecommendationRule.ads_account_id == AdsAccount.id)
        .where(RecommendationRule.id == rule_id, AdsAccount.user_id == user.id)
    )
    row = res.first()
    if not row:
        raise HTTPException(status_code=404, detail="Rule not found")
    return row[0], row[1]


# ---------------------------------------------------------------------------
# Static paths declared BEFORE /{id} routes
# ---------------------------------------------------------------------------


@router.get("/meta")
async def get_meta(
    ads_account_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = await assert_owned_account(db, ads_account_id, current_user)
    m_rules = marketplace_or_in(account.marketplace)

    return {
        "date_filters": DATE_FILTERS,
        "metrics": {k: {"label": v[0], "unit": v[1]} for k, v in METRICS.items()},
        "operators": {k: {"label": v[0], "symbol": v[1]} for k, v in OPERATORS.items()},
        "affected_parameters": {
            k: {"label": v, "available": True}
            for k, v in get_visible_affected_parameters().items()
        },
        "actions": get_visible_actions(),
        "templates": [
            {
                **resolve_template(t, account.marketplace),
                "available": True,
            }
            for t in get_visible_templates()
        ],
        "limits": LIMITS,
        "currency": {
            "symbol": m_rules["symbol"],
            "locale": m_rules["locale"],
            "code": m_rules["currency"],
        },
    }


@router.get("/rules", response_model=list[RuleResponse])
async def list_rules(
    ads_account_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = await assert_owned_account(db, ads_account_id, current_user)

    rules_res = await db.execute(
        select(RecommendationRule)
        .where(RecommendationRule.ads_account_id == account.id)
        .order_by(RecommendationRule.created_at.desc())
    )
    rules = rules_res.scalars().all()

    # Bulk load campaign ids
    camps_res = await db.execute(
        select(RecommendationRuleCampaign.rule_id, RecommendationRuleCampaign.campaign_id)
        .join(RecommendationRule, RecommendationRuleCampaign.rule_id == RecommendationRule.id)
        .where(RecommendationRule.ads_account_id == account.id)
    )
    camps_map: dict[int, list[int]] = {}
    for rid, cid in camps_res.all():
        camps_map.setdefault(rid, []).append(cid)

    # Bulk load open counts
    open_res = await db.execute(
        select(Recommendation.rule_id, func.count(Recommendation.id))
        .where(Recommendation.ads_account_id == account.id, Recommendation.status == "OPEN")
        .group_by(Recommendation.rule_id)
    )
    open_map: dict[int, int] = dict(open_res.all())

    return [
        format_rule_dict(r, camps_map.get(r.id, []), open_map.get(r.id, 0))
        for r in rules
    ]


@router.post("/rules", response_model=CreateRuleResponse)
async def create_rule(
    payload: RuleIn,
    ads_account_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_account_id = payload.ads_account_id or ads_account_id
    if not target_account_id:
        raise HTTPException(status_code=422, detail="ads_account_id is required")

    account = await assert_owned_account(db, target_account_id, current_user)
    await validate_rule_in(db, account, payload, rule_id=None)

    clean_name = payload.name.strip()
    rule = RecommendationRule(
        ads_account_id=account.id,
        name=clean_name,
        name_key=clean_name.lower(),
        affected_parameter=payload.affected_parameter,
        date_filter=payload.date_filter,
        min_clicks=payload.min_clicks,
        conditions=[c.model_dump() for c in payload.conditions],
        actions=[a.model_dump() for a in payload.actions],
        scope_mode=payload.scope_mode,
        is_active=payload.is_active,
        template_key=payload.template_key,
    )
    db.add(rule)
    await db.flush()

    if payload.scope_mode in ("INCLUDE", "EXCLUDE") and payload.campaign_ids:
        for cid in set(payload.campaign_ids):
            db.add(RecommendationRuleCampaign(rule_id=rule.id, campaign_id=cid))

    run_dict = None
    if payload.is_active:
        from app.recommendations.engine import run_engine
        run_summary = await run_engine(db, account, rule_ids=[rule.id], trigger="rule_saved")
        run_dict = {
            "id": run_summary.get("run_id"),
            "trigger": "rule_saved",
            "rules_evaluated": 1,
            "created": run_summary.get("created", 0),
            "expired": run_summary.get("expired", 0),
            "skipped": run_summary.get("skipped", 0),
            "status": "SUCCESS",
        }
    else:
        run_dict = {
            "id": None,
            "trigger": "rule_saved",
            "rules_evaluated": 0,
            "created": 0,
            "expired": 0,
            "skipped": 0,
            "status": "SUCCESS",
        }

    await db.refresh(rule)

    rule_dict = format_rule_dict(
        rule,
        payload.campaign_ids if payload.scope_mode in ("INCLUDE", "EXCLUDE") else [],
        0,
    )
    return {"rule": rule_dict, "run": run_dict}


@router.post("/rules/bulk")
async def bulk_rules_action(
    payload: BulkRulesRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    results = []
    for rid in payload.ids:
        row = (
            await db.execute(
                select(RecommendationRule, AdsAccount)
                .join(AdsAccount, RecommendationRule.ads_account_id == AdsAccount.id)
                .where(RecommendationRule.id == rid, AdsAccount.user_id == current_user.id)
            )
        ).first()

        if not row:
            results.append({"id": rid, "success": False, "error": "Rule not found"})
            continue

        rule, _ = row
        if payload.action == "ENABLE":
            rule.is_active = True
            results.append({"id": rid, "success": True, "action": "ENABLE"})
        elif payload.action == "DISABLE":
            rule.is_active = False
            results.append({"id": rid, "success": True, "action": "DISABLE"})
        elif payload.action == "DELETE":
            await db.execute(
                update(Recommendation)
                .where(Recommendation.rule_id == rule.id, Recommendation.status == "OPEN")
                .values(status="EXPIRED", rule_id=None)
            )
            await db.execute(
                update(Recommendation)
                .where(Recommendation.rule_id == rule.id)
                .values(rule_id=None)
            )
            await db.delete(rule)
            results.append({"id": rid, "success": True, "action": "DELETE"})

    await db.commit()
    return {"results": results}


@router.post("/rules/preview")
async def preview_rule(
    payload: PreviewRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = await assert_owned_account(db, payload.ads_account_id, current_user)
    await validate_rule_in(db, account, payload.rule, rule_id=None)
    from app.recommendations.engine import get_engine
    engine = get_engine()
    today = today_for(account.marketplace)
    candidates, funnels = await engine.evaluate(db, account, [payload.rule], today=today, dry_run=True)
    rule_key = getattr(payload.rule, "id", "draft") or "draft"
    funnel = funnels.get(rule_key) or (list(funnels.values())[0] if funnels else None)

    # 1. Contradictions within conditions (Q14)
    conditions = [c.model_dump() if hasattr(c, "model_dump") else dict(c) for c in payload.rule.conditions]
    contradiction_warnings = check_rule_contradictions(conditions)

    # 2. Overlaps with other active rules for this account (Q14)
    active_rules_res = await db.execute(
        select(RecommendationRule).where(
            RecommendationRule.ads_account_id == account.id,
            RecommendationRule.is_active == True,
        )
    )
    active_rules = active_rules_res.scalars().all()
    active_rule_ids = [r.id for r in active_rules]
    camps_map: dict[int, list[int]] = {}
    if active_rule_ids:
        c_res = await db.execute(
            select(RecommendationRuleCampaign.rule_id, RecommendationRuleCampaign.campaign_id)
            .where(RecommendationRuleCampaign.rule_id.in_(active_rule_ids))
        )
        for r_id, c_id in c_res.all():
            camps_map.setdefault(r_id, []).append(c_id)

    rules_with_camps = []
    for r in active_rules:
        setattr(r, "campaign_ids", camps_map.get(r.id, []))
        rules_with_camps.append(r)

    draft_camps = getattr(payload.rule, "campaign_ids", []) or []
    overlap_warnings = check_rule_overlaps(
        active_rules=rules_with_camps,
        draft_param=payload.rule.affected_parameter,
        draft_scope_mode=payload.rule.scope_mode,
        draft_campaign_ids=draft_camps,
        exclude_rule_id=getattr(payload.rule, "id", None) if isinstance(getattr(payload.rule, "id", None), int) else None,
    )
    warnings = contradiction_warnings + overlap_warnings

    # 3. Backtest in preview (Q16)
    backtest: Optional[dict[str, Any]] = None
    backtest_date = today - timedelta(days=7)

    earliest_date = None
    camp_ids_stmt = select(Campaign.id).where(Campaign.ads_account_id == account.id)
    acc_camp_ids = list((await db.execute(camp_ids_stmt)).scalars().all())
    if acc_camp_ids:
        earliest_stmt = select(func.min(MetricDaily.date)).where(
            MetricDaily.entity_type == "campaign",
            MetricDaily.entity_id.in_(acc_camp_ids),
        )
        earliest_date_raw = (await db.execute(earliest_stmt)).scalar()
        earliest_date = date.fromisoformat(earliest_date_raw) if isinstance(earliest_date_raw, str) else earliest_date_raw

    # Only show backtest if history is >= 14 days old (hidden if history is short per Q16)
    if earliest_date and earliest_date <= (today - timedelta(days=14)):
        back_candidates, _ = await engine.evaluate(
            db, account, [payload.rule], today=backtest_date, dry_run=True
        )
        matched_count = len(back_candidates)
        next_7d_spend = Decimal("0.00")
        next_7d_orders = 0
        next_7d_sales = Decimal("0.00")

        if matched_count > 0:
            eval_start = backtest_date + timedelta(days=1)
            eval_end = today
            kw_ids = [c.entity_id for c in back_candidates if c.entity_type in ("keyword", "target", "search_term") and c.entity_id]
            camp_ids = [c.campaign_id for c in back_candidates if c.entity_type == "campaign" and c.campaign_id]

            if kw_ids:
                m_stmt = select(
                    func.sum(MetricDaily.spend).label("spend"),
                    func.sum(MetricDaily.orders).label("orders"),
                    func.sum(MetricDaily.sales).label("sales"),
                ).where(
                    MetricDaily.entity_type == "keyword",
                    MetricDaily.entity_id.in_(list(set(kw_ids))),
                    MetricDaily.date >= eval_start,
                    MetricDaily.date <= eval_end,
                )
                m_row = (await db.execute(m_stmt)).first()
                if m_row:
                    next_7d_spend += Decimal(str(m_row.spend or 0))
                    next_7d_orders += int(m_row.orders or 0)
                    next_7d_sales += Decimal(str(m_row.sales or 0))

            if camp_ids:
                c_stmt = select(
                    func.sum(MetricDaily.spend).label("spend"),
                    func.sum(MetricDaily.orders).label("orders"),
                    func.sum(MetricDaily.sales).label("sales"),
                ).where(
                    MetricDaily.entity_type == "campaign",
                    MetricDaily.entity_id.in_(list(set(camp_ids))),
                    MetricDaily.date >= eval_start,
                    MetricDaily.date <= eval_end,
                )
                c_row = (await db.execute(c_stmt)).first()
                if c_row:
                    next_7d_spend += Decimal(str(c_row.spend or 0))
                    next_7d_orders += int(c_row.orders or 0)
                    next_7d_sales += Decimal(str(c_row.sales or 0))

        backtest = {
            "eligible": True,
            "matched_count": matched_count,
            "next_7d_spend": float(next_7d_spend.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "next_7d_orders": next_7d_orders,
            "next_7d_sales": float(next_7d_sales.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        }

    return {
        "match_count": len(candidates),
        "evaluated_entities": getattr(engine, "last_evaluated_count", 0),
        "sample": [c.to_dict() for c in candidates[:5]],
        "funnel": funnel,
        "warnings": warnings,
        "backtest": backtest,
    }


# ---------------------------------------------------------------------------
# Individual rule endpoints /{id}
# ---------------------------------------------------------------------------


@router.get("/rules/{id}", response_model=RuleResponse)
async def get_rule(
    id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rule, account = await load_owned_rule(db, id, current_user)

    camps_res = await db.execute(
        select(RecommendationRuleCampaign.campaign_id).where(
            RecommendationRuleCampaign.rule_id == rule.id
        )
    )
    campaign_ids = list(camps_res.scalars().all())

    open_res = await db.execute(
        select(func.count(Recommendation.id)).where(
            Recommendation.rule_id == rule.id, Recommendation.status == "OPEN"
        )
    )
    open_count = open_res.scalar() or 0

    return format_rule_dict(rule, campaign_ids, open_count)


@router.put("/rules/{id}", response_model=RuleResponse)
async def update_rule(
    id: int,
    payload: RuleIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rule, account = await load_owned_rule(db, id, current_user)
    await validate_rule_in(db, account, payload, rule_id=rule.id)

    clean_name = payload.name.strip()
    rule.name = clean_name
    rule.name_key = clean_name.lower()
    rule.affected_parameter = payload.affected_parameter
    rule.date_filter = payload.date_filter
    rule.min_clicks = payload.min_clicks
    rule.conditions = [c.model_dump() for c in payload.conditions]
    rule.actions = [a.model_dump() for a in payload.actions]
    rule.scope_mode = payload.scope_mode
    rule.is_active = payload.is_active

    await db.execute(
        delete(RecommendationRuleCampaign).where(
            RecommendationRuleCampaign.rule_id == rule.id
        )
    )
    if payload.scope_mode in ("INCLUDE", "EXCLUDE") and payload.campaign_ids:
        for cid in set(payload.campaign_ids):
            db.add(RecommendationRuleCampaign(rule_id=rule.id, campaign_id=cid))

    await db.commit()
    await db.refresh(rule)

    if rule.is_active:
        from app.recommendations.engine import run_engine
        await run_engine(db, account, rule_ids=[rule.id], trigger="rule_saved")
        await db.refresh(rule)

    camps_res = await db.execute(
        select(RecommendationRuleCampaign.campaign_id).where(
            RecommendationRuleCampaign.rule_id == rule.id
        )
    )
    campaign_ids = list(camps_res.scalars().all())

    open_res = await db.execute(
        select(func.count(Recommendation.id)).where(
            Recommendation.rule_id == rule.id, Recommendation.status == "OPEN"
        )
    )
    open_count = open_res.scalar() or 0

    return format_rule_dict(rule, campaign_ids, open_count)


@router.delete("/rules/{id}")
async def delete_rule(
    id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rule, _ = await load_owned_rule(db, id, current_user)

    # OPEN recommendations become EXPIRED (kept as history, rule_id -> NULL)
    await db.execute(
        update(Recommendation)
        .where(Recommendation.rule_id == rule.id, Recommendation.status == "OPEN")
        .values(status="EXPIRED", rule_id=None)
    )
    # Remaining recommendations set rule_id -> NULL
    await db.execute(
        update(Recommendation)
        .where(Recommendation.rule_id == rule.id)
        .values(rule_id=None)
    )

    await db.delete(rule)
    await db.commit()
    return {"status": "deleted", "id": id}


@router.patch("/rules/{id}/active", response_model=RuleResponse)
async def set_rule_active(
    id: int,
    payload: ActiveUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rule, account = await load_owned_rule(db, id, current_user)
    rule.is_active = payload.is_active
    await db.commit()
    await db.refresh(rule)

    if payload.is_active:
        from app.recommendations.engine import run_engine
        await run_engine(db, account, rule_ids=[rule.id], trigger="rule_saved")
        await db.refresh(rule)

    camps_res = await db.execute(
        select(RecommendationRuleCampaign.campaign_id).where(
            RecommendationRuleCampaign.rule_id == rule.id
        )
    )
    campaign_ids = list(camps_res.scalars().all())

    open_res = await db.execute(
        select(func.count(Recommendation.id)).where(
            Recommendation.rule_id == rule.id, Recommendation.status == "OPEN"
        )
    )
    open_count = open_res.scalar() or 0

    return format_rule_dict(rule, campaign_ids, open_count)


@router.put("/rules/{id}/scope", response_model=RuleResponse)
async def update_rule_scope(
    id: int,
    payload: ScopeUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rule, account = await load_owned_rule(db, id, current_user)

    if payload.scope_mode in ("INCLUDE", "EXCLUDE"):
        if not payload.campaign_ids:
            raise ServiceValidationError([{
                "field": "campaign_ids",
                "message": f"Select at least one campaign for scope mode {payload.scope_mode}",
            }])
        c_res = await db.execute(
            select(Campaign.id).where(
                Campaign.ads_account_id == account.id,
                Campaign.id.in_(payload.campaign_ids),
            )
        )
        owned_ids = set(c_res.scalars().all())
        for cid in payload.campaign_ids:
            if cid not in owned_ids:
                raise ServiceValidationError([{
                    "field": "campaign_ids",
                    "message": f"Campaign {cid} does not belong to this account",
                }])

    rule.scope_mode = payload.scope_mode
    await db.execute(
        delete(RecommendationRuleCampaign).where(
            RecommendationRuleCampaign.rule_id == rule.id
        )
    )
    if payload.scope_mode in ("INCLUDE", "EXCLUDE") and payload.campaign_ids:
        for cid in set(payload.campaign_ids):
            db.add(RecommendationRuleCampaign(rule_id=rule.id, campaign_id=cid))

    await db.commit()
    await db.refresh(rule)

    camps_res = await db.execute(
        select(RecommendationRuleCampaign.campaign_id).where(
            RecommendationRuleCampaign.rule_id == rule.id
        )
    )
    campaign_ids = list(camps_res.scalars().all())

    open_res = await db.execute(
        select(func.count(Recommendation.id)).where(
            Recommendation.rule_id == rule.id, Recommendation.status == "OPEN"
        )
    )
    open_count = open_res.scalar() or 0

    return format_rule_dict(rule, campaign_ids, open_count)


@router.post("/rules/{id}/duplicate", response_model=RuleResponse)
@router.post("/rules/{id}/clone", response_model=RuleResponse)
async def duplicate_rule(
    id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rule, account = await load_owned_rule(db, id, current_user)

    cnt = (
        await db.execute(
            select(func.count(RecommendationRule.id)).where(
                RecommendationRule.ads_account_id == account.id
            )
        )
    ).scalar() or 0
    if cnt >= LIMITS["max_rules_per_account"]:
        raise ServiceValidationError([{
            "field": "name",
            "message": f"Maximum of {LIMITS['max_rules_per_account']} rules reached for this account",
        }])

    base_name = f"{rule.name} (copy)"[:60]
    target_name = base_name
    counter = 2
    while True:
        exists = (
            await db.execute(
                select(RecommendationRule.id).where(
                    RecommendationRule.ads_account_id == account.id,
                    RecommendationRule.name_key == target_name.strip().lower(),
                )
            )
        ).scalars().first()
        if not exists:
            break
        suffix = f" (copy {counter})"
        target_name = f"{rule.name[:60 - len(suffix)]}{suffix}"
        counter += 1

    new_rule = RecommendationRule(
        ads_account_id=account.id,
        name=target_name,
        name_key=target_name.strip().lower(),
        affected_parameter=rule.affected_parameter,
        date_filter=rule.date_filter,
        min_clicks=rule.min_clicks,
        conditions=rule.conditions,
        actions=rule.actions,
        scope_mode=rule.scope_mode,
        is_active=False,
        template_key=rule.template_key,
    )
    db.add(new_rule)
    await db.flush()

    camps_res = await db.execute(
        select(RecommendationRuleCampaign.campaign_id).where(
            RecommendationRuleCampaign.rule_id == rule.id
        )
    )
    c_ids = list(camps_res.scalars().all())
    for cid in c_ids:
        db.add(RecommendationRuleCampaign(rule_id=new_rule.id, campaign_id=cid))

    await db.commit()
    await db.refresh(new_rule)

    return format_rule_dict(new_rule, c_ids, 0)


@router.post("/rules/{id}/run-now")
async def run_rule_now(
    id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Evaluates and persists recommendations exclusively for this rule (Q17)."""
    rule, account = await load_owned_rule(db, id, current_user)
    from app.recommendations.engine import run_engine
    result = await run_engine(db, account, rule_ids=[rule.id], trigger="rule_run_now")
    await db.commit()
    await db.refresh(rule)
    return {
        "rule_id": rule.id,
        "rule_name": rule.name,
        "created": result.get("created", 0),
        "expired": result.get("expired", 0),
        "skipped": result.get("skipped", 0),
        "funnel": rule.last_funnel,
        "last_match_count": rule.last_match_count,
        "last_run_at": rule.last_run_at.isoformat() if rule.last_run_at else None,
        "notice": result.get("notice"),
    }
