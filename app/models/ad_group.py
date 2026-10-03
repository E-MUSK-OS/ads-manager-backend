from sqlalchemy import Column, String, Integer, ForeignKey, Float
from sqlalchemy.orm import relationship
from app.database import Base

class AdGroup(Base):
    __tablename__ = "ad_groups"

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    external_id = Column(String, index=True)
    name = Column(String, nullable=False)
    state = Column(String, default="ENABLED")
    default_bid = Column(Float, default=0.75)

    campaign = relationship("Campaign", backref="ad_groups")
