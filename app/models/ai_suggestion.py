from sqlalchemy import Column, String, Integer, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from app.database import Base

class AiSuggestion(Base):
    __tablename__ = "ai_suggestions"

    id = Column(Integer, primary_key=True, index=True)
    ads_account_id = Column(Integer, ForeignKey("ads_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String, nullable=False)
    description = Column(String, nullable=False)
    action_type = Column(String, nullable=False)
    entity_id = Column(Integer)
    status = Column(String, default="PENDING") # PENDING, APPROVED, DISMISSED

    account = relationship("AdsAccount")
