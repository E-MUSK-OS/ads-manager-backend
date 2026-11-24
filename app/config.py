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
    ADS_PROVIDER: str = "amazon"
    LLM_PROVIDER: str = "none"
    FRONTEND_URL: str = "http://localhost:3000"
    RECOMMENDATION_ENGINE: str = "rules"
    SUPPORTED_MARKETPLACES: str = "IN"
    CONTACT_RETENTION_DAYS: int = 365
    ENABLE_PLACEMENT_RULES: bool = False

    # Recommendation Guards & Caps (Empty by default per C5, Q5, Q7, Q12, Q18)
    ENTITY_CHANGE_GUARD_DAYS: Optional[int] = None
    MAX_BID_CHANGE_PCT_CAP: Optional[float] = None
    MAX_DAILY_BUDGET_INCREASE_CAP: Optional[float] = None
    FREEZE_PERIODS: str = ""
    AMAZON_REPORTING_LOOKBACK_DAYS: int = 60
    DATA_FRESHNESS_HOURS: int = 24

    # Database
    DATABASE_URL: str
    REDIS_URL: Optional[str] = None

    # Auth
    JWT_SECRET_KEY: str
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days (10,080 minutes)
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7  # 7 days
    GOOGLE_OAUTH_CLIENT_ID: Optional[str] = None

    # Amazon Ads API / LWA
    AMAZON_ADS_REGION: str = "eu"  # India belongs to EU region: https://advertising.amazon.com/API/docs/en-us/guides/get-started/create-authorization-grant
    AMAZON_LWA_SCOPE: str = "advertising::campaign_management"
    AMAZON_OAUTH_STATE_TTL_MINUTES: int = 10
    FRONTEND_BASE_URL: Optional[str] = None
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

    @property
    def frontend_base_url(self) -> str:
        if self.ENVIRONMENT.lower() == "production":
            effective = self.FRONTEND_BASE_URL or self.FRONTEND_URL
            if not effective or effective.startswith("http://localhost") or effective.startswith("http://127.0.0.1"):
                raise ValueError(
                    "FRONTEND_BASE_URL must be configured with a public HTTPS URL in production (never fallback to localhost)"
                )
            return effective.rstrip("/")
        return (self.FRONTEND_BASE_URL or self.FRONTEND_URL).rstrip("/")

    @model_validator(mode="after")
    def validate_production_and_amazon_config(self) -> "Settings":
        # Validate JWT_SECRET_KEY
        if not self.JWT_SECRET_KEY or self.JWT_SECRET_KEY.strip().lower() in (
            "changeme",
            "your_random_jwt_secret_key_at_least_32_characters",
        ):
            raise ValueError("JWT_SECRET_KEY must be configured and cannot be 'changeme' or the default placeholder")

        if self.ADS_PROVIDER.lower() == "mock":
            self.ADS_PROVIDER = "amazon"

        if self.ENVIRONMENT.lower() == "production":
            if len(self.JWT_SECRET_KEY) < 32:
                raise ValueError("JWT_SECRET_KEY must be at least 32 characters in production")

            origins = [o.strip() for o in self.FRONTEND_URL.split(",") if o.strip()]
            for origin in origins:
                if not origin.startswith("https://"):
                    raise ValueError(f"FRONTEND_URL origin '{origin}' must start with 'https://' in production")

            missing = []
            if not self.FRONTEND_BASE_URL:
                missing.append("FRONTEND_BASE_URL")
            elif not self.FRONTEND_BASE_URL.startswith("https://") or "localhost" in self.FRONTEND_BASE_URL:
                raise ValueError("FRONTEND_BASE_URL must start with 'https://' in production (never localhost)")

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
                    f"Fast-fail startup error: production mode requires: {', '.join(missing)}"
                )

        # Ensure AMAZON_ADS_API_BASE_URL has a value if accessed directly
        if not self.AMAZON_ADS_API_BASE_URL:
            region = self.AMAZON_ADS_REGION.lower()
            endpoints = AMAZON_REGIONAL_ENDPOINTS.get(region, AMAZON_REGIONAL_ENDPOINTS["eu"])
            self.AMAZON_ADS_API_BASE_URL = endpoints["ads_api_base_url"]

        return self

settings = Settings()
