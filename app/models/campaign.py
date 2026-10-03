from sqlalchemy import Column, String, Integer, ForeignKey, Float, Enum, UniqueConstraint
from sqlalchemy.orm import relationship
import enum
from app.database import Base

class CampaignState(str, enum.Enum):
    ENABLED = "ENABLED"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"

class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        UniqueConstraint("ads_account_id", "external_id", name="uix_campaign_external_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    ads_account_id = Column(Integer, ForeignKey("ads_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    external_id = Column(String, index=True)
    name = Column(String, nullable=False)
    state = Column(String, default=CampaignState.ENABLED.value)
    daily_budget = Column(Float, default=10.0)
    targeting_type = Column(String, default="MANUAL")
    campaign_type = Column(String, default="SPONSORED_PRODUCTS")

    account = relationship("AdsAccount", backref="campaigns")
