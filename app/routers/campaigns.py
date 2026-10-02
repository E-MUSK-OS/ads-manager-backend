from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.models.campaign import Campaign
from app.models.ads_account import AdsAccount
from app.schemas.campaign import CampaignResponse

router = APIRouter(prefix="/campaigns", tags=["campaigns"])

@router.get("", response_model=list[CampaignResponse])
async def get_campaigns(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    query = select(Campaign).join(AdsAccount).where(AdsAccount.user_id == current_user.id)
    result = await db.execute(query)
    return result.scalars().all()

from app.schemas.campaign import CampaignCreate
from pydantic import BaseModel

class StateUpdate(BaseModel):
    state: str

@router.post("", response_model=CampaignResponse)
async def create_campaign(campaign_in: CampaignCreate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    new_campaign = Campaign(
        name=campaign_in.name,
        ads_account_id=campaign_in.ads_account_id,
        daily_budget=campaign_in.daily_budget,
        state=campaign_in.state,
        targeting_type=campaign_in.targeting_type,
        campaign_type=campaign_in.campaign_type
    )
    db.add(new_campaign)
    await db.commit()
    await db.refresh(new_campaign)
    return new_campaign

@router.put("/{campaign_id}/state")
async def update_state(campaign_id: int, state_update: StateUpdate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalars().first()
    if campaign:
        campaign.state = state_update.state
        await db.commit()
        return {"status": "ok"}
    return {"status": "not_found"}
