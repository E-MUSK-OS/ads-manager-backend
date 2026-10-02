from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.models.ad_group import AdGroup
from app.schemas.ad_group import AdGroupResponse

router = APIRouter(prefix="/ad-groups", tags=["ad-groups"])

@router.get("", response_model=list[AdGroupResponse])
async def get_ad_groups(campaign_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    result = await db.execute(select(AdGroup).where(AdGroup.campaign_id == campaign_id))
    return result.scalars().all()
