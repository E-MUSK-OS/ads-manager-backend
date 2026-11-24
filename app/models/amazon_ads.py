from sqlalchemy import (
    Column,
    String,
    Integer,
    ForeignKey,
    Boolean,
    DateTime,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.database import Base


class AmazonOAuthState(Base):
    """
    Stores one-time CSRF state hashes for Amazon Ads (LWA) OAuth flow.
    The raw state exists only in the browser redirect URL; only its SHA-256 hash is stored.
    """
    __tablename__ = "amazon_oauth_states"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    region = Column(String, nullable=False, default="eu")
    state_hash = Column(String(64), unique=True, index=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", backref="amazon_oauth_states")


class AmazonConnection(Base):
    """
    Stores Amazon Ads API LWA credentials per user and region.
    Tokens are stored encrypted using Fernet (app.core.crypto).
    """
    __tablename__ = "amazon_connections"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    region = Column(String, nullable=False, default="eu")
    refresh_token_encrypted = Column(Text, nullable=True)
    status = Column(String, nullable=False, default="CONNECTED")  # CONNECTED, NOT_CONNECTED, PENDING_PROFILES, REAUTH_REQUIRED
    connected_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_error = Column(Text, nullable=True)
    access_token_expires_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "region", name="uq_amazon_connection_user_region"),
        Index("ix_amazon_connections_user_region", "user_id", "region"),
    )

    user = relationship("User", backref="amazon_connections")
    profiles = relationship("AmazonProfile", back_populates="connection", cascade="all, delete-orphan")


class AmazonProfile(Base):
    """
    Stores Amazon Advertising profiles associated with an AmazonConnection.
    Users select which profile to connect to AdsManager.
    """
    __tablename__ = "amazon_profiles"

    id = Column(Integer, primary_key=True, index=True)
    connection_id = Column(Integer, ForeignKey("amazon_connections.id", ondelete="CASCADE"), nullable=False, index=True)
    profile_id = Column(String, index=True, nullable=False)
    account_name = Column(String, nullable=False)
    country_code = Column(String, nullable=False)
    currency_code = Column(String, nullable=False)
    timezone = Column(String, nullable=False)
    is_selected = Column(Boolean, nullable=False, default=False)

    __table_args__ = (
        UniqueConstraint("connection_id", "profile_id", name="uq_amazon_profile_connection_profile"),
        Index("ix_amazon_profiles_conn_profile", "connection_id", "profile_id"),
    )

    connection = relationship("AmazonConnection", back_populates="profiles")
