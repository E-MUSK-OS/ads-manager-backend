from sqlalchemy import Column, Integer, String, Date, Boolean, ForeignKey, Text, CheckConstraint
from app.database import Base

class ProductReview(Base):
    __tablename__ = "product_reviews"
    __table_args__ = (
        CheckConstraint("rating >= 1 AND rating <= 5", name="check_rating_range"),
    )

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    rating = Column(Integer, nullable=False)
    review_text = Column(Text, nullable=True)
    review_date = Column(Date, nullable=False, index=True)
    verified_purchase = Column(Boolean, default=True)
