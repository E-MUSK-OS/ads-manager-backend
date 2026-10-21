from sqlalchemy import Column, String, Integer, ForeignKey, DateTime, JSON, CheckConstraint, Index, func
from sqlalchemy.orm import relationship
from app.database import Base


class Recommendation(Base):
    __tablename__ = "recommendations"

    id = Column(Integer, primary_key=True, index=True)
    ads_account_id = Column(Integer, ForeignKey("ads_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    rule_id = Column(Integer, ForeignKey("recommendation_rules.id", ondelete="SET NULL"), nullable=True)
    rule_name = Column(String, nullable=False)
    source = Column(String, nullable=False, default="RULE", server_default="RULE")
    affected_parameter = Column(String, nullable=False)
    entity_type = Column(String, nullable=False)  # campaign | target | search_term | placement
    entity_id = Column(Integer, nullable=True)
    entity_label = Column(String, nullable=False)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    ad_group_id = Column(Integer, ForeignKey("ad_groups.id", ondelete="CASCADE"), nullable=True)
    evidence = Column(JSON, nullable=False)
    suggested_actions = Column(JSON, nullable=False)
    status = Column(String, nullable=False, default="OPEN", server_default="OPEN")
    dedupe_key = Column(String, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolved_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    apply_result = Column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_recommendations_account_status", "ads_account_id", "status"),
        Index("ix_recommendations_account_dedupe", "ads_account_id", "dedupe_key"),
        CheckConstraint(
            "status IN ('OPEN', 'APPLIED', 'DISMISSED', 'EXPIRED', 'FAILED')",
            name="ck_recommendations_status",
        ),
    )

    ads_account = relationship("AdsAccount")
    rule = relationship("RecommendationRule")
    campaign = relationship("Campaign")
    ad_group = relationship("AdGroup")
    resolver = relationship("User")
