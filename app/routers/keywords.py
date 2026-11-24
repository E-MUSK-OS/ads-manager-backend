from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.dependencies import get_current_user, load_owned_ad_group, load_owned_keyword
from app.models.user import User
from app.models.keyword import Keyword
from app.models.negative_keyword import NegativeKeyword
from app.models.ad_group import AdGroup
from app.models.campaign import Campaign
from app.models.ads_account import AdsAccount
from app.models.campaign_history import CampaignHistory
from app.schemas.keyword import KeywordResponse, KeywordCreate, KeywordPatch
from app.schemas.campaign import clean_keyword_text
from app.core.marketplace_rules import marketplace_or_in, round_money
from app.providers.ads.amazon_provider import AmazonAdsProvider

router = APIRouter(prefix="/keywords", tags=["keywords"])

@router.get("", response_model=list[KeywordResponse])
async def get_keywords(ad_group_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    await load_owned_ad_group(db, ad_group_id, current_user)
    result = await db.execute(select(Keyword).where(Keyword.ad_group_id == ad_group_id))
    return result.scalars().all()

@router.post("", response_model=KeywordResponse)
async def create_keyword(
    payload: KeywordCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        cleaned_text = clean_keyword_text(payload.keyword_text)
    except ValueError as e:
        raise HTTPException(422, detail=[{"field": "keyword_text", "message": str(e)}])

    ag = await load_owned_ad_group(db, payload.ad_group_id, current_user)
    campaign = (await db.execute(select(Campaign).where(Campaign.id == ag.campaign_id))).scalars().first()
    account = (await db.execute(select(AdsAccount).where(AdsAccount.id == campaign.ads_account_id))).scalars().first()
    rules = marketplace_or_in(account.marketplace)

    if payload.bid is not None:
        if payload.bid < rules["min_bid"] or payload.bid > rules["max_bid"]:
            raise HTTPException(422, detail=[{"field": "bid", "message": f"Bid must be between {rules['min_bid']} and {rules['max_bid']}"}])

    # Duplicate check on (ad_group_id, keyword_text, match_type)
    existing = (await db.execute(
        select(Keyword).where(
            Keyword.ad_group_id == ag.id,
            Keyword.keyword_text == cleaned_text,
            Keyword.match_type == payload.match_type
        )
    )).scalars().first()
    if existing:
        raise HTTPException(409, detail="Keyword with this match type already exists in this ad group")

    # Conflict check: may not equal an exact negative of the same campaign
    exact_neg = (await db.execute(
        select(NegativeKeyword).where(
            NegativeKeyword.campaign_id == campaign.id,
            NegativeKeyword.keyword_text == cleaned_text,
            NegativeKeyword.match_type == "NEGATIVE_EXACT"
        )
    )).scalars().first()
    if exact_neg:
        raise HTTPException(422, detail=[{"field": "keyword_text", "message": f"Keyword '{cleaned_text}' conflicts with an existing exact negative keyword in this campaign"}])

    new_kw = Keyword(
        ad_group_id=ag.id,
        keyword_text=cleaned_text,
        match_type=payload.match_type,
        bid=payload.bid,
        state="ENABLED"
    )
    db.add(new_kw)
    await db.commit()
    await db.refresh(new_kw)
    return new_kw

@router.patch("/{id}", response_model=KeywordResponse)
async def patch_keyword(
    id: int,
    payload: KeywordPatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Update keyword bid and/or state.
    For connected real accounts, writes to Amazon Ads API first:
    PUT /sp/keywords (https://advertising.amazon.com/API/docs/en-us/sponsored-products/3-0/openapi/prod#/Keywords/updateKeywords)
    Quote: "Updates one or more keywords."
    Refuses with a clear message if keyword has no Amazon keyword ID until keyword sync exists.
    """
    kw = await load_owned_keyword(db, id, current_user)
    ag = (await db.execute(select(AdGroup).where(AdGroup.id == kw.ad_group_id))).scalars().first()
    campaign = (await db.execute(select(Campaign).where(Campaign.id == ag.campaign_id))).scalars().first()
    account = (await db.execute(select(AdsAccount).where(AdsAccount.id == campaign.ads_account_id))).scalars().first()
    
    if not account or not account.is_active or account.connection_status != "connected":
        raise HTTPException(
            status_code=400,
            detail="Cannot modify keywords for a disconnected or inactive Amazon Ads account."
        )

    rules = marketplace_or_in(account.marketplace)

    if payload.bid is not None:
        if payload.bid < rules["min_bid"] or payload.bid > rules["max_bid"]:
            raise HTTPException(422, detail=[{"field": "bid", "message": f"Bid must be between {rules['min_bid']} and {rules['max_bid']}"}])

    # Amazon First: write to Amazon Ads API if real connected account
    if not account.is_mock and account.refresh_token_encrypted:
        if not kw.external_id:
            raise HTTPException(
                status_code=400,
                detail="Keyword does not have an Amazon keyword ID. Keyword sync is required before editing keywords directly on Amazon."
            )

        provider = AmazonAdsProvider(db)
        item_payload = {"keyword_id": kw.external_id}
        if payload.bid is not None:
            item_payload["bid"] = payload.bid
        if payload.state is not None:
            item_payload["state"] = payload.state

        results = await provider.update_sp_keywords(account, [item_payload])
        if not results or not results[0].get("success"):
            err_msg = results[0].get("error") if results else "Failed to update keyword on Amazon Ads"
            raise HTTPException(status_code=400, detail=f"Amazon Ads API error: {err_msg}")

    # Local update from Amazon's successful response / mock update
    old_bid = kw.bid
    old_state = kw.state

    if payload.bid is not None:
        kw.bid = payload.bid

    if payload.state is not None:
        kw.state = payload.state

    hist = CampaignHistory(
        campaign_id=campaign.id,
        event_type="KEYWORD_UPDATE",
        event_title="Keyword Updated",
        previous_value=f"bid: ₹{old_bid:.2f}, state: {old_state}" if old_bid else str(old_state),
        new_value=f"bid: ₹{kw.bid:.2f}, state: {kw.state}" if kw.bid else str(kw.state),
        description=f"Keyword '{kw.keyword_text}' updated (Amazon sync validated)",
        user_id=current_user.id
    )
    db.add(hist)

    await db.commit()
    await db.refresh(kw)
    return kw
