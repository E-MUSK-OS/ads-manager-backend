from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    # App
    ADS_PROVIDER: str = "mock"
    LLM_PROVIDER: str = "claude"
    FRONTEND_URL: str = "http://localhost:3000"

    # Database
    DATABASE_URL: str
    REDIS_URL: Optional[str] = None

    # Auth
    JWT_SECRET_KEY: str
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    GOOGLE_OAUTH_CLIENT_ID: Optional[str] = None

    # Amazon Ads API
    AMAZON_LWA_CLIENT_ID: Optional[str] = None
    AMAZON_LWA_CLIENT_SECRET: Optional[str] = None
    AMAZON_LWA_REDIRECT_URI: Optional[str] = None
    AMAZON_ADS_API_BASE_URL: str = "https://advertising-api.amazon.com"

    # AI
    ANTHROPIC_API_KEY: Optional[str] = None

    # Billing
    STRIPE_SECRET_KEY: Optional[str] = None
    STRIPE_WEBHOOK_SECRET: Optional[str] = None

    class Config:
        env_file = ".env"

settings = Settings()
