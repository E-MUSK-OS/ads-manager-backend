from sqlalchemy import Column, Integer, Float, Date, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import relationship
from app.database import Base


class CampaignHourlyMetric(Base):
    __tablename__ = "campaign_hourly_metrics"

    id = Column(Integer, primary_key=True, index=True)
    ads_account_id = Column(Integer, ForeignKey("ads_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)
    hour = Column(Integer, nullable=False)  # 0 to 23 in Asia/Kolkata IST
    impressions = Column(Integer, default=0, nullable=False)
    clicks = Column(Integer, default=0, nullable=False)
    cost = Column(Float, default=0.0, nullable=False)  # Spend in INR
    purchases = Column(Integer, default=0, nullable=False)  # Orders
    sales = Column(Float, default=0.0, nullable=False)  # Attributed sales in INR
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("ads_account_id", "campaign_id", "date", "hour", name="uq_campaign_hourly_metric"),
    )

    ads_account = relationship("AdsAccount")
    campaign = relationship("Campaign")
