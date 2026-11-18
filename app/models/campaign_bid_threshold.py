from sqlalchemy import Column, Integer, Float, ForeignKey, DateTime, func, CheckConstraint
from sqlalchemy.orm import relationship
from app.database import Base


class CampaignBidThreshold(Base):
    __tablename__ = "campaign_bid_thresholds"
    __table_args__ = (
        CheckConstraint("min_bid >= 0", name="ck_bid_threshold_min_bid"),
        CheckConstraint("max_bid >= 0", name="ck_bid_threshold_max_bid"),
        CheckConstraint("min_bid <= max_bid OR min_bid IS NULL OR max_bid IS NULL", name="ck_bid_threshold_min_le_max"),
        CheckConstraint("target_acos >= 0 AND target_acos <= 100", name="ck_bid_threshold_target_acos"),
    )

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), unique=True, nullable=False, index=True)
    min_bid = Column(Float, nullable=True)
    max_bid = Column(Float, nullable=True)
    target_acos = Column(Float, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    updated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    campaign = relationship("Campaign", backref="bid_threshold")
    user = relationship("User")
