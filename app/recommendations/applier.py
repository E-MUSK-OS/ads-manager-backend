from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.marketplace_rules import marketplace_or_us
from app.models.ads_account import AdsAccount
from app.models.campaign import Campaign
from app.models.ad_group import AdGroup
from app.models.keyword import Keyword
from app.models.negative_keyword import NegativeKeyword
from app.models.recommendation import Recommendation
from app.models.user import User
from app.schemas.campaign import clean_keyword_text


@dataclass
class ChangeRecord:
    changes: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


async def apply_action(
    db: AsyncSession,
    user: User,
    rec: Recommendation,
    action: dict[str, Any],
    overrides: Optional[dict[str, Any]] = None,
) -> ChangeRecord:
    account = (await db.execute(select(AdsAccount).where(AdsAccount.id == rec.ads_account_id))).scalars().first()
    if not account:
        raise HTTPException(404, detail="Ads account not found")

    m_rules = marketplace_or_us(account.marketplace)
    act_type = action.get("type")
    params = dict(action.get("params") or {})

    changes: list[dict[str, Any]] = []
    notes: list[str] = []

    if act_type == "ADD_AS_NEGATIVE":
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

        # Duplicate check: treat as already applied (idempotent, note it)
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
        else:
            # Conflict check: an exact negative equal to an enabled positive EXACT keyword in the campaign -> fail
            if match_type == "NEGATIVE_EXACT":
                exact_pos = (
                    await db.execute(
                        select(Keyword)
                        .join(AdGroup, Keyword.ad_group_id == AdGroup.id)
                        .where(
                            AdGroup.campaign_id == rec.campaign_id,
                            Keyword.state == "ENABLED",
                            Keyword.match_type == "EXACT",
                            Keyword.keyword_text == cleaned_text,
                        )
                    )
                ).scalars().first()
                if exact_pos:
                    raise HTTPException(
                        status_code=422,
                        detail=[{
                            "field": "keyword_text",
                            "message": f"Negative exact keyword '{cleaned_text}' conflicts with an enabled positive EXACT keyword in this campaign",
                        }],
                    )

            nk = NegativeKeyword(
                campaign_id=rec.campaign_id,
                ad_group_id=target_ad_group_id,
                keyword_text=cleaned_text,
                match_type=match_type,
            )
            db.add(nk)
            changes.append({
                "field": "negative_keyword",
                "from": None,
                "to": f"{cleaned_text} ({match_type}, {level})",
            })

    elif act_type == "ADD_AS_TARGETING":
        if overrides:
            if "match_type" in overrides:
                params["match_type"] = overrides["match_type"]
            if "bid" in overrides:
                params["bid"] = overrides["bid"]

        match_type = params.get("match_type", "EXACT")
        if match_type not in ("EXACT", "PHRASE", "BROAD"):
            raise HTTPException(422, detail=[{"field": "match_type", "message": f"Invalid match type: {match_type}"}])

        cleaned_text = clean_keyword_text(rec.entity_label)

        # Existing same text+match in ad group -> already applied (idempotent, note it)
        existing = (
            await db.execute(
                select(Keyword).where(
                    Keyword.ad_group_id == rec.ad_group_id,
                    Keyword.keyword_text == cleaned_text,
                    Keyword.match_type == match_type,
                )
            )
        ).scalars().first()

        if existing:
            notes.append(f"Keyword '{cleaned_text}' ({match_type}) already exists in this ad group (idempotent)")
        else:
            # Conflict check: must not equal an exact negative of the campaign
            exact_neg = (
                await db.execute(
                    select(NegativeKeyword).where(
                        NegativeKeyword.campaign_id == rec.campaign_id,
                        NegativeKeyword.keyword_text == cleaned_text,
                        NegativeKeyword.match_type == "NEGATIVE_EXACT",
                    )
                )
            ).scalars().first()
            if exact_neg:
                raise HTTPException(
                    status_code=422,
                    detail=[{
                        "field": "keyword_text",
                        "message": f"Keyword '{cleaned_text}' conflicts with an existing exact negative keyword in this campaign",
                    }],
                )

            bid = params.get("bid")
            if bid is not None:
                bid = float(bid)
                if bid < m_rules["min_bid"] or bid > m_rules["max_bid"]:
                    raise HTTPException(
                        status_code=422,
                        detail=[{
                            "field": "bid",
                            "message": f"Bid must be between {m_rules['min_bid']} and {m_rules['max_bid']}",
                        }],
                    )
            else:
                ag = (await db.execute(select(AdGroup).where(AdGroup.id == rec.ad_group_id))).scalars().first()
                bid = ag.default_bid if ag and ag.default_bid else None

            kw = Keyword(
                ad_group_id=rec.ad_group_id,
                keyword_text=cleaned_text,
                match_type=match_type,
                bid=bid,
                state="ENABLED",
            )
            db.add(kw)
            changes.append({
                "field": "keyword",
                "from": None,
                "to": f"{cleaned_text} ({match_type})",
            })

    elif act_type in ("INCREASE_BID", "DECREASE_BID"):
        kw = (await db.execute(select(Keyword).where(Keyword.id == rec.entity_id))).scalars().first()
        if not kw:
            raise HTTPException(404, detail="Target keyword not found")

        # Stale check
        expected_bid = (rec.evidence or {}).get("current", {}).get("bid")
        live_bid = kw.bid
        if live_bid is None:
            ag = (await db.execute(select(AdGroup).where(AdGroup.id == kw.ad_group_id))).scalars().first()
            live_bid = ag.default_bid if ag else None

        if expected_bid is not None and live_bid is not None:
            if abs(float(live_bid) - float(expected_bid)) > 1e-4:
                raise HTTPException(409, detail="This value changed since the analysis — run the analysis again")

        to_bid = None
        if overrides and "bid" in overrides:
            to_bid = float(overrides["bid"])
        elif "to" in action:
            to_bid = float(action["to"])
        else:
            pct = float(params.get("pct", 10))
            current = float(live_bid or m_rules["min_bid"])
            if act_type == "INCREASE_BID":
                to_bid = current * (1 + pct / 100)
            else:
                to_bid = current * (1 - pct / 100)

        new_bid = round(min(max(to_bid, m_rules["min_bid"]), m_rules["max_bid"]), 2)
        old_bid = kw.bid
        kw.bid = new_bid
        changes.append({"field": "bid", "from": old_bid, "to": new_bid})

    elif act_type == "PAUSE_TARGET":
        kw = (await db.execute(select(Keyword).where(Keyword.id == rec.entity_id))).scalars().first()
        if not kw:
            raise HTTPException(404, detail="Target keyword not found")

        # Stale check
        expected_state = (rec.evidence or {}).get("current", {}).get("state")
        if expected_state is not None and kw.state != expected_state:
            raise HTTPException(409, detail="This value changed since the analysis — run the analysis again")

        old_state = kw.state
        kw.state = "PAUSED"
        changes.append({"field": "state", "from": old_state, "to": "PAUSED"})

    elif act_type in ("INCREASE_BUDGET", "DECREASE_BUDGET"):
        c = (await db.execute(select(Campaign).where(Campaign.id == rec.campaign_id))).scalars().first()
        if not c:
            raise HTTPException(404, detail="Campaign not found")

        # Stale check
        expected_budget = (rec.evidence or {}).get("current", {}).get("daily_budget")
        if expected_budget is not None and abs(float(c.daily_budget or 0) - float(expected_budget)) > 1e-4:
            raise HTTPException(409, detail="This value changed since the analysis — run the analysis again")

        to_budget = None
        if overrides and "daily_budget" in overrides:
            to_budget = float(overrides["daily_budget"])
        elif "to" in action:
            to_budget = float(action["to"])
        else:
            pct = float(params.get("pct", 10))
            current = float(c.daily_budget or m_rules["min_budget"])
            if act_type == "INCREASE_BUDGET":
                to_budget = current * (1 + pct / 100)
            else:
                to_budget = current * (1 - pct / 100)

        new_budget = round(max(to_budget, m_rules["min_budget"]), 2)
        old_budget = c.daily_budget
        c.daily_budget = new_budget
        changes.append({"field": "daily_budget", "from": old_budget, "to": new_budget})

    elif act_type == "PAUSE_CAMPAIGN":
        c = (await db.execute(select(Campaign).where(Campaign.id == rec.campaign_id))).scalars().first()
        if not c:
            raise HTTPException(404, detail="Campaign not found")

        # Stale check
        expected_state = (rec.evidence or {}).get("current", {}).get("state")
        if expected_state is not None and c.state != expected_state:
            raise HTTPException(409, detail="This value changed since the analysis — run the analysis again")

        old_state = c.state
        c.state = "PAUSED"
        changes.append({"field": "state", "from": old_state, "to": "PAUSED"})

    elif act_type in ("INCREASE_PLACEMENT", "DECREASE_PLACEMENT"):
        c = (await db.execute(select(Campaign).where(Campaign.id == rec.campaign_id))).scalars().first()
        if not c:
            raise HTTPException(404, detail="Campaign not found")

        placement = params.get("placement") or (rec.evidence or {}).get("current", {}).get("placement")
        if not placement:
            if rec.entity_label == "Top of Search":
                placement = "TOP_OF_SEARCH"
            elif rec.entity_label == "Rest of Search":
                placement = "REST_OF_SEARCH"
            elif rec.entity_label == "Product Pages":
                placement = "PRODUCT_PAGES"
            else:
                placement = "TOP_OF_SEARCH"

        if placement == "TOP_OF_SEARCH":
            field_name = "placement_top_pct"
        elif placement == "REST_OF_SEARCH":
            field_name = "placement_rest_pct"
        elif placement == "PRODUCT_PAGES":
            field_name = "placement_product_page_pct"
        else:
            raise HTTPException(400, detail=f"Unknown placement: {placement}")

        live_pct = getattr(c, field_name, 0)

        # Stale check
        expected_pct = (rec.evidence or {}).get("current", {}).get("pct")
        if expected_pct is not None and abs(int(live_pct) - int(expected_pct)) > 0:
            raise HTTPException(409, detail="This value changed since the analysis — run the analysis again")

        to_pct = None
        if overrides and "pct" in overrides:
            to_pct = int(overrides["pct"])
        elif overrides and "to" in overrides:
            to_pct = int(overrides["to"])
        elif "to" in action:
            to_pct = int(action["to"])
        else:
            points = int(params.get("points", 10))
            if act_type == "INCREASE_PLACEMENT":
                to_pct = live_pct + points
            else:
                to_pct = live_pct - points

        # Clamp 0–900
        clamped_pct = max(0, min(900, to_pct))
        old_pct = live_pct
        setattr(c, field_name, clamped_pct)
        changes.append({"field": field_name, "from": old_pct, "to": clamped_pct})

    return ChangeRecord(changes=changes, notes=notes)


async def apply_recommendation(
    db: AsyncSession,
    user: User,
    rec: Recommendation,
    overrides: Optional[dict[str, Any]] = None,
) -> Recommendation:
    if rec.status != "OPEN":
        raise HTTPException(status_code=400, detail=f"Cannot apply recommendation in {rec.status} status")

    all_changes: list[dict[str, Any]] = []
    all_notes: list[str] = []

    for action in (rec.suggested_actions or []):
        change_rec = await apply_action(db, user, rec, action, overrides)
        all_changes.extend(change_rec.changes)
        all_notes.extend(change_rec.notes)

    rec.status = "APPLIED"
    rec.resolved_at = datetime.now(timezone.utc)
    rec.resolved_by = user.id
    rec.apply_result = {"changes": all_changes, "notes": all_notes}

    await db.commit()
    await db.refresh(rec)
    return rec
