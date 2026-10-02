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
    result = await db.execute(select(MetricDaily).limit(100))
    return result.scalars().all()
