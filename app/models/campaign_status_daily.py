from sqlalchemy import Column, Integer, String, Date, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import relationship
from app.database import Base


class CampaignStatusDaily(Base):
    __tablename__ = "campaign_status_daily"
    __table_args__ = (
        UniqueConstraint("ads_account_id", "campaign_id", "date", name="uq_campaign_status_daily_account_camp_date"),
    )

    id = Column(Integer, primary_key=True, index=True)
    ads_account_id = Column(Integer, ForeignKey("ads_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)
    state = Column(String(30), nullable=False)
    serving_status = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    account = relationship("AdsAccount", backref="daily_statuses")
    campaign = relationship("Campaign", backref="daily_statuses")
