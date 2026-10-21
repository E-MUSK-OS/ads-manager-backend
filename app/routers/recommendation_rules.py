from datetime import datetime, timezone
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
from app.recommendations.registry import (
    DATE_FILTERS,
    METRICS,
    OPERATORS,
    AFFECTED_PARAMETERS,
    ACTIONS,
    TEMPLATES,
    LIMITS,
    resolve_template,
)
from app.recommendations.schemas import (
    RuleIn,
    RuleResponse,
    CreateRuleResponse,
    ActiveUpdate,
    ScopeUpdate,
    BulkRulesRequest,
    ExportRulesRequest,
    ImportRulesRequest,
    PreviewRequest,
    validate_rule_in,
)
from app.core.marketplace_rules import marketplace_or_us, today_for
from app.core.exceptions import ServiceValidationError

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


def format_rule_dict(rule: RecommendationRule, campaign_ids: list[int], open_count: int) -> dict:
    if rule.scope_mode == "ALL":
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
    m_rules = marketplace_or_us(account.marketplace)

    return {
        "date_filters": DATE_FILTERS,
        "metrics": {k: {"label": v[0], "unit": v[1]} for k, v in METRICS.items()},
        "operators": {k: {"label": v[0], "symbol": v[1]} for k, v in OPERATORS.items()},
        "affected_parameters": {
            k: {"label": v, "available": True}
            for k, v in AFFECTED_PARAMETERS.items()
        },
        "actions": ACTIONS,
        "templates": [
            {
                **resolve_template(t, account.marketplace),
                "available": True,
            }
            for t in TEMPLATES
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


@router.post("/rules/export")
async def export_rules(
    payload: ExportRulesRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rules_res = await db.execute(
        select(RecommendationRule)
        .join(AdsAccount, RecommendationRule.ads_account_id == AdsAccount.id)
        .where(
            RecommendationRule.id.in_(payload.ids),
            AdsAccount.user_id == current_user.id,
        )
    )
    rules = rules_res.scalars().all()

    return {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "rules": [
            {
                "name": r.name,
                "affected_parameter": r.affected_parameter,
                "date_filter": r.date_filter,
                "min_clicks": r.min_clicks,
                "conditions": r.conditions,
                "actions": r.actions,
                "scope_mode": r.scope_mode,
            }
            for r in rules
        ],
    }


@router.post("/rules/import")
async def import_rules(
    payload: ImportRulesRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = await assert_owned_account(db, payload.ads_account_id, current_user)
    results = []

    for item in payload.rules:
        name = str(item.get("name", "")).strip()
        if not name:
            results.append({"name": name, "status": "invalid", "reason": "Rule name is required"})
            continue

        # Check name clash in this account
        clash = (
            await db.execute(
                select(RecommendationRule.id).where(
                    RecommendationRule.ads_account_id == account.id,
                    RecommendationRule.name_key == name.lower(),
                )
            )
        ).scalars().first()
        if clash:
            results.append({
                "name": name,
                "status": "skipped",
                "reason": f"A rule named '{name}' already exists",
            })
            continue

        try:
            rule_in = RuleIn(
                name=name,
                affected_parameter=item.get("affected_parameter"),
                date_filter=item.get("date_filter", "LAST_30_DAYS"),
                min_clicks=item.get("min_clicks", 0),
                conditions=item.get("conditions", []),
                actions=item.get("actions", []),
                scope_mode="ALL",
                is_active=False,
                campaign_ids=[],
            )
        except Exception as e:
            results.append({"name": name, "status": "invalid", "reason": str(e)})
            continue

        try:
            await validate_rule_in(db, account, rule_in, rule_id=None)
        except ServiceValidationError as e:
            results.append({
                "name": name,
                "status": "invalid",
                "reason": ", ".join(err["message"] for err in e.errors),
            })
            continue
        except HTTPException as e:
            results.append({"name": name, "status": "invalid", "reason": e.detail})
            continue

        new_rule = RecommendationRule(
            ads_account_id=account.id,
            name=name,
            name_key=name.lower(),
            affected_parameter=rule_in.affected_parameter,
            date_filter=rule_in.date_filter,
            min_clicks=rule_in.min_clicks,
            conditions=[c.model_dump() for c in rule_in.conditions],
            actions=[a.model_dump() for a in rule_in.actions],
            scope_mode="ALL",
            is_active=False,
        )
        db.add(new_rule)
        await db.flush()
        results.append({"name": name, "status": "created", "rule_id": new_rule.id})

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
    candidates = await engine.evaluate(db, account, [payload.rule], today=today, dry_run=True)
    return {
        "match_count": len(candidates),
        "evaluated_entities": getattr(engine, "last_evaluated_count", 0),
        "sample": [c.to_dict() for c in candidates[:5]],
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
