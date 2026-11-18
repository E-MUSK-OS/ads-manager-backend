from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.providers.ads.base import AdsProvider
from app.providers.ads.amazon_provider import AmazonAdsProvider
from app.models.ads_account import AdsAccount

def get_ads_provider(db: AsyncSession, account: Optional[AdsAccount] = None) -> AdsProvider:
    """
    Return AmazonAdsProvider. Only Amazon Ads API is supported.
    """
    return AmazonAdsProvider(db)
