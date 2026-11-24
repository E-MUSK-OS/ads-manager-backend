from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta, date
from decimal import Decimal, ROUND_HALF_UP
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
from app.models.keyword import Keyword
from app.models.negative_keyword import NegativeKeyword
from app.models.campaign_history import CampaignHistory
from app.models.metric_daily import MetricDaily
from app.models.recommendation import Recommendation
from app.models.recommendation_run import RecommendationRun
from app.models.user import User
from app.providers.ads.amazon_provider import AmazonAdsProvider
from app.recommendations.applier import apply_recommendation, apply_batch_recommendations
from app.recommendations.conflicts import are_conflicting_recommendations, detect_conflicts
from app.recommendations.engine import run_engine
from app.core.marketplace_rules import today_for

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


class RunRecommendationsRequest(BaseModel):
    ads_account_id: int
    rule_ids: Optional[list[int]] = None


class ApplyRequest(BaseModel):
    overrides: Optional[dict[str, Any]] = None


class DismissRequest(BaseModel):
    reason: Optional[str] = None


class SnoozeRequest(BaseModel):
    days: Literal[7, 14, 30] = 7


class BulkRecommendationAction(BaseModel):
    ids: list[int] = Field(..., max_length=100)
    action: Literal["APPLY", "DISMISS"]
    confirm: Optional[bool] = None
    idempotency_key: Optional[str] = Field(default=None, max_length=128)


_apply_idempotency_cache: dict[str, list[dict[str, Any]]] = {}
_account_apply_in_flight: set[int] = set()


