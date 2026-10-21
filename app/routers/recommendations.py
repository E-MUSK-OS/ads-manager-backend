from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
import sqlalchemy as sa
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.dependencies import assert_owned_account, get_current_user
from app.models.ads_account import AdsAccount
from app.models.campaign import Campaign
from app.models.ad_group import AdGroup
from app.models.recommendation import Recommendation
from app.models.recommendation_run import RecommendationRun
from app.models.user import User
from app.recommendations.applier import apply_recommendation
from app.recommendations.engine import run_engine

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


class RunRecommendationsRequest(BaseModel):
    ads_account_id: int
    rule_ids: Optional[list[int]] = None


class ApplyRequest(BaseModel):
    overrides: Optional[dict[str, Any]] = None


class DismissRequest(BaseModel):
    reason: Optional[str] = None


class BulkRecommendationAction(BaseModel):
    ids: list[int]
    action: Literal["APPLY", "DISMISS"]


def format_recommendation(r: Recommendation) -> dict[str, Any]:
    return {
        "id": r.id,
        "ads_account_id": r.ads_account_id,
        "rule_id": r.rule_id,
        "rule_name": r.rule_name,
        "source": r.source,
        "affected_parameter": r.affected_parameter,
        "entity_type": r.entity_type,
        "entity_id": r.entity_id,
        "entity_label": r.entity_label,
        "campaign_id": r.campaign_id,
        "campaign_name": r.campaign.name if getattr(r, "campaign", None) else None,
        "ad_group_id": r.ad_group_id,
        "ad_group_name": r.ad_group.name if getattr(r, "ad_group", None) else None,
        "evidence": r.evidence,
        "suggested_actions": r.suggested_actions,
        "status": r.status,
        "dedupe_key": r.dedupe_key,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
        "resolved_by": r.resolved_by,
        "apply_result": r.apply_result,
    }


async def load_owned_recommendation(db: AsyncSession, rec_id: int, user: User) -> Recommendation:
    res = await db.execute(
        select(Recommendation)
        .join(AdsAccount, Recommendation.ads_account_id == AdsAccount.id)
        .where(Recommendation.id == rec_id, AdsAccount.user_id == user.id)
        .options(selectinload(Recommendation.campaign), selectinload(Recommendation.ad_group))
    )
    rec = res.scalars().first()
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    return rec


# STATIC PATHS DECLARED BEFORE /{id}


