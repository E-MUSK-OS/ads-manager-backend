from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.providers.ads.base import AdsProvider
from app.providers.ads.mock_provider import MockAdsProvider
from app.providers.ads.amazon_provider import AmazonAdsProvider
from app.models.ads_account import AdsAccount
from app.config import settings

def get_ads_provider(db: AsyncSession, account: Optional[AdsAccount] = None) -> AdsProvider:
    """
    Return MockAdsProvider when account.is_mock is True or ADS_PROVIDER == 'mock'.
    Return AmazonAdsProvider otherwise.
    """
    if account is not None and account.is_mock:
        return MockAdsProvider(db)
    if settings.ADS_PROVIDER.lower() == "mock":
        return MockAdsProvider(db)
    return AmazonAdsProvider(db)

