from sqlalchemy import Column, Integer, String, ForeignKey, UniqueConstraint
from app.database import Base

class CampaignProduct(Base):
    __tablename__ = "campaign_products"
    __table_args__ = (
        UniqueConstraint("campaign_id", "product_id", name="uix_campaign_product"),
    )

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    state = Column(String, default="ENABLED", nullable=False)
