from sqlalchemy import Column, String, Integer, ForeignKey, DateTime, func
from sqlalchemy.orm import relationship
from app.database import Base


class RecommendationRun(Base):
    __tablename__ = "recommendation_runs"

    id = Column(Integer, primary_key=True, index=True)
    ads_account_id = Column(Integer, ForeignKey("ads_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    trigger = Column(String, nullable=False)  # manual | sync | rule_saved
    rules_evaluated = Column(Integer, nullable=False, default=0, server_default="0")
    created = Column(Integer, nullable=False, default=0, server_default="0")
    expired = Column(Integer, nullable=False, default=0, server_default="0")
    skipped = Column(Integer, nullable=False, default=0, server_default="0")
    status = Column(String, nullable=False, default="SUCCESS", server_default="SUCCESS")

    ads_account = relationship("AdsAccount")
