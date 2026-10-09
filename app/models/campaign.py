from sqlalchemy import Column, String, Integer, ForeignKey, Float, Date, DateTime, CheckConstraint, UniqueConstraint, Index, func
from sqlalchemy.orm import relationship
from app.database import Base

class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        UniqueConstraint("ads_account_id", "external_id", name="uix_campaign_external_id"),
        Index("ix_campaigns_account_state", "ads_account_id", "state"),
        CheckConstraint("bidding_strategy IN ('LEGACY_FOR_SALES', 'AUTO_FOR_SALES', 'MANUAL')", name="ck_campaign_bidding_strategy"),
        CheckConstraint("placement_top_pct >= 0 AND placement_top_pct <= 900", name="ck_campaign_placement_top"),
        CheckConstraint("placement_rest_pct >= 0 AND placement_rest_pct <= 900", name="ck_campaign_placement_rest"),
        CheckConstraint("placement_product_page_pct >= 0 AND placement_product_page_pct <= 900", name="ck_campaign_placement_product_page"),
        CheckConstraint("state IN ('ENABLED', 'PAUSED', 'ARCHIVED')", name="ck_campaign_state")
    )

    id = Column(Integer, primary_key=True, index=True)
    ads_account_id = Column(Integer, ForeignKey("ads_accounts.id", ondelete="CASCADE"), nullable=False)
    external_id = Column(String, index=True)
    name = Column(String, nullable=False)
    state = Column(String, default="ENABLED")
    daily_budget = Column(Float, default=10.0)
    targeting_type = Column(String, default="MANUAL")
    campaign_type = Column(String, default="SPONSORED_PRODUCTS")
    product_id = Column(Integer, ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True)

    start_date = Column(Date, nullable=False, server_default=func.current_date())
    end_date = Column(Date, nullable=True)
    bidding_strategy = Column(String, nullable=False, server_default="AUTO_FOR_SALES")
    placement_top_pct = Column(Integer, nullable=False, server_default="0")
    placement_rest_pct = Column(Integer, nullable=False, server_default="0")
    placement_product_page_pct = Column(Integer, nullable=False, server_default="0")
    target_acos = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    account = relationship("AdsAccount", backref="campaigns")
    product = relationship("Product", backref="campaigns")

