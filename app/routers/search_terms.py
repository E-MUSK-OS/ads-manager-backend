from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.dependencies import get_current_user, load_owned_campaign
from app.models.user import User
from app.models.search_term import SearchTerm
from app.schemas.search_term import SearchTermResponse

router = APIRouter(prefix="/search-terms", tags=["search-terms"])

@router.get("", response_model=list[SearchTermResponse])
async def get_search_terms(campaign_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    await load_owned_campaign(db, campaign_id, current_user)
    result = await db.execute(select(SearchTerm).where(SearchTerm.campaign_id == campaign_id))
    return result.scalars().all()
