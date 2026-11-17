from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import model_validator
from typing import Optional

# Regional endpoints mapping for Amazon Ads API and Login with Amazon (LWA)
# Values: na (North America), eu (Europe), fe (Far East)
# TODO(verify against official Amazon Ads API docs: verify regional endpoints, scopes, and correct routing for India)
AMAZON_REGIONAL_ENDPOINTS = {
    "na": {
        "ads_api_base_url": "https://advertising-api.amazon.com",
        "lwa_authorize_url": "https://www.amazon.com/ap/oa",
        "lwa_token_url": "https://api.amazon.com/auth/o2/token",
    },
    "eu": {
        "ads_api_base_url": "https://advertising-api-eu.amazon.com",
        "lwa_authorize_url": "https://eu.account.amazon.com/ap/oa",
        "lwa_token_url": "https://api.amazon.co.uk/auth/o2/token",
    },
    "fe": {
        "ads_api_base_url": "https://advertising-api-fe.amazon.com",
        "lwa_authorize_url": "https://apac.account.amazon.com/ap/oa",
        "lwa_token_url": "https://api.amazon.co.jp/auth/o2/token",
    },
}

class Settings(BaseSettings):
    # App
    ENVIRONMENT: str = "development"
    ADS_PROVIDER: str = "mock"
    LLM_PROVIDER: str = "claude"
    FRONTEND_URL: str = "http://localhost:3000"
    RECOMMENDATION_ENGINE: str = "rules"
    SUPPORTED_MARKETPLACES: str = "IN"
    CONTACT_RETENTION_DAYS: int = 365

    # Database
    DATABASE_URL: str
    REDIS_URL: Optional[str] = None

    # Auth
    JWT_SECRET_KEY: str
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    GOOGLE_OAUTH_CLIENT_ID: Optional[str] = None

    # Amazon Ads API / LWA
    AMAZON_ADS_REGION: str = "eu"  # na | eu | fe. TODO(verify against official Amazon Ads API docs: verify region for India)
    AMAZON_LWA_SCOPE: str = "advertising::campaign_management"  # TODO(verify against official Amazon Ads API docs)
    TOKEN_ENCRYPTION_KEY: Optional[str] = None
    AMAZON_LWA_CLIENT_ID: Optional[str] = None
    AMAZON_LWA_CLIENT_SECRET: Optional[str] = None
    AMAZON_LWA_REDIRECT_URI: Optional[str] = None
    AMAZON_ADS_API_BASE_URL: Optional[str] = None

    # AI
    ANTHROPIC_API_KEY: Optional[str] = None

    # Billing
    STRIPE_SECRET_KEY: Optional[str] = None
    STRIPE_WEBHOOK_SECRET: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"
    )

    @property
    def ads_api_base_url(self) -> str:
        if self.AMAZON_ADS_API_BASE_URL:
            return self.AMAZON_ADS_API_BASE_URL.rstrip("/")
        region = self.AMAZON_ADS_REGION.lower()
        endpoints = AMAZON_REGIONAL_ENDPOINTS.get(region, AMAZON_REGIONAL_ENDPOINTS["eu"])
        return endpoints["ads_api_base_url"]

    @property
    def lwa_authorize_url(self) -> str:
        region = self.AMAZON_ADS_REGION.lower()
        endpoints = AMAZON_REGIONAL_ENDPOINTS.get(region, AMAZON_REGIONAL_ENDPOINTS["eu"])
        return endpoints["lwa_authorize_url"]

    @property
    def lwa_token_url(self) -> str:
        region = self.AMAZON_ADS_REGION.lower()
        endpoints = AMAZON_REGIONAL_ENDPOINTS.get(region, AMAZON_REGIONAL_ENDPOINTS["eu"])
        return endpoints["lwa_token_url"]

    @property
    def supported_marketplaces(self) -> list[str]:
        return [m.strip().upper() for m in self.SUPPORTED_MARKETPLACES.split(",") if m.strip()]

    @model_validator(mode="after")
    def validate_production_and_amazon_config(self) -> "Settings":
        # Validate JWT_SECRET_KEY
        if not self.JWT_SECRET_KEY or self.JWT_SECRET_KEY.strip().lower() in (
            "changeme",
            "your_random_jwt_secret_key_at_least_32_characters",
        ):
            raise ValueError("JWT_SECRET_KEY must be configured and cannot be 'changeme' or the default placeholder")

        strict = self.ENVIRONMENT.lower() == "production" or self.ADS_PROVIDER.lower() != "mock"
        if strict:
            if len(self.JWT_SECRET_KEY) < 32:
                raise ValueError("JWT_SECRET_KEY must be at least 32 characters in production or non-mock mode")

        if self.ENVIRONMENT.lower() == "production":
            origins = [o.strip() for o in self.FRONTEND_URL.split(",") if o.strip()]
            for origin in origins:
                if not origin.startswith("https://"):
                    raise ValueError(f"FRONTEND_URL origin '{origin}' must start with 'https://' in production")

        # Ensure AMAZON_ADS_API_BASE_URL has a value if accessed directly
        if not self.AMAZON_ADS_API_BASE_URL:
            region = self.AMAZON_ADS_REGION.lower()
            endpoints = AMAZON_REGIONAL_ENDPOINTS.get(region, AMAZON_REGIONAL_ENDPOINTS["eu"])
            self.AMAZON_ADS_API_BASE_URL = endpoints["ads_api_base_url"]

        # Fail fast if ADS_PROVIDER is amazon and credentials/keys are missing
        if self.ADS_PROVIDER.lower() == "amazon":
            missing = []
            if not self.AMAZON_LWA_CLIENT_ID:
                missing.append("AMAZON_LWA_CLIENT_ID")
            if not self.AMAZON_LWA_CLIENT_SECRET:
                missing.append("AMAZON_LWA_CLIENT_SECRET")
            if not self.AMAZON_LWA_REDIRECT_URI:
                missing.append("AMAZON_LWA_REDIRECT_URI")
            if not self.TOKEN_ENCRYPTION_KEY:
                missing.append("TOKEN_ENCRYPTION_KEY")
            if missing:
                raise ValueError(
                    f"Fast-fail startup error: ADS_PROVIDER='amazon' requires: {', '.join(missing)}"
                )

        return self

settings = Settings()