def format_recommendation(
    r: Recommendation,
    conflicts_with: Optional[list[int]] = None,
) -> dict[str, Any]:
    apply_res = r.apply_result or {}
    evidence_dict = r.evidence or {}
    display_status = r.status
    if apply_res.get("undone_at"):
        display_status = "UNDONE"
    elif evidence_dict.get("snoozed_until"):
        display_status = "SNOOZED"

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
        "display_status": display_status,
        "snoozed_until": evidence_dict.get("snoozed_until"),
        "dedupe_key": r.dedupe_key,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
        "resolved_by": r.resolved_by,
        "apply_result": r.apply_result,
        "conflicts_with": conflicts_with or [],
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
    if payload.action == "APPLY":
        if len(payload.ids) > 100:
            raise HTTPException(status_code=422, detail="Maximum 100 items per apply batch")
        if payload.confirm is not True:
            raise HTTPException(status_code=422, detail="Explicit confirmation is required (confirm: true)")
        if not payload.idempotency_key or not payload.idempotency_key.strip():
            raise HTTPException(status_code=422, detail="idempotency_key is required for apply batch")

        cache_key = f"{current_user.id}:{payload.idempotency_key.strip()}"
        if cache_key in _apply_idempotency_cache:
            return _apply_idempotency_cache[cache_key]

        if not payload.ids:
            return []

        recs_to_apply = []
        account_ids: set[int] = set()
        for rid in payload.ids:
            rec = await load_owned_recommendation(db, rid, current_user)
            recs_to_apply.append(rec)
            account_ids.add(rec.ads_account_id)

        # Per-account concurrency lock
        for acc_id in account_ids:
            if acc_id in _account_apply_in_flight:
                raise HTTPException(status_code=409, detail="An Apply batch is already running for this account")

        for acc_id in account_ids:
            _account_apply_in_flight.add(acc_id)

        try:
            batch_results = await apply_batch_recommendations(db, current_user, recs_to_apply)
            results = []
            for br in batch_results:
                err_str = str(br.get("error") or "")
                if br["success"]:
                    item_status = "APPLIED"
                elif "stale" in err_str.lower() or "changed" in err_str.lower():
                    item_status = "STALE"
                else:
                    item_status = "FAILED"
                results.append({
                    "id": br["id"],
                    "status": item_status,
                    "success": br["success"],
                    "error": br["error"],
                    "changes": br.get("changes", []),
                    "recommendation": format_recommendation(br["recommendation"]) if br.get("recommendation") else None,
                })
            _apply_idempotency_cache[cache_key] = results
            return results
        finally:
            for acc_id in account_ids:
                _account_apply_in_flight.discard(acc_id)

    results = []
    for rid in payload.ids:
        try:
            rec = await load_owned_recommendation(db, rid, current_user)
            if payload.action == "DISMISS":
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

    # Default ranking by spend at stake (window spend) per Q20; support sortable columns
    if sort in ("spend", "impact"):
        expr = sa.cast(
            Recommendation.evidence["metrics"]["spend"].as_string(),
            sa.Float,
        )
        base_query = base_query.order_by(expr.desc().nullslast(), Recommendation.created_at.desc())
    elif sort in ("purchases", "orders"):
        expr = sa.cast(
            Recommendation.evidence["metrics"]["orders"].as_string(),
            sa.Float,
        )
        base_query = base_query.order_by(expr.desc().nullslast(), Recommendation.created_at.desc())
    elif sort == "acos":
        expr = sa.cast(
            Recommendation.evidence["metrics"]["acos"].as_string(),
            sa.Float,
        )
        base_query = base_query.order_by(expr.desc().nullslast(), Recommendation.created_at.desc())
    elif sort == "newest":
        base_query = base_query.order_by(Recommendation.created_at.desc())
    else:
        expr = sa.cast(
            Recommendation.evidence["metrics"]["spend"].as_string(),
            sa.Float,
        )
        base_query = base_query.order_by(expr.desc().nullslast(), Recommendation.created_at.desc())

    items_stmt = (
        base_query
        .options(selectinload(Recommendation.campaign), selectinload(Recommendation.ad_group))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    recs = (await db.execute(items_stmt)).scalars().all()

    # Calculate conflicts among OPEN recommendations for this account (R3.5, Q5)
    open_recs_res = await db.execute(
        select(Recommendation).where(
            Recommendation.ads_account_id == account.id,
            Recommendation.status == "OPEN",
        )
    )
    all_open_recs = open_recs_res.scalars().all()
    conflicts_map = detect_conflicts(all_open_recs)

    return {
        "items": [format_recommendation(r, conflicts_map.get(r.id, [])) for r in recs],
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


@router.post("/{id}/snooze")
async def snooze_single_recommendation(
    id: int,
    payload: SnoozeRequest = Body(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Snoozes recommendation for 7, 14, or 30 days (Q17)."""
    rec = await load_owned_recommendation(db, id, current_user)
    if rec.status != "OPEN":
        raise HTTPException(status_code=400, detail=f"Cannot snooze recommendation in {rec.status} status")

    account = await assert_owned_account(db, rec.ads_account_id, current_user)
    today = today_for(account.marketplace)
    snooze_until = today + timedelta(days=payload.days)

    now_utc = datetime.now(timezone.utc)
    ev = dict(rec.evidence or {})
    ev["snoozed_until"] = snooze_until.isoformat()
    ev["snooze_days"] = payload.days
    rec.evidence = ev
    rec.status = "DISMISSED"
    rec.resolved_at = now_utc
    rec.resolved_by = current_user.id
    rec.apply_result = {"reason": f"Snoozed for {payload.days} days (until {snooze_until.isoformat()})"}

    await db.commit()
    await db.refresh(rec)
    return format_recommendation(rec)


@router.post("/{id}/undo")
async def undo_single_recommendation(
    id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Undoes an APPLIED recommendation within 7 days (Q17).
    Writes old value back only if live value still equals the applied one.
    """
    rec = await load_owned_recommendation(db, id, current_user)
    if rec.status != "APPLIED":
        raise HTTPException(status_code=400, detail="Only APPLIED recommendations can be undone")

    now_utc = datetime.now(timezone.utc)
    applied_at = rec.resolved_at or rec.created_at
    if applied_at:
        applied_utc = applied_at if applied_at.tzinfo else applied_at.replace(tzinfo=timezone.utc)
        if (now_utc - applied_utc).total_seconds() > 7 * 86400:
            raise HTTPException(status_code=400, detail="Recommendations can only be undone within 7 days of application")

    account = await assert_owned_account(db, rec.ads_account_id, current_user)
    provider = AmazonAdsProvider(db=db)
    is_connected_real_account = bool(account.refresh_token_encrypted and not account.is_mock)

    apply_result = rec.apply_result or {}
    changes = list(apply_result.get("changes") or [])
    if not changes:
        for act in (rec.suggested_actions or []):
            if "from" in act and "to" in act:
                changes.append({"field": act.get("type"), "from": act.get("from"), "to": act.get("to")})

    if not changes:
        raise HTTPException(status_code=400, detail="No applied changes recorded to undo")

    undone_changes: list[dict[str, Any]] = []
    for change in changes:
        fld = change.get("field")
        old_val = change.get("from")
        applied_val = change.get("to")

        if fld in ("bid", "INCREASE_BID", "DECREASE_BID"):
            kw = (await db.execute(select(Keyword).where(Keyword.id == rec.entity_id))).scalars().first()
            if not kw:
                raise HTTPException(404, detail="Keyword not found to undo")
            live_bid = kw.bid
            if live_bid is None:
                ag = (await db.execute(select(AdGroup).where(AdGroup.id == kw.ad_group_id))).scalars().first()
                live_bid = ag.default_bid if ag else None
            if live_bid is None or abs(Decimal(str(live_bid)) - Decimal(str(applied_val))) >= Decimal("0.01"):
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot undo: live bid ({live_bid}) no longer equals the applied value ({applied_val})"
                )
            target_bid = float(Decimal(str(old_val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            if is_connected_real_account:
                await provider.update_keyword_bids(account, [{"keyword_id": kw.external_id or str(kw.id), "bid": target_bid}])
            kw.bid = target_bid
            undone_changes.append({"field": "bid", "from": applied_val, "to": target_bid})

            history_entry = CampaignHistory(
                campaign_id=rec.campaign_id,
                event_type="RECOMMENDATION_UNDONE",
                event_title=f"Recommendation Undone: {rec.rule_name}",
                description=f"Reverted bid on '{kw.keyword_text}' from ₹{applied_val} back to ₹{target_bid}",
                previous_value=str(applied_val),
                new_value=str(target_bid),
                entity_type="keyword",
                entity_id=kw.id,
                recommendation_id=rec.id,
                user_id=current_user.id,
            )
            db.add(history_entry)

        elif fld in ("daily_budget", "INCREASE_BUDGET", "DECREASE_BUDGET", "SET_BUDGET"):
            c = (await db.execute(select(Campaign).where(Campaign.id == rec.campaign_id))).scalars().first()
            if not c:
                raise HTTPException(404, detail="Campaign not found to undo")
            live_budget = c.daily_budget
            if live_budget is None or abs(Decimal(str(live_budget)) - Decimal(str(applied_val))) >= Decimal("0.01"):
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot undo: live daily budget ({live_budget}) no longer equals the applied value ({applied_val})"
                )
            target_budget = float(Decimal(str(old_val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            if is_connected_real_account:
                await provider.update_campaign_budgets(account, [{"campaign_id": c.amazon_campaign_id or str(c.id), "daily_budget": target_budget}])
            c.daily_budget = target_budget
            undone_changes.append({"field": "daily_budget", "from": applied_val, "to": target_budget})

            history_entry = CampaignHistory(
                campaign_id=c.id,
                event_type="RECOMMENDATION_UNDONE",
                event_title=f"Recommendation Undone: {rec.rule_name}",
                description=f"Reverted daily budget from ₹{applied_val} back to ₹{target_budget}",
                previous_value=str(applied_val),
                new_value=str(target_budget),
                entity_type="campaign",
                entity_id=c.id,
                recommendation_id=rec.id,
                user_id=current_user.id,
            )
            db.add(history_entry)

        elif fld in ("state", "ENABLE_CAMPAIGN", "PAUSE_CAMPAIGN"):
            c = (await db.execute(select(Campaign).where(Campaign.id == rec.campaign_id))).scalars().first()
            if not c:
                raise HTTPException(404, detail="Campaign not found to undo")
            if c.state != applied_val:
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot undo: live campaign state ({c.state}) no longer equals applied state ({applied_val})"
                )
            target_state = str(old_val)
            if is_connected_real_account:
                await provider.set_states(account, entity_type="campaigns", items=[{"id": c.amazon_campaign_id or str(c.id), "state": target_state}])
            c.state = target_state
            undone_changes.append({"field": "state", "from": applied_val, "to": target_state})

            history_entry = CampaignHistory(
                campaign_id=c.id,
                event_type="RECOMMENDATION_UNDONE",
                event_title=f"Recommendation Undone: {rec.rule_name}",
                description=f"Reverted campaign state from {applied_val} back to {target_state}",
                previous_value=str(applied_val),
                new_value=str(target_state),
                entity_type="campaign",
                entity_id=c.id,
                recommendation_id=rec.id,
                user_id=current_user.id,
            )
            db.add(history_entry)

        elif fld in ("PAUSE_TARGET",):
            kw = (await db.execute(select(Keyword).where(Keyword.id == rec.entity_id))).scalars().first()
            if not kw:
                raise HTTPException(404, detail="Keyword not found to undo")
            if kw.state != applied_val:
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot undo: live keyword state ({kw.state}) no longer equals applied state ({applied_val})"
                )
            target_state = str(old_val)
            if is_connected_real_account:
                await provider.set_states(account, entity_type="keywords", items=[{"id": kw.external_id or str(kw.id), "state": target_state}])
            kw.state = target_state
            undone_changes.append({"field": "state", "from": applied_val, "to": target_state})

            history_entry = CampaignHistory(
                campaign_id=rec.campaign_id,
                event_type="RECOMMENDATION_UNDONE",
                event_title=f"Recommendation Undone: {rec.rule_name}",
                description=f"Reverted keyword state from {applied_val} back to {target_state}",
                previous_value=str(applied_val),
                new_value=str(target_state),
                entity_type="keyword",
                entity_id=kw.id,
                recommendation_id=rec.id,
                user_id=current_user.id,
            )
            db.add(history_entry)

        elif fld == "negative_keyword":
            nk_res = await db.execute(
                select(NegativeKeyword).where(
                    NegativeKeyword.campaign_id == rec.campaign_id,
                    NegativeKeyword.ad_group_id == rec.ad_group_id,
                    NegativeKeyword.keyword_text == rec.entity_label,
                )
            )
            nk = nk_res.scalars().first()
            if nk:
                await db.delete(nk)
                undone_changes.append({"field": "negative_keyword", "from": applied_val, "to": None})

    rec.status = "DISMISSED"
    rec.apply_result = {
        **apply_result,
        "undone_at": now_utc.isoformat(),
        "undone_by": current_user.id,
        "undone_changes": undone_changes,
    }
    await db.commit()
    await db.refresh(rec)
    return format_recommendation(rec)


@router.get("/{id}/outcome")
async def get_recommendation_outcome(
    id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Outcome tracking: 7 days before vs 7 days after for applied items (Q19).
    Label: 'Before/after comparison, not proof of cause'.
    """
    rec = await load_owned_recommendation(db, id, current_user)
    if rec.status != "APPLIED":
        return {
            "eligible": False,
            "recommendation_id": rec.id,
            "reason": "Outcome tracking is only available for applied recommendations",
            "disclaimer": "Before/after comparison, not proof of cause",
        }

    account = await assert_owned_account(db, rec.ads_account_id, current_user)
    today = today_for(account.marketplace)
    applied_dt = rec.resolved_at or rec.created_at
    applied_date = applied_dt.date() if hasattr(applied_dt, "date") else today

    before_start = applied_date - timedelta(days=7)
    before_end = applied_date - timedelta(days=1)
    after_start = applied_date + timedelta(days=1)
    after_end = min(today, applied_date + timedelta(days=7))

    def compute_window_metrics(rows: list[Any]) -> dict[str, Any]:
        spend = Decimal("0.00")
        sales = Decimal("0.00")
        orders = 0
        clicks = 0
        impressions = 0
        for r in rows:
            spend += Decimal(str(r.spend or 0))
            sales += Decimal(str(r.sales or 0))
            orders += int(r.orders or 0)
            clicks += int(r.clicks or 0)
            impressions += int(r.impressions or 0)

        spend_flt = float(spend.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        sales_flt = float(sales.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        acos = round((spend_flt / sales_flt) * 100, 2) if sales_flt > 0 else (None if spend_flt == 0 else float("inf"))
        cpc = round(spend_flt / clicks, 2) if clicks > 0 else None
        ctr = round((clicks / impressions) * 100, 2) if impressions > 0 else None
        cvr = round((orders / clicks) * 100, 2) if clicks > 0 else None

        return {
            "spend": spend_flt,
            "sales": sales_flt,
            "orders": orders,
            "clicks": clicks,
            "impressions": impressions,
            "acos": acos,
            "cpc": cpc,
            "ctr": ctr,
            "cvr": cvr,
        }

    filters_entity = []
    if rec.entity_type in ("keyword", "target", "search_term"):
        filters_entity.append(MetricDaily.entity_type == "keyword")
        filters_entity.append(MetricDaily.entity_id == rec.entity_id)
    elif rec.entity_type == "campaign":
        filters_entity.append(MetricDaily.entity_type == "campaign")
        filters_entity.append(MetricDaily.entity_id == rec.campaign_id)

    before_stmt = select(
        MetricDaily.spend, MetricDaily.sales, MetricDaily.orders, MetricDaily.clicks, MetricDaily.impressions
    ).where(
        *filters_entity,
        MetricDaily.date >= before_start,
        MetricDaily.date <= before_end,
    )
    before_rows = (await db.execute(before_stmt)).all()
    before_metrics = compute_window_metrics(before_rows)

    after_metrics = {"spend": 0.0, "sales": 0.0, "orders": 0, "clicks": 0, "impressions": 0, "acos": None, "cpc": None, "ctr": None, "cvr": None}
    after_days = max((after_end - after_start).days + 1, 0)
    if after_end >= after_start:
        after_stmt = select(
            MetricDaily.spend, MetricDaily.sales, MetricDaily.orders, MetricDaily.clicks, MetricDaily.impressions
        ).where(
            *filters_entity,
            MetricDaily.date >= after_start,
            MetricDaily.date <= after_end,
        )
        after_rows = (await db.execute(after_stmt)).all()
        after_metrics = compute_window_metrics(after_rows)

    def pct_diff(after_val: float | None, before_val: float | None) -> float | None:
        if after_val is None or before_val is None or before_val == 0:
            return None
        return round(((after_val - before_val) / before_val) * 100, 1)

    delta = {
        "spend_diff": round(after_metrics["spend"] - before_metrics["spend"], 2),
        "spend_change_pct": pct_diff(after_metrics["spend"], before_metrics["spend"]),
        "orders_diff": after_metrics["orders"] - before_metrics["orders"],
        "orders_change_pct": pct_diff(float(after_metrics["orders"]), float(before_metrics["orders"])),
        "sales_diff": round(after_metrics["sales"] - before_metrics["sales"], 2),
        "sales_change_pct": pct_diff(after_metrics["sales"], before_metrics["sales"]),
        "acos_diff": round(after_metrics["acos"] - before_metrics["acos"], 2) if (after_metrics["acos"] is not None and before_metrics["acos"] is not None and not math.isinf(after_metrics["acos"]) and not math.isinf(before_metrics["acos"])) else None,
    }

    return {
        "eligible": True,
        "recommendation_id": rec.id,
        "applied_at": applied_dt.isoformat() if hasattr(applied_dt, "isoformat") else str(applied_dt),
        "before_window": {"start": before_start.isoformat(), "end": before_end.isoformat(), "days": 7},
        "after_window": {"start": after_start.isoformat(), "end": after_end.isoformat(), "days": after_days},
        "before": before_metrics,
        "after": after_metrics,
        "delta": delta,
        "disclaimer": "Before/after comparison, not proof of cause",
    }
