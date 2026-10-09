from sqlalchemy import Column, Integer, String, Float, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.database import Base

class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("ads_account_id", "asin", name="uix_product_ads_account_asin"),
    )

    id = Column(Integer, primary_key=True, index=True)
    ads_account_id = Column(Integer, ForeignKey("ads_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    asin = Column(String, nullable=False, index=True)
    title = Column(String, nullable=False)
    image_url = Column(String, nullable=True)
    category = Column(String, nullable=True)
    price = Column(Float, nullable=False)
    
    rating_avg = Column(Float, default=0.0)
    review_count = Column(Integer, default=0)

    account = relationship("AdsAccount", backref="products")
    reviews = relationship("ProductReview", backref="product", cascade="all, delete-orphan")
