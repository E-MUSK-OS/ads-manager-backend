from sqlalchemy import Column, String, Integer, ForeignKey, DateTime, Text, Date
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.database import Base

class SyncRun(Base):
    __tablename__ = "sync_runs"

    id = Column(Integer, primary_key=True, index=True)
    ads_account_id = Column(Integer, ForeignKey("ads_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String, default="running", server_default="running", nullable=False) # running, completed, failed
    sync_type = Column(String, default="full", server_default="full", nullable=False) # full, campaigns, metrics
    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    campaigns_count = Column(Integer, default=0, server_default="0", nullable=False)
    metrics_rows_count = Column(Integer, default=0, server_default="0", nullable=False)
    error_message = Column(Text, nullable=True)
    date_range_start = Column(Date, nullable=True)
    date_range_end = Column(Date, nullable=True)

    account = relationship("AdsAccount", backref="sync_runs")
