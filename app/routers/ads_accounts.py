from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.models.ads_account import AdsAccount
from app.schemas.ads_account import AdsAccountResponse
from app.providers.ads.mock_provider import MockAdsProvider
from app.config import settings

router = APIRouter(prefix="/ads-accounts", tags=["ads-accounts"])

@router.post("/connect", response_model=AdsAccountResponse)
async def connect_amazon(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    # Fall back to creating a mock connected account for that user only
    account = AdsAccount(user_id=current_user.id, marketplace="US", is_mock=True)
    db.add(account)
    await db.commit()
    await db.refresh(account)
    
    provider = MockAdsProvider(db)
    await provider.generate_for_new_connection(account.id)
    
    return account

@router.get("", response_model=list[AdsAccountResponse])
async def list_accounts(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    from sqlalchemy import select
    result = await db.execute(select(AdsAccount).where(AdsAccount.user_id == current_user.id))
    return result.scalars().all()
