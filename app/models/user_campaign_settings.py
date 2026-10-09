from sqlalchemy import Column, Integer, ForeignKey, Float
from app.database import Base

class UserCampaignSettings(Base):
    __tablename__ = "user_campaign_settings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    acos_low = Column(Float, default=20.0, nullable=False)
    acos_high = Column(Float, default=90.0, nullable=False)
    default_target_acos = Column(Float, default=30.0, nullable=False)
    bid_limit_acos = Column(Float, nullable=True)
