from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from app.database import get_db
from app.dependencies import get_current_user, assert_owned_account
from app.models.user import User
from app.models.ads_account import AdsAccount
from app.schemas.ads_account import AdsAccountResponse
from app.providers.ads.mock_provider import MockAdsProvider
from app.core.marketplace_rules import MARKETPLACE_RULES

router = APIRouter(prefix="/ads-accounts", tags=["ads-accounts"])

class ConnectAccountRequest(BaseModel):
    marketplace: str = "US"

@router.post("/connect", response_model=AdsAccountResponse)
async def connect_amazon(
    payload: ConnectAccountRequest,
    db: AsyncSession = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    marketplace = payload.marketplace.upper()
    if marketplace not in MARKETPLACE_RULES:
        raise HTTPException(400, "Unsupported marketplace")
    
    account = AdsAccount(user_id=current_user.id, marketplace=marketplace, is_mock=True)
    db.add(account)
    await db.commit()
    await db.refresh(account)
    
    provider = MockAdsProvider(db)
    await provider.generate_for_new_connection(account.id)
    
    return account

@router.get("", response_model=list[AdsAccountResponse])
async def list_accounts(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    result = await db.execute(select(AdsAccount).where(AdsAccount.user_id == current_user.id))
    return result.scalars().all()

@router.get("/{account_id}/rules")
async def get_account_rules(account_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    account = await assert_owned_account(db, account_id, current_user)
    return MARKETPLACE_RULES[account.marketplace]

@router.post("/{account_id}/sync")
async def sync_account(account_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    account = await assert_owned_account(db, account_id, current_user)
    provider = MockAdsProvider(db)
    await provider.sync_account_data(account.id)
    return {"status": "ok"}