@router.post("/run")
async def trigger_run(
    payload: RunRecommendationsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = await assert_owned_account(db, payload.ads_account_id, current_user)
    result = await run_engine(db, account, rule_ids=payload.rule_ids, trigger="manual")
    return result


@router.get("/summary")
async def get_summary(
    ads_account_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = await assert_owned_account(db, ads_account_id, current_user)
    now = datetime.now(timezone.utc)
    thirty_days_ago = now - timedelta(days=30)

    open_cnt = (
        await db.execute(
            select(func.count(Recommendation.id)).where(
                Recommendation.ads_account_id == account.id,
                Recommendation.status == "OPEN",
            )
        )
    ).scalar() or 0

    applied_30d = (
        await db.execute(
            select(func.count(Recommendation.id)).where(
                Recommendation.ads_account_id == account.id,
                Recommendation.status == "APPLIED",
                Recommendation.resolved_at >= thirty_days_ago,
            )
        )
    ).scalar() or 0

    dismissed_30d = (
        await db.execute(
            select(func.count(Recommendation.id)).where(
                Recommendation.ads_account_id == account.id,
                Recommendation.status == "DISMISSED",
                Recommendation.resolved_at >= thirty_days_ago,
            )
        )
    ).scalar() or 0

    latest_run = (
        await db.execute(
            select(RecommendationRun)
            .where(RecommendationRun.ads_account_id == account.id)
            .order_by(RecommendationRun.started_at.desc())
            .limit(1)
        )
    ).scalars().first()

    last_run_at = None
    if latest_run:
        last_run_at = (
            latest_run.finished_at.isoformat()
            if latest_run.finished_at
            else latest_run.started_at.isoformat()
        )

    return {
        "open": open_cnt,
        "applied_30d": applied_30d,
        "dismissed_30d": dismissed_30d,
        "last_run_at": last_run_at,
    }


@router.post("/bulk")
async def bulk_recommendations_action(
    payload: BulkRecommendationAction,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    results = []
    for rid in payload.ids:
        try:
            rec = await load_owned_recommendation(db, rid, current_user)
            if payload.action == "APPLY":
                rec = await apply_recommendation(db, current_user, rec)
            elif payload.action == "DISMISS":
                if rec.status != "OPEN":
                    raise HTTPException(status_code=400, detail=f"Cannot dismiss recommendation in {rec.status} status")
                rec.status = "DISMISSED"
                rec.resolved_at = datetime.now(timezone.utc)
                rec.resolved_by = current_user.id
                await db.commit()
                await db.refresh(rec)

            results.append({
                "id": rid,
                "success": True,
                "error": None,
                "recommendation": format_recommendation(rec),
            })
        except Exception as e:
            await db.rollback()
            err_msg = getattr(e, "detail", str(e))
            if isinstance(err_msg, list) and len(err_msg) > 0 and isinstance(err_msg[0], dict):
                err_msg = err_msg[0].get("message", str(err_msg))
            results.append({
                "id": rid,
                "success": False,
                "error": str(err_msg),
            })

    return results


@router.get("")
async def list_recommendations(
    ads_account_id: int = Query(...),
    status: Optional[str] = Query(None),
    affected_parameter: Optional[str] = Query(None),
    rule_id: Optional[int] = Query(None),
    campaign_id: Optional[int] = Query(None),
    q: Optional[str] = Query(None),
    sort: str = Query("impact"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = await assert_owned_account(db, ads_account_id, current_user)

    # 1. Counts per status for this account
    counts_res = await db.execute(
        select(Recommendation.status, func.count(Recommendation.id))
        .where(Recommendation.ads_account_id == account.id)
        .group_by(Recommendation.status)
    )
    counts = {s: 0 for s in ("OPEN", "APPLIED", "DISMISSED", "EXPIRED", "FAILED")}
    for s_val, cnt in counts_res.all():
        if s_val in counts:
            counts[s_val] = cnt

    # 2. Filtered list query
    base_query = (
        select(Recommendation)
        .join(Campaign, Recommendation.campaign_id == Campaign.id)
        .where(Recommendation.ads_account_id == account.id)
    )

    if status:
        base_query = base_query.where(Recommendation.status == status)
    if affected_parameter:
        base_query = base_query.where(Recommendation.affected_parameter == affected_parameter)
    if rule_id:
        base_query = base_query.where(Recommendation.rule_id == rule_id)
    if campaign_id:
        base_query = base_query.where(Recommendation.campaign_id == campaign_id)
    if q:
        pattern = f"%{q.strip()}%"
        base_query = base_query.where(
            or_(
                Recommendation.entity_label.ilike(pattern),
                Recommendation.rule_name.ilike(pattern),
                Campaign.name.ilike(pattern),
            )
        )

    count_stmt = select(func.count()).select_from(base_query.subquery())
    total_count = (await db.execute(count_stmt)).scalar() or 0

    if sort == "impact":
        expr = sa.cast(
            Recommendation.evidence["projection"]["monthly_equivalent"].as_string(),
            sa.Float,
        )
        base_query = base_query.order_by(expr.desc().nullslast(), Recommendation.created_at.desc())
    else:  # newest
        base_query = base_query.order_by(Recommendation.created_at.desc())

    items_stmt = (
        base_query
        .options(selectinload(Recommendation.campaign), selectinload(Recommendation.ad_group))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    recs = (await db.execute(items_stmt)).scalars().all()

    return {
        "items": [format_recommendation(r) for r in recs],
        "total_count": total_count,
        "counts": counts,
    }


# DYNAMIC PATHS /{id}


@router.get("/{id}")
async def get_recommendation(
    id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rec = await load_owned_recommendation(db, id, current_user)
    return format_recommendation(rec)


@router.post("/{id}/apply")
async def apply_single_recommendation(
    id: int,
    payload: ApplyRequest = Body(default=ApplyRequest()),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rec = await load_owned_recommendation(db, id, current_user)
    updated = await apply_recommendation(db, current_user, rec, payload.overrides)
    return format_recommendation(updated)


@router.post("/{id}/dismiss")
async def dismiss_single_recommendation(
    id: int,
    payload: DismissRequest = Body(default=DismissRequest()),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rec = await load_owned_recommendation(db, id, current_user)
    if rec.status != "OPEN":
        raise HTTPException(status_code=400, detail=f"Cannot dismiss recommendation in {rec.status} status")

    rec.status = "DISMISSED"
    rec.resolved_at = datetime.now(timezone.utc)
    rec.resolved_by = current_user.id
    if payload.reason:
        rec.apply_result = {"reason": payload.reason}

    await db.commit()
    await db.refresh(rec)
    return format_recommendation(rec)


@router.post("/{id}/reopen")
async def reopen_single_recommendation(
    id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rec = await load_owned_recommendation(db, id, current_user)
    if rec.status != "DISMISSED":
        raise HTTPException(status_code=400, detail="Only dismissed recommendations can be reopened")

    rec.status = "OPEN"
    rec.resolved_at = None
    rec.resolved_by = None
    rec.apply_result = None

    await db.commit()
    await db.refresh(rec)
    return format_recommendation(rec)
