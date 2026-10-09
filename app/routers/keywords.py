from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.dependencies import get_current_user, load_owned_ad_group
from app.models.user import User
from app.models.keyword import Keyword
from app.schemas.keyword import KeywordResponse

router = APIRouter(prefix="/keywords", tags=["keywords"])

@router.get("", response_model=list[KeywordResponse])
async def get_keywords(ad_group_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    await load_owned_ad_group(db, ad_group_id, current_user)
    result = await db.execute(select(Keyword).where(Keyword.ad_group_id == ad_group_id))
    return result.scalars().all()
