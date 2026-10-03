from sqlalchemy import Column, String, Integer, ForeignKey, Float
from sqlalchemy.orm import relationship
from app.database import Base

class Keyword(Base):
    __tablename__ = "keywords"

    id = Column(Integer, primary_key=True, index=True)
    ad_group_id = Column(Integer, ForeignKey("ad_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    external_id = Column(String, index=True)
    keyword_text = Column(String, nullable=False)
    match_type = Column(String, nullable=False) # EXACT, PHRASE, BROAD
    state = Column(String, default="ENABLED")
    bid = Column(Float, nullable=True) # Override default bid

    ad_group = relationship("AdGroup", backref="keywords")
