from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from decimal import Decimal, ROUND_HALF_UP

from app.core.marketplace_rules import marketplace_or_in, today_for
from app.config import settings
from app.models.ad_group import AdGroup
from app.models.ads_account import AdsAccount
from app.models.campaign import Campaign
from app.models.campaign_bid_threshold import CampaignBidThreshold
from app.models.campaign_history import CampaignHistory
from app.models.keyword import Keyword
from app.models.negative_keyword import NegativeKeyword
from app.models.recommendation import Recommendation
from app.models.sync_run import SyncRun
from app.models.user import User
from app.providers.ads.amazon_provider import AmazonAdsProvider, ensure_utc
from app.recommendations.conflicts import are_conflicting_recommendations
from app.recommendations.dates import is_in_freeze_period
from app.recommendations.engine import compute_decimal_bid_change, compute_decimal_budget_change, pct_change
from app.schemas.campaign import clean_keyword_text


# Account-level in-memory lock to prevent race conditions during apply
_account_locks: dict[int, asyncio.Lock] = {}


def get_account_lock(account_id: int) -> asyncio.Lock:
    if account_id not in _account_locks:
        _account_locks[account_id] = asyncio.Lock()
    return _account_locks[account_id]


def _sync_post_amazon_success(target_obj: Any, field_name: str, value: Any) -> None:
    """Safely updates a local ORM model field only after verified Amazon write success."""
    setattr(target_obj, field_name, value)


@dataclass
class ChangeRecord:
    changes: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    amazon_calls: list[dict[str, Any]] = field(default_factory=list)


async def apply_action(
    db: AsyncSession,
    user: User,
    rec: Recommendation,
    action: dict[str, Any],
    overrides: Optional[dict[str, Any]] = None,
) -> ChangeRecord:
    """Compatibility helper returning planned ChangeRecord."""
    account = (await db.execute(select(AdsAccount).where(AdsAccount.id == rec.ads_account_id))).scalars().first()
    if not account:
        raise HTTPException(404, detail="Ads account not found")
    provider = AmazonAdsProvider(db)
    planned, changes, notes = await validate_and_plan_action(
        db=db,
        account=account,
        provider=provider,
        rec=rec,
        action=action,
        overrides=overrides,
    )
    return ChangeRecord(changes=changes, notes=notes)


async def check_apply_guardrails(db: AsyncSession, account: AdsAccount) -> None:
    """Verifies profile and sync constraints before any write attempt."""
    if not account.is_active or account.connection_status != "connected":
        raise HTTPException(
            status_code=400,
            detail="Cannot apply recommendations to an inactive or disconnected ads account",
        )
    if account.profile_id and "dummy" in account.profile_id.lower():
        raise HTTPException(
            status_code=400,
            detail="Cannot apply recommendations to a dummy profile",
        )

    # Check for active running sync
    now_utc = datetime.now(timezone.utc)
    active_sync_res = await db.execute(
        select(SyncRun)
        .where(
            SyncRun.ads_account_id == account.id,
            SyncRun.status == "running",
        )
        .order_by(SyncRun.started_at.desc())
    )
    active_sync = active_sync_res.scalars().first()
    if active_sync:
        started = ensure_utc(active_sync.started_at)
        if started and (now_utc - started).total_seconds() < 600:
            raise HTTPException(
                status_code=409,
                detail="A synchronization is currently running for this account. Please wait for it to complete.",
            )

    # Check data freshness (Q7)
    max_age_hours = getattr(settings, "DATA_FRESHNESS_HOURS", 24)
    if account.last_synced_at is not None:
        last_sync_utc = ensure_utc(account.last_synced_at)
        if last_sync_utc and (now_utc - last_sync_utc).total_seconds() > max_age_hours * 3600:
            age_hours = (now_utc - last_sync_utc).total_seconds() / 3600.0
            raise HTTPException(
                status_code=409,
                detail=f"Data is stale: last successful sync was {age_hours:.1f} hours ago (limit is {max_age_hours} hours). Please synchronize the account before applying recommendations.",
            )

    # Check freeze periods (Q12: Apply blocked during active freeze period)
    today = today_for(account.marketplace)
    in_freeze, freeze_range = is_in_freeze_period(today)
    if in_freeze:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot apply recommendations: a freeze period is currently active ({freeze_range}).",
        )


