from sqlalchemy import Column, String, Integer, ForeignKey, DateTime, func, Text
from sqlalchemy.orm import relationship
from app.database import Base


class CampaignHistory(Base):
    __tablename__ = "campaign_history"

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = Column(String, nullable=False, index=True)  # BUDGET_UPDATE, STATE_CHANGE, PLACEMENT_UPDATE, HARVEST_KEYWORD, NEGATE_KEYWORD, TARGET_UPDATE, RECOMMENDATION_APPLIED
    event_title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    previous_value = Column(String, nullable=True)
    new_value = Column(String, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    campaign = relationship("Campaign")

