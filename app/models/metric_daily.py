from sqlalchemy import Column, Integer, ForeignKey, Float, Date, String, UniqueConstraint, CheckConstraint
from app.database import Base

class MetricDaily(Base):
    __tablename__ = "metrics_daily"
    __table_args__ = (
        CheckConstraint("entity_type IN ('campaign', 'ad_group', 'keyword')", name="check_entity_type"),
        UniqueConstraint("entity_type", "entity_id", "date", name="uix_metric_daily")
    )

    id = Column(Integer, primary_key=True, index=True)
    entity_type = Column(String, nullable=False) # CAMPAIGN, AD_GROUP, KEYWORD, SEARCH_TERM
    entity_id = Column(Integer, nullable=False, index=True) # ID of the entity in our DB
    date = Column(Date, nullable=False, index=True)
    
    impressions = Column(Integer, default=0)
    clicks = Column(Integer, default=0)
    spend = Column(Float, default=0.0)
    sales = Column(Float, default=0.0)
    orders = Column(Integer, default=0)
