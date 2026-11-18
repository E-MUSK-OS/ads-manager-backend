from sqlalchemy import Column, String, Integer, ForeignKey, Boolean, DateTime, Text
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.database import Base

class AdsAccount(Base):
    __tablename__ = "ads_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    profile_id = Column(String, unique=True, index=True, nullable=True)
    marketplace = Column(String, default="IN", server_default="IN")
    is_active = Column(Boolean, default=True)
    is_mock = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Amazon Ads API / LWA integration fields
    refresh_token_encrypted = Column(Text, nullable=True)
    access_token_expires_at = Column(DateTime(timezone=True), nullable=True)
    region = Column(String, nullable=True)
    amazon_account_name = Column(String, nullable=True)
    last_synced_at = Column(DateTime(timezone=True), nullable=True)
    connection_status = Column(String, default="connected", server_default="connected", nullable=False)
    sync_status = Column(String, default="idle", server_default="idle", nullable=False)

    user = relationship("User", backref="ads_accounts")
