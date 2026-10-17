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
from app.schemas.keyword import KeywordResponse, KeywordCreate, KeywordPatch
from app.schemas.campaign import clean_keyword_text
from app.core.marketplace_rules import marketplace_or_us

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
    rules = marketplace_or_us(account.marketplace)

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
    kw = await load_owned_keyword(db, id, current_user)
    ag = (await db.execute(select(AdGroup).where(AdGroup.id == kw.ad_group_id))).scalars().first()
    campaign = (await db.execute(select(Campaign).where(Campaign.id == ag.campaign_id))).scalars().first()
    account = (await db.execute(select(AdsAccount).where(AdsAccount.id == campaign.ads_account_id))).scalars().first()
    rules = marketplace_or_us(account.marketplace)

    if payload.bid is not None:
        if payload.bid < rules["min_bid"] or payload.bid > rules["max_bid"]:
            raise HTTPException(422, detail=[{"field": "bid", "message": f"Bid must be between {rules['min_bid']} and {rules['max_bid']}"}])
        kw.bid = payload.bid

    if payload.state is not None:
        kw.state = payload.state

    await db.commit()
    await db.refresh(kw)
    return kw
