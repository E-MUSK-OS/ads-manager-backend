from sqlalchemy import Column, String, Integer, ForeignKey, Float, Date, DateTime, CheckConstraint, UniqueConstraint, Index, JSON, func
from sqlalchemy.orm import relationship
from app.database import Base

class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        UniqueConstraint("ads_account_id", "external_id", name="uix_campaign_external_id"),
        Index("ix_campaigns_account_state", "ads_account_id", "state"),
        CheckConstraint("bidding_strategy IN ('DOWN_ONLY', 'UP_AND_DOWN', 'FIXED', 'LEGACY_FOR_SALES', 'AUTO_FOR_SALES', 'MANUAL')", name="ck_campaign_bidding_strategy"),
        CheckConstraint("placement_top_pct >= 0 AND placement_top_pct <= 900", name="ck_campaign_placement_top"),
        CheckConstraint("placement_rest_pct >= 0 AND placement_rest_pct <= 900", name="ck_campaign_placement_rest"),
        CheckConstraint("placement_product_page_pct >= 0 AND placement_product_page_pct <= 900", name="ck_campaign_placement_product_page"),
        CheckConstraint("state IN ('ENABLED', 'PAUSED', 'ARCHIVED')", name="ck_campaign_state"),
        CheckConstraint("delivery_status IN ('DELIVERING', 'OUT_OF_BUDGET', 'PAUSED', 'ARCHIVED', 'ENDED')", name="ck_campaign_delivery_status"),
        CheckConstraint("site IN ('IN', 'AMAZON_AND_BEYOND', 'AMAZON_BUSINESS')", name="ck_campaign_site"),
    )

    id = Column(Integer, primary_key=True, index=True)
    ads_account_id = Column(Integer, ForeignKey("ads_accounts.id", ondelete="CASCADE"), nullable=False)
    amazon_campaign_id = Column(String, index=True, nullable=True)
    external_id = Column(String, index=True, nullable=True)
    name = Column(String, nullable=False)
    state = Column(String, default="ENABLED", nullable=False)
    delivery_status = Column(String, default="DELIVERING", server_default="DELIVERING", nullable=False)
    daily_budget = Column(Float, default=100.0, nullable=False)
    targeting_type = Column(String, default="MANUAL", nullable=False)
    campaign_type = Column(String, default="SPONSORED_PRODUCTS", nullable=False)
    site = Column(String, nullable=False, server_default="IN", default="IN")
    portfolio_name = Column(String, nullable=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True)

    start_date = Column(Date, nullable=False, server_default=func.current_date())
    end_date = Column(Date, nullable=True)
    bidding_strategy = Column(String, nullable=False, server_default="DOWN_ONLY", default="DOWN_ONLY")
    
    # New placement fields
    placement_top_of_search_pct = Column(Integer, nullable=False, server_default="0", default=0)
    placement_product_pages_pct = Column(Integer, nullable=False, server_default="0", default=0)
    placement_rest_of_search_pct = Column(Integer, nullable=False, server_default="0", default=0)

    # Legacy placement fields for backward compatibility
    placement_top_pct = Column(Integer, nullable=False, server_default="0", default=0)
    placement_rest_pct = Column(Integer, nullable=False, server_default="0", default=0)
    placement_product_page_pct = Column(Integer, nullable=False, server_default="0", default=0)

    negative_brands = Column(JSON, default=list, server_default="[]", nullable=False)
    target_acos = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    account = relationship("AdsAccount", backref="campaigns")
    product = relationship("Product", backref="campaigns")
    rule_campaigns = relationship("RecommendationRuleCampaign", cascade="all, delete-orphan", back_populates="campaign")
