from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.dependencies import get_current_user, load_owned_campaign, load_owned_ad_group
from app.models.user import User
from app.models.ad_group import AdGroup
from app.models.campaign import Campaign
from app.models.ads_account import AdsAccount
from app.schemas.ad_group import AdGroupResponse, AdGroupPatch
from app.core.marketplace_rules import marketplace_or_us

router = APIRouter(prefix="/ad-groups", tags=["ad-groups"])

@router.get("", response_model=list[AdGroupResponse])
async def get_ad_groups(campaign_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    await load_owned_campaign(db, campaign_id, current_user)
    result = await db.execute(select(AdGroup).where(AdGroup.campaign_id == campaign_id))
    return result.scalars().all()

@router.patch("/{id}", response_model=AdGroupResponse)
async def patch_ad_group(
    id: int,
    payload: AdGroupPatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    ag = await load_owned_ad_group(db, id, current_user)
    campaign = (await db.execute(select(Campaign).where(Campaign.id == ag.campaign_id))).scalars().first()
    account = (await db.execute(select(AdsAccount).where(AdsAccount.id == campaign.ads_account_id))).scalars().first()
    rules = marketplace_or_us(account.marketplace)

    if payload.name is not None:
        name_clean = payload.name.strip()
        if not name_clean or len(name_clean) > 128:
            raise HTTPException(422, detail=[{"field": "name", "message": "Ad group name must be 1–128 characters"}])
        ag.name = name_clean

    if payload.default_bid is not None:
        if payload.default_bid < rules["min_bid"] or payload.default_bid > rules["max_bid"]:
            raise HTTPException(422, detail=[{"field": "default_bid", "message": f"Bid must be between {rules['min_bid']} and {rules['max_bid']}"}])
        ag.default_bid = payload.default_bid

    if payload.state is not None:
        ag.state = payload.state

    await db.commit()
    await db.refresh(ag)
    return ag
