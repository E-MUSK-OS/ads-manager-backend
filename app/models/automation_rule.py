from sqlalchemy import Column, String, Integer, ForeignKey, Boolean, Float
from sqlalchemy.orm import relationship
from app.database import Base

class AutomationRule(Base):
    __tablename__ = "automation_rules"

    id = Column(Integer, primary_key=True, index=True)
    ads_account_id = Column(Integer, ForeignKey("ads_accounts.id"), nullable=False)
    name = Column(String, nullable=False)
    metric = Column(String, nullable=False)  # e.g., ACOS, ROAS
    operator = Column(String, nullable=False) # e.g., GREATER_THAN, LESS_THAN
    value = Column(Float, nullable=False)
    action = Column(String, nullable=False)   # e.g., PAUSE, INCREASE_BID
    scope = Column(String, nullable=False)    # e.g., CAMPAIGN, KEYWORD
    is_active = Column(Boolean, default=True)

    account = relationship("AdsAccount")
