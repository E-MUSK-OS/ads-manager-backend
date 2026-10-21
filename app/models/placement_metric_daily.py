from sqlalchemy import Column, Integer, ForeignKey, Float, Date, String, UniqueConstraint, CheckConstraint, Index
from sqlalchemy.orm import relationship
from app.database import Base

class PlacementMetricDaily(Base):
    __tablename__ = "placement_metrics_daily"
    __table_args__ = (
        CheckConstraint(
            "placement IN ('TOP_OF_SEARCH', 'REST_OF_SEARCH', 'PRODUCT_PAGES')",
            name="ck_placement_metrics_daily_placement",
        ),
        UniqueConstraint("campaign_id", "placement", "date", name="uix_placement_metrics_daily"),
        Index("ix_placement_metrics_daily_campaign_date", "campaign_id", "date"),
    )

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    placement = Column(String, nullable=False)  # TOP_OF_SEARCH, REST_OF_SEARCH, PRODUCT_PAGES
    date = Column(Date, nullable=False, index=True)

    impressions = Column(Integer, default=0, nullable=False)
    clicks = Column(Integer, default=0, nullable=False)
    spend = Column(Float, default=0.0, nullable=False)
    sales = Column(Float, default=0.0, nullable=False)
    orders = Column(Integer, default=0, nullable=False)

    campaign = relationship("Campaign", backref="placement_metrics")
