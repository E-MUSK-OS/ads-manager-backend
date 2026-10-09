from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.database import get_db
from app.config import settings
from app.models.user import User
from app.core.security import ALGORITHM

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

async def get_current_user(token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    
    result = await db.execute(select(User).where(User.id == int(user_id)))
    user = result.scalars().first()
    if user is None:
        raise credentials_exception
    return user

from app.models.ads_account import AdsAccount
from app.models.campaign import Campaign
from app.models.ad_group import AdGroup

async def assert_owned_account(db: AsyncSession, account_id: int, user: User) -> AdsAccount:
    acc = (await db.execute(select(AdsAccount).where(
        AdsAccount.id == account_id, AdsAccount.user_id == user.id))).scalars().first()
    if not acc:
        raise HTTPException(404, "Ads account not found")
    return acc

async def load_owned_campaign(db: AsyncSession, campaign_id: int, user: User) -> Campaign:
    c = (await db.execute(select(Campaign).join(AdsAccount).where(
        Campaign.id == campaign_id, AdsAccount.user_id == user.id))).scalars().first()
    if not c:
        raise HTTPException(404, "Campaign not found")
    return c

async def load_owned_ad_group(db: AsyncSession, ad_group_id: int, user: User) -> AdGroup:
    ag = (await db.execute(select(AdGroup).join(Campaign).join(AdsAccount).where(
        AdGroup.id == ad_group_id, AdsAccount.user_id == user.id))).scalars().first()
    if not ag:
        raise HTTPException(404, "Ad group not found")
    return ag
