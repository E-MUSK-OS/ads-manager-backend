from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, func
from sqlalchemy.orm import relationship
from app.database import Base


class HourlyStreamStatus(Base):
    __tablename__ = "hourly_stream_status"

    id = Column(Integer, primary_key=True, index=True)
    ads_account_id = Column(Integer, ForeignKey("ads_accounts.id", ondelete="CASCADE"), unique=True, nullable=False, index=True)
    status = Column(String, default="NOT_CONNECTED", nullable=False)  # NOT_CONNECTED, PENDING_CONFIRMATION, ACTIVE, ERROR, PAUSED
    sp_traffic_subscription_id = Column(String, nullable=True)
    sp_conversion_subscription_id = Column(String, nullable=True)
    destination_arn = Column(String, nullable=True)
    subscribed_at = Column(DateTime(timezone=True), nullable=True)
    last_message_at = Column(DateTime(timezone=True), nullable=True)
    message_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    ads_account = relationship("AdsAccount")