async def validate_and_plan_action(
    db: AsyncSession,
    account: AdsAccount,
    provider: AmazonAdsProvider,
    rec: Recommendation,
    action: dict[str, Any],
    overrides: Optional[dict[str, Any]] = None,
    live_keywords: Optional[dict[str, dict[str, Any]]] = None,
    live_campaigns: Optional[dict[str, dict[str, Any]]] = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """
    Re-validates local DB and live Amazon values against recommendation evidence.
    If any value changed, raises HTTPException 409 (stale).
    Returns (planned_execution_dict, changes_list, notes_list).
    """
    m_rules = marketplace_or_in(account.marketplace)
    act_type = action.get("type")
    params = dict(action.get("params") or {})

    changes: list[dict[str, Any]] = []
    notes: list[str] = []
    planned: dict[str, Any] = {"type": act_type, "rec_id": rec.id}

    if act_type == "FLAGGED_CANNOT_NEGATE":
        raise HTTPException(
            status_code=422,
            detail=[{
                "field": "keyword_text",
                "message": f"Cannot negate search term '{rec.entity_label}': conflicts with an enabled positive EXACT keyword in this ad group",
            }],
        )

    elif act_type == "ADD_AS_NEGATIVE":
        if overrides:
            if "match_type" in overrides:
                params["match_type"] = overrides["match_type"]
            if "level" in overrides:
                params["level"] = overrides["level"]

        match_type = params.get("match_type", "NEGATIVE_EXACT")
        if match_type not in ("NEGATIVE_EXACT", "NEGATIVE_PHRASE"):
            raise HTTPException(422, detail=[{"field": "match_type", "message": f"Invalid match type: {match_type}"}])

        level = params.get("level", "AD_GROUP")
        if level not in ("AD_GROUP", "CAMPAIGN"):
            raise HTTPException(422, detail=[{"field": "level", "message": f"Invalid level: {level}"}])

        cleaned_text = clean_keyword_text(rec.entity_label)
        target_ad_group_id = rec.ad_group_id if level == "AD_GROUP" else None

        # Check existing (idempotent)
        if level == "CAMPAIGN":
            existing = (
                await db.execute(
                    select(NegativeKeyword).where(
                        NegativeKeyword.campaign_id == rec.campaign_id,
                        NegativeKeyword.ad_group_id.is_(None),
                        NegativeKeyword.keyword_text == cleaned_text,
                        NegativeKeyword.match_type == match_type,
                    )
                )
            ).scalars().first()
        else:
            existing = (
                await db.execute(
                    select(NegativeKeyword).where(
                        NegativeKeyword.campaign_id == rec.campaign_id,
                        NegativeKeyword.ad_group_id == rec.ad_group_id,
                        NegativeKeyword.keyword_text == cleaned_text,
                        NegativeKeyword.match_type == match_type,
                    )
                )
            ).scalars().first()

        if existing:
            notes.append(f"Negative keyword '{cleaned_text}' ({match_type}) already exists in this {level.lower()} (idempotent)")
            planned["skip_amazon"] = True
            return planned, changes, notes

        # Conflict check: never negate a term that is an ENABLED exact keyword in the same ad group (Q13)
        exact_pos = (
            await db.execute(
                select(Keyword)
                .join(AdGroup, Keyword.ad_group_id == AdGroup.id)
                .where(
                    AdGroup.campaign_id == rec.campaign_id,
                    Keyword.state == "ENABLED",
                    Keyword.match_type == "EXACT",
                    func.lower(Keyword.keyword_text) == cleaned_text.lower(),
                )
            )
        ).scalars().first()
        if exact_pos:
            raise HTTPException(
                status_code=422,
                detail=[{
                    "field": "keyword_text",
                    "message": f"Negative keyword '{cleaned_text}' conflicts with an enabled positive EXACT keyword in this ad group/campaign",
                }],
            )

        camp = (await db.execute(select(Campaign).where(Campaign.id == rec.campaign_id))).scalars().first()
        ag = (await db.execute(select(AdGroup).where(AdGroup.id == rec.ad_group_id))).scalars().first() if target_ad_group_id else None

        planned["level"] = level
        planned["campaign_id"] = rec.campaign_id
        planned["ad_group_id"] = target_ad_group_id
        planned["amazon_campaign_id"] = camp.amazon_campaign_id if camp and camp.amazon_campaign_id else str(rec.campaign_id)
        planned["amazon_ad_group_id"] = str(ag.id) if ag else None
        planned["keyword_text"] = cleaned_text
        planned["match_type"] = match_type

        changes.append({
            "field": "negative_keyword",
            "from": None,
            "to": f"{cleaned_text} ({match_type}, {level})",
        })

    elif act_type in ("INCREASE_BID", "DECREASE_BID"):
        kw = (await db.execute(select(Keyword).where(Keyword.id == rec.entity_id))).scalars().first()
        if not kw:
            raise HTTPException(404, detail="Target keyword not found")

        # Stale check local
        expected_bid = (rec.evidence or {}).get("current", {}).get("bid")
        live_bid = kw.bid
        if live_bid is None:
            ag = (await db.execute(select(AdGroup).where(AdGroup.id == kw.ad_group_id))).scalars().first()
            live_bid = ag.default_bid if ag else None

        if live_bid is None:
            raise HTTPException(400, detail="Target live bid is unknown — cannot apply recommendation")

        if expected_bid is not None:
            if abs(Decimal(str(live_bid)) - Decimal(str(expected_bid))) >= Decimal("0.01"):
                raise HTTPException(409, detail="This value changed since the analysis — run the analysis again")

        # Stale check Amazon live
        if live_keywords and kw.external_id and kw.external_id in live_keywords:
            amzn_kw = live_keywords[kw.external_id]
            if amzn_kw.get("bid") is not None and expected_bid is not None:
                if abs(Decimal(str(amzn_kw["bid"])) - Decimal(str(expected_bid))) >= Decimal("0.01"):
                    raise HTTPException(409, detail="This value changed since the analysis — run the analysis again")

        min_bid = Decimal(str(m_rules["min_bid"])).quantize(Decimal("0.01"))
        max_bid = Decimal(str(m_rules["max_bid"])).quantize(Decimal("0.01"))

        # Clamp to campaign's saved CampaignBidThreshold if present (Q6)
        if rec.campaign_id:
            bt = (await db.execute(select(CampaignBidThreshold).where(CampaignBidThreshold.campaign_id == rec.campaign_id))).scalars().first()
            if bt:
                if bt.min_bid is not None:
                    min_bid = max(min_bid, Decimal(str(bt.min_bid)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                if bt.max_bid is not None:
                    max_bid = min(max_bid, Decimal(str(bt.max_bid)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        curr_bid_dec = Decimal(str(live_bid)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        if overrides and "bid" in overrides:
            to_bid = Decimal(str(overrides["bid"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            clamped_new_bid = min(max(to_bid, min_bid), max_bid)
        elif "to" in action and action["to"] is not None:
            to_bid = Decimal(str(action["to"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            clamped_new_bid = min(max(to_bid, min_bid), max_bid)
        else:
            mode = params.get("mode", "percent")
            val = params.get("value", params.get("pct", 10))
            _, clamped_new_bid = compute_decimal_bid_change(
                current_bid=curr_bid_dec,
                pct=params.get("pct"),
                up=(act_type == "INCREASE_BID"),
                min_bid=min_bid,
                max_bid=max_bid,
                mode=mode,
                value=val,
            )

        # Cap check if server setting is configured (Q18)
        max_bid_pct_cap = getattr(settings, "MAX_BID_CHANGE_PCT_CAP", None)
        if max_bid_pct_cap is not None and curr_bid_dec > 0:
            change_pct = abs((clamped_new_bid - curr_bid_dec) / curr_bid_dec) * Decimal("100")
            if change_pct > Decimal(str(max_bid_pct_cap)):
                raise HTTPException(
                    422,
                    detail=f"Bid change of {change_pct:.1f}% exceeds maximum allowed limit of {max_bid_pct_cap}%",
                )

        new_bid = float(clamped_new_bid)
        old_bid = float(curr_bid_dec)

        planned["kw_obj"] = kw
        planned["amazon_keyword_id"] = kw.external_id or str(kw.id)
        planned["old_bid"] = old_bid
        planned["new_bid"] = new_bid

        changes.append({"field": "bid", "from": old_bid, "to": new_bid})

    elif act_type == "PAUSE_TARGET":
        kw = (await db.execute(select(Keyword).where(Keyword.id == rec.entity_id))).scalars().first()
        if not kw:
            raise HTTPException(404, detail="Target keyword not found")

        # Stale check local
        expected_state = (rec.evidence or {}).get("current", {}).get("state")
        if expected_state is not None and kw.state != expected_state:
            raise HTTPException(409, detail="This value changed since the analysis — run the analysis again")

        # Stale check Amazon live
        if live_keywords and kw.external_id and kw.external_id in live_keywords:
            amzn_kw = live_keywords[kw.external_id]
            if amzn_kw.get("state") and expected_state and amzn_kw["state"] != expected_state:
                raise HTTPException(409, detail="This value changed since the analysis — run the analysis again")

        old_state = kw.state
        planned["kw_obj"] = kw
        planned["amazon_keyword_id"] = kw.external_id or str(kw.id)
        planned["old_state"] = old_state
        planned["new_state"] = "PAUSED"

        changes.append({"field": "state", "from": old_state, "to": "PAUSED"})

    elif act_type in ("INCREASE_BUDGET", "DECREASE_BUDGET"):
        c = (await db.execute(select(Campaign).where(Campaign.id == rec.campaign_id))).scalars().first()
        if not c:
            raise HTTPException(404, detail="Campaign not found")

        # Stale check local
        expected_budget = (rec.evidence or {}).get("current", {}).get("daily_budget")
        if c.daily_budget is None:
            raise HTTPException(400, detail="Campaign live daily budget is unknown — cannot apply recommendation")

        if expected_budget is not None:
            if abs(Decimal(str(c.daily_budget)) - Decimal(str(expected_budget))) >= Decimal("0.01"):
                raise HTTPException(409, detail="This value changed since the analysis — run the analysis again")

        # Stale check Amazon live
        if live_campaigns and c.amazon_campaign_id and c.amazon_campaign_id in live_campaigns:
            amzn_c = live_campaigns[c.amazon_campaign_id]
            if amzn_c.get("daily_budget") is not None and expected_budget is not None:
                if abs(Decimal(str(amzn_c["daily_budget"])) - Decimal(str(expected_budget))) >= Decimal("0.01"):
                    raise HTTPException(409, detail="This value changed since the analysis — run the analysis again")

        min_budget = Decimal(str(m_rules["min_budget"])).quantize(Decimal("0.01"))
        max_budget = Decimal(str(m_rules.get("max_daily_budget", 20000000.0))).quantize(Decimal("0.01"))
        curr_budget_dec = Decimal(str(c.daily_budget)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        if overrides and "daily_budget" in overrides:
            to_budget = Decimal(str(overrides["daily_budget"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            clamped_new_budget = min(max(to_budget, min_budget), max_budget)
        elif "to" in action and action["to"] is not None:
            to_budget = Decimal(str(action["to"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            clamped_new_budget = min(max(to_budget, min_budget), max_budget)
        else:
            mode = params.get("mode", "percent")
            val = params.get("value", params.get("pct", 10))
            _, clamped_new_budget = compute_decimal_budget_change(
                current_budget=curr_budget_dec,
                pct=params.get("pct"),
                up=(act_type == "INCREASE_BUDGET"),
                min_budget=min_budget,
                max_budget=max_budget,
                mode=mode,
                value=val,
            )

        new_budget = float(clamped_new_budget)
        old_budget = float(curr_budget_dec)

        planned["campaign_obj"] = c
        planned["amazon_campaign_id"] = c.amazon_campaign_id or str(c.id)
        planned["old_budget"] = old_budget
        planned["new_budget"] = new_budget

        changes.append({"field": "daily_budget", "from": old_budget, "to": new_budget})

    elif act_type == "SET_BUDGET":
        c = (await db.execute(select(Campaign).where(Campaign.id == rec.campaign_id))).scalars().first()
        if not c:
            raise HTTPException(404, detail="Campaign not found")

        # Stale check local
        expected_budget = (rec.evidence or {}).get("current", {}).get("daily_budget")
        if c.daily_budget is None:
            raise HTTPException(400, detail="Campaign live daily budget is unknown — cannot apply recommendation")

        if expected_budget is not None:
            if abs(Decimal(str(c.daily_budget)) - Decimal(str(expected_budget))) >= Decimal("0.01"):
                raise HTTPException(409, detail="This value changed since the analysis — run the analysis again")

        # Stale check Amazon live
        if live_campaigns and c.amazon_campaign_id and c.amazon_campaign_id in live_campaigns:
            amzn_c = live_campaigns[c.amazon_campaign_id]
            if amzn_c.get("daily_budget") is not None and expected_budget is not None:
                if abs(Decimal(str(amzn_c["daily_budget"])) - Decimal(str(expected_budget))) >= Decimal("0.01"):
                    raise HTTPException(409, detail="This value changed since the analysis — run the analysis again")

        min_budget = Decimal(str(m_rules["min_budget"])).quantize(Decimal("0.01"))
        max_budget = Decimal(str(m_rules.get("max_daily_budget", 20000000.0))).quantize(Decimal("0.01"))
        curr_budget_dec = Decimal(str(c.daily_budget)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        if overrides and "daily_budget" in overrides:
            to_budget = Decimal(str(overrides["daily_budget"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        elif "to" in action and action["to"] is not None:
            to_budget = Decimal(str(action["to"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            val = params.get("value")
            if val is None:
                raise HTTPException(400, detail="SET_BUDGET requires a target value")
            to_budget = Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        clamped_new_budget = min(max(to_budget, min_budget), max_budget)
        new_budget = float(clamped_new_budget)
        old_budget = float(curr_budget_dec)

        planned["campaign_obj"] = c
        planned["amazon_campaign_id"] = c.amazon_campaign_id or str(c.id)
        planned["old_budget"] = old_budget
        planned["new_budget"] = new_budget

        changes.append({"field": "daily_budget", "from": old_budget, "to": new_budget})

    elif act_type == "ENABLE_CAMPAIGN":
        c = (await db.execute(select(Campaign).where(Campaign.id == rec.campaign_id))).scalars().first()
        if not c:
            raise HTTPException(404, detail="Campaign not found")

        # Stale check local: expected state must be PAUSED
        expected_state = (rec.evidence or {}).get("current", {}).get("state", "PAUSED")
        if c.state != "PAUSED" or (expected_state is not None and c.state != expected_state):
            raise HTTPException(409, detail="This value changed since the analysis — run the analysis again")

        # Stale check Amazon live
        if live_campaigns and c.amazon_campaign_id and c.amazon_campaign_id in live_campaigns:
            amzn_c = live_campaigns[c.amazon_campaign_id]
            if amzn_c.get("state") and amzn_c["state"] != "PAUSED":
                raise HTTPException(409, detail="This value changed since the analysis — run the analysis again")

        old_state = c.state
        planned["campaign_obj"] = c
        planned["amazon_campaign_id"] = c.amazon_campaign_id or str(c.id)
        planned["old_state"] = old_state
        planned["new_state"] = "ENABLED"

        changes.append({"field": "state", "from": old_state, "to": "ENABLED"})

    elif act_type == "PAUSE_CAMPAIGN":
        c = (await db.execute(select(Campaign).where(Campaign.id == rec.campaign_id))).scalars().first()
        if not c:
            raise HTTPException(404, detail="Campaign not found")

        # Stale check local
        expected_state = (rec.evidence or {}).get("current", {}).get("state")
        if expected_state is not None and c.state != expected_state:
            raise HTTPException(409, detail="This value changed since the analysis — run the analysis again")

        # Stale check Amazon live
        if live_campaigns and c.amazon_campaign_id and c.amazon_campaign_id in live_campaigns:
            amzn_c = live_campaigns[c.amazon_campaign_id]
            if amzn_c.get("state") and expected_state and amzn_c["state"] != expected_state:
                raise HTTPException(409, detail="This value changed since the analysis — run the analysis again")

        old_state = c.state
        planned["campaign_obj"] = c
        planned["amazon_campaign_id"] = c.amazon_campaign_id or str(c.id)
        planned["old_state"] = old_state
        planned["new_state"] = "PAUSED"

        changes.append({"field": "state", "from": old_state, "to": "PAUSED"})

    elif act_type == "ADD_AS_TARGETING":
        # Hidden/not supported in R4
        raise HTTPException(400, detail="Action ADD_AS_TARGETING is not enabled for write.")

    elif act_type in ("INCREASE_PLACEMENT", "DECREASE_PLACEMENT"):
        raise HTTPException(400, detail="Placements are read-only and cannot be applied.")

    else:
        raise HTTPException(400, detail=f"Unsupported action type: {act_type}")

    return planned, changes, notes


async def apply_batch_recommendations(
    db: AsyncSession,
    user: User,
    recs: list[Recommendation],
    overrides_map: Optional[dict[int, dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """
    Applies a batch of recommendations:
    1. Locks the account.
    2. Enforces guardrails (no dummy profile, no running sync).
    3. Detects conflicts across the batch.
    4. Gathers live state from Amazon & local DB for stale validation.
    5. Dispatches official batch write calls to Amazon Ads API.
    6. On Amazon confirmed success, updates local DB & writes CampaignHistory.
    7. Returns per-recommendation execution results.
    """
    if not recs:
        return []

    account_id = recs[0].ads_account_id
    account = (await db.execute(select(AdsAccount).where(AdsAccount.id == account_id))).scalars().first()
    if not account:
        raise HTTPException(404, detail="Ads account not found")

    async with get_account_lock(account_id):
        # 1. Guardrail checks
        await check_apply_guardrails(db, account)

        # 2. Intra-batch conflict check
        for i in range(len(recs)):
            for j in range(i + 1, len(recs)):
                r1, r2 = recs[i], recs[j]
                if are_conflicting_recommendations(r1, r2):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Conflict: Cannot apply recommendation #{r1.id} ('{r1.rule_name}') "
                            f"and #{r2.id} ('{r2.rule_name}') together on the same {r1.entity_type} ('{r1.entity_label}')."
                        ),
                    )

        provider = AmazonAdsProvider(db)

        # 3. Pre-fetch external IDs for live checking
        kw_ext_ids = []
        camp_ext_ids = []
        for r in recs:
            if r.affected_parameter == "TARGETING" and r.entity_id:
                kw = (await db.execute(select(Keyword).where(Keyword.id == r.entity_id))).scalars().first()
                if kw and kw.external_id:
                    kw_ext_ids.append(kw.external_id)
            if r.campaign_id:
                c = (await db.execute(select(Campaign).where(Campaign.id == r.campaign_id))).scalars().first()
                if c and c.amazon_campaign_id:
                    camp_ext_ids.append(c.amazon_campaign_id)

        live_keywords: dict[str, dict[str, Any]] = {}
        live_campaigns: dict[str, dict[str, Any]] = {}

        is_connected_real_account = bool(account.refresh_token_encrypted and not account.is_mock)
        if is_connected_real_account:
            try:
                if kw_ext_ids:
                    live_keywords = await provider.fetch_live_keywords(account, kw_ext_ids)
                if camp_ext_ids:
                    live_campaigns = await provider.fetch_live_campaigns(account, camp_ext_ids)
            except Exception as e:
                logger_err = f"Failed to fetch live Amazon entities: {e}"
                # If auth failure, will raise properly during write or get_access_token
                pass

        results = []

        for rec in recs:
            if rec.status != "OPEN":
                results.append({
                    "id": rec.id,
                    "success": False,
                    "error": f"Cannot apply recommendation in {rec.status} status",
                    "recommendation": rec,
                })
                continue

            overrides = (overrides_map or {}).get(rec.id)
            all_planned = []
            all_changes = []
            all_notes = []
            stale_error: Optional[Any] = None
            stale_status_code: int = 400

            for action in (rec.suggested_actions or []):
                try:
                    planned, changes, notes = await validate_and_plan_action(
                        db=db,
                        account=account,
                        provider=provider,
                        rec=rec,
                        action=action,
                        overrides=overrides,
                        live_keywords=live_keywords,
                        live_campaigns=live_campaigns,
                    )
                    all_planned.append(planned)
                    all_changes.extend(changes)
                    all_notes.extend(notes)
                except HTTPException as he:
                    stale_error = he.detail
                    stale_status_code = he.status_code
                    break
                except Exception as ex:
                    stale_error = str(ex)
                    stale_status_code = 400
                    break

            if stale_error:
                results.append({
                    "id": rec.id,
                    "success": False,
                    "error": stale_error,
                    "status_code": stale_status_code,
                    "recommendation": rec,
                })
                continue

            # Check total daily budget increase cap across batch (Q18)
            max_budget_inc_cap = getattr(settings, "MAX_DAILY_BUDGET_INCREASE_CAP", None)
            if max_budget_inc_cap is not None:
                total_budget_increase = Decimal("0.00")
                for plan in all_planned:
                    if plan.get("type") in ("INCREASE_BUDGET", "SET_BUDGET"):
                        old_b = Decimal(str(plan.get("old_budget", 0)))
                        new_b = Decimal(str(plan.get("new_budget", 0)))
                        if new_b > old_b:
                            total_budget_increase += (new_b - old_b)
                if total_budget_increase > Decimal(str(max_budget_inc_cap)):
                    results.append({
                        "id": rec.id,
                        "success": False,
                        "error": f"Total daily budget increase of ₹{total_budget_increase:.2f} exceeds maximum allowed limit of ₹{max_budget_inc_cap}",
                        "status_code": 422,
                        "recommendation": rec,
                    })
                    continue

            # Now execute planned actions with Amazon
            apply_failed = False
            amazon_error_msg = None
            amazon_status_code: int = 400

            for plan in all_planned:
                act_type = plan.get("type")
                if plan.get("skip_amazon"):
                    continue

                if is_connected_real_account:
                    try:
                        if act_type in ("INCREASE_BID", "DECREASE_BID"):
                            res = await provider.update_keyword_bids(
                                account=account,
                                items=[{"keyword_id": plan["amazon_keyword_id"], "bid": plan["new_bid"]}],
                            )
                            if not res or not res[0].get("success"):
                                apply_failed = True
                                amazon_error_msg = res[0].get("error") if res else "Unknown Amazon error"
                                break

                        elif act_type == "PAUSE_TARGET":
                            res = await provider.set_states(
                                account=account,
                                entity_type="keyword",
                                items=[{"entity_id": plan["amazon_keyword_id"], "state": "PAUSED"}],
                            )
                            if not res or not res[0].get("success"):
                                apply_failed = True
                                amazon_error_msg = res[0].get("error") if res else "Unknown Amazon error"
                                break

                        elif act_type in ("INCREASE_BUDGET", "DECREASE_BUDGET", "SET_BUDGET"):
                            res = await provider.update_campaign_budgets(
                                account=account,
                                items=[{"campaign_id": plan["amazon_campaign_id"], "daily_budget": plan["new_budget"]}],
                            )
                            if not res or not res[0].get("success"):
                                apply_failed = True
                                amazon_error_msg = res[0].get("error") if res else "Unknown Amazon error"
                                break

                        elif act_type in ("PAUSE_CAMPAIGN", "ENABLE_CAMPAIGN"):
                            res = await provider.set_states(
                                account=account,
                                entity_type="campaign",
                                items=[{"entity_id": plan["amazon_campaign_id"], "state": plan["new_state"]}],
                            )
                            if not res or not res[0].get("success"):
                                apply_failed = True
                                amazon_error_msg = res[0].get("error") if res else "Unknown Amazon error"
                                break

                        elif act_type == "ADD_AS_NEGATIVE":
                            res = await provider.create_negative_keywords(
                                account=account,
                                items=[{
                                    "campaign_id": plan["amazon_campaign_id"],
                                    "ad_group_id": plan["amazon_ad_group_id"],
                                    "keyword_text": plan["keyword_text"],
                                    "match_type": plan["match_type"],
                                }],
                            )
                            if not res or not res[0].get("success"):
                                apply_failed = True
                                amazon_error_msg = res[0].get("error") if res else "Unknown Amazon error"
                                break

                    except HTTPException as he:
                        apply_failed = True
                        amazon_error_msg = str(he.detail)
                        amazon_status_code = he.status_code
                        break
                    except Exception as exc:
                        apply_failed = True
                        amazon_error_msg = str(exc)
                        amazon_status_code = 502
                        break

            if apply_failed:
                # Keep recommendation OPEN and record error
                rec.apply_result = {"error": amazon_error_msg}
                results.append({
                    "id": rec.id,
                    "success": False,
                    "error": amazon_error_msg,
                    "status_code": amazon_status_code,
                    "recommendation": rec,
                })
                continue

            # Amazon write succeeded (or simulated in mock test account) -> Apply local updates & record history
            for plan in all_planned:
                act_type = plan.get("type")
                if act_type in ("INCREASE_BID", "DECREASE_BID"):
                    kw_obj = plan["kw_obj"]
                    _sync_post_amazon_success(kw_obj, "bid", plan["new_bid"])

                    history_entry = CampaignHistory(
                        campaign_id=rec.campaign_id,
                        event_type="RECOMMENDATION_APPLIED",
                        event_title=f"Recommendation Applied: {rec.rule_name}",
                        description=f"Keyword '{kw_obj.keyword_text}' bid updated from ₹{plan['old_bid']:.2f} to ₹{plan['new_bid']:.2f}",
                        previous_value=str(plan["old_bid"]),
                        new_value=str(plan["new_bid"]),
                        entity_type="keyword",
                        entity_id=kw_obj.id,
                        recommendation_id=rec.id,
                        user_id=user.id,
                    )
                    db.add(history_entry)

                elif act_type == "PAUSE_TARGET":
                    kw_obj = plan["kw_obj"]
                    _sync_post_amazon_success(kw_obj, "state", "PAUSED")

                    history_entry = CampaignHistory(
                        campaign_id=rec.campaign_id,
                        event_type="RECOMMENDATION_APPLIED",
                        event_title=f"Recommendation Applied: {rec.rule_name}",
                        description=f"Keyword '{kw_obj.keyword_text}' state changed from {plan['old_state']} to PAUSED",
                        previous_value=plan["old_state"],
                        new_value="PAUSED",
                        entity_type="keyword",
                        entity_id=kw_obj.id,
                        recommendation_id=rec.id,
                        user_id=user.id,
                    )
                    db.add(history_entry)

                elif act_type in ("INCREASE_BUDGET", "DECREASE_BUDGET", "SET_BUDGET"):
                    c_obj = plan["campaign_obj"]
                    _sync_post_amazon_success(c_obj, "daily_budget", plan["new_budget"])

                    history_entry = CampaignHistory(
                        campaign_id=rec.campaign_id,
                        event_type="RECOMMENDATION_APPLIED",
                        event_title=f"Recommendation Applied: {rec.rule_name}",
                        description=f"Campaign daily budget updated from ₹{plan['old_budget']:.2f} to ₹{plan['new_budget']:.2f}",
                        previous_value=str(plan["old_budget"]),
                        new_value=str(plan["new_budget"]),
                        entity_type="campaign",
                        entity_id=c_obj.id,
                        recommendation_id=rec.id,
                        user_id=user.id,
                    )
                    db.add(history_entry)

                elif act_type in ("PAUSE_CAMPAIGN", "ENABLE_CAMPAIGN"):
                    c_obj = plan["campaign_obj"]
                    _sync_post_amazon_success(c_obj, "state", plan["new_state"])
                    new_delivery_status = "DELIVERING" if plan["new_state"] == "ENABLED" else "PAUSED"
                    _sync_post_amazon_success(c_obj, "delivery_status", new_delivery_status)

                    history_entry = CampaignHistory(
                        campaign_id=rec.campaign_id,
                        event_type="RECOMMENDATION_APPLIED",
                        event_title=f"Recommendation Applied: {rec.rule_name}",
                        description=f"Campaign state changed from {plan['old_state']} to {plan['new_state']}",
                        previous_value=plan["old_state"],
                        new_value=plan["new_state"],
                        entity_type="campaign",
                        entity_id=c_obj.id,
                        recommendation_id=rec.id,
                        user_id=user.id,
                    )
                    db.add(history_entry)

                elif act_type == "ADD_AS_NEGATIVE" and not plan.get("skip_amazon"):
                    nk = NegativeKeyword(
                        campaign_id=plan["campaign_id"],
                        ad_group_id=plan["ad_group_id"],
                        keyword_text=plan["keyword_text"],
                        match_type=plan["match_type"],
                    )
                    db.add(nk)
                    await db.flush()

                    history_entry = CampaignHistory(
                        campaign_id=rec.campaign_id,
                        event_type="RECOMMENDATION_APPLIED",
                        event_title=f"Recommendation Applied: {rec.rule_name}",
                        description=f"Added negative {plan['level'].lower()} keyword '{plan['keyword_text']}' ({plan['match_type']})",
                        previous_value=None,
                        new_value=f"{plan['keyword_text']} ({plan['match_type']}, {plan['level']})",
                        entity_type="negative_keyword",
                        entity_id=nk.id,
                        recommendation_id=rec.id,
                        user_id=user.id,
                    )
                    db.add(history_entry)

            rec.status = "APPLIED"
            rec.resolved_at = datetime.now(timezone.utc)
            rec.resolved_by = user.id
            rec.apply_result = {"changes": all_changes, "notes": all_notes}

            results.append({
                "id": rec.id,
                "success": True,
                "error": None,
                "recommendation": rec,
            })

        await db.commit()
        for res in results:
            if res.get("recommendation"):
                await db.refresh(res["recommendation"])

        return results


async def apply_recommendation(
    db: AsyncSession,
    user: User,
    rec: Recommendation,
    overrides: Optional[dict[str, Any]] = None,
) -> Recommendation:
    """Convenience wrapper for applying a single recommendation."""
    results = await apply_batch_recommendations(
        db=db,
        user=user,
        recs=[rec],
        overrides_map={rec.id: overrides} if overrides else None,
    )
    res = results[0]
    if not res["success"]:
        err = res["error"]
        code = res.get("status_code") or (409 if "stale" in str(err).lower() or "changed since the analysis" in str(err).lower() else 400)
        raise HTTPException(status_code=code, detail=err)

    return res["recommendation"]
