from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Index, JSON
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.database import Base


class CampaignDraft(Base):
    __tablename__ = "campaign_drafts"
    __table_args__ = (
        Index("ix_campaign_drafts_user_account", "user_id", "ads_account_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    ads_account_id = Column(Integer, ForeignKey("ads_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String, nullable=False)
    step = Column(Integer, nullable=False, default=1)
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User")
    account = relationship("AdsAccount")
