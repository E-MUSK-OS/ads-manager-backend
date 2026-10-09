from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.models.metric_daily import MetricDaily
from app.schemas.metric_daily import MetricDailyResponse

router = APIRouter(prefix="/metrics", tags=["metrics"])

@router.get("", response_model=list[MetricDailyResponse])
async def get_metrics(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    from app.models.campaign import Campaign
    from app.models.ads_account import AdsAccount
    from datetime import date, timedelta
    
    thirty_days_ago = date.today() - timedelta(days=30)
    
    query = (
        select(MetricDaily)
        .join(Campaign, Campaign.id == MetricDaily.entity_id)
        .join(AdsAccount, AdsAccount.id == Campaign.ads_account_id)
        .where(
            AdsAccount.user_id == current_user.id,
            MetricDaily.entity_type == "campaign",
            MetricDaily.date >= thirty_days_ago
        )
    )
    result = await db.execute(query)
    return result.scalars().all()
