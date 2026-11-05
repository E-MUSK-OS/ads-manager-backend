from sqlalchemy import Column, Integer, String, ForeignKey, Float, CheckConstraint
from sqlalchemy.orm import relationship
from app.database import Base

class Target(Base):
    __tablename__ = "targets"
    __table_args__ = (
        CheckConstraint("kind IS NULL OR kind IN ('AUTO_GROUP', 'ASIN', 'CATEGORY', 'PRODUCT', 'NEGATIVE_ASIN', 'NEGATIVE_BRAND')", name="ck_target_kind"),
        CheckConstraint("state IN ('ENABLED', 'PAUSED', 'ARCHIVED')", name="ck_target_state"),
    )

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=True, index=True)
    ad_group_id = Column(Integer, ForeignKey("ad_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # Required Phase 1 columns
    target_type = Column(String, nullable=True)  # AUTO_GROUP, PRODUCT, CATEGORY
    expression_type = Column(String, nullable=True)  # CLOSE_MATCH, LOOSE_MATCH, SUBSTITUTES, COMPLEMENTS, ASIN_SAME_AS, CATEGORY_SAME_AS
    expression_value = Column(String, nullable=True)
    
    # Legacy columns for compatibility
    kind = Column(String, nullable=True)
    value = Column(String, nullable=True)
    
    bid = Column(Float, nullable=True)
    state = Column(String, nullable=False, default="ENABLED")

    campaign = relationship("Campaign", backref="targets")
    ad_group = relationship("AdGroup", backref="targets")

    def __init__(self, **kwargs):
        if "target_type" in kwargs and "kind" not in kwargs:
            kwargs["kind"] = kwargs["target_type"]
        elif "kind" in kwargs and "target_type" not in kwargs:
            kwargs["target_type"] = kwargs["kind"]

        if "expression_value" in kwargs and "value" not in kwargs:
            kwargs["value"] = kwargs["expression_value"]
        elif "expression_type" in kwargs and "value" not in kwargs:
            kwargs["value"] = kwargs["expression_type"]
        elif "value" in kwargs:
            if "expression_value" not in kwargs:
                kwargs["expression_value"] = kwargs["value"]
            if "expression_type" not in kwargs and kwargs["value"] in ("CLOSE_MATCH", "LOOSE_MATCH", "SUBSTITUTES", "COMPLEMENTS"):
                kwargs["expression_type"] = kwargs["value"]
        super().__init__(**kwargs)
