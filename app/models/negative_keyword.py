from sqlalchemy import Column, Integer, String, ForeignKey, UniqueConstraint, CheckConstraint
from app.database import Base

class NegativeKeyword(Base):
    __tablename__ = "negative_keywords"
    __table_args__ = (
        UniqueConstraint("campaign_id", "ad_group_id", "keyword_text", "match_type", name="uix_negative_keyword"),
        CheckConstraint("match_type IN ('NEGATIVE_EXACT', 'NEGATIVE_PHRASE')", name="ck_negative_keyword_match_type"),
    )

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    ad_group_id = Column(Integer, ForeignKey("ad_groups.id", ondelete="CASCADE"), nullable=True, index=True)
    keyword_text = Column(String, nullable=False)
    match_type = Column(String, nullable=False)
