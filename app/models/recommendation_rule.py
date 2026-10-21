from sqlalchemy import Column, String, Integer, ForeignKey, Boolean, DateTime, JSON, UniqueConstraint, CheckConstraint, Index, func, true
from sqlalchemy.orm import relationship
from app.database import Base


class RecommendationRule(Base):
    __tablename__ = "recommendation_rules"

    id = Column(Integer, primary_key=True, index=True)
    ads_account_id = Column(Integer, ForeignKey("ads_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(60), nullable=False)
    name_key = Column(String(60), nullable=False)
    affected_parameter = Column(String, nullable=False)
    date_filter = Column(String, nullable=False)
    min_clicks = Column(Integer, nullable=False, default=0, server_default="0")
    conditions = Column(JSON, nullable=False)
    actions = Column(JSON, nullable=False)
    scope_mode = Column(String, nullable=False, default="ALL", server_default="ALL")
    is_active = Column(Boolean, nullable=False, default=True, server_default=true())
    template_key = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    last_match_count = Column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        UniqueConstraint("ads_account_id", "name_key", name="uq_recommendation_rules_account_name_key"),
        CheckConstraint("affected_parameter IN ('CAMPAIGN', 'PLACEMENT', 'TARGETING', 'SEARCH_TERM')", name="ck_rec_rules_affected_parameter"),
        CheckConstraint("scope_mode IN ('ALL', 'INCLUDE', 'EXCLUDE')", name="ck_rec_rules_scope_mode"),
    )

    ads_account = relationship("AdsAccount")
    campaigns = relationship("Campaign", secondary="recommendation_rule_campaigns", overlaps="rule_campaigns")
    rule_campaigns = relationship("RecommendationRuleCampaign", cascade="all, delete-orphan", back_populates="rule", overlaps="campaigns")


class RecommendationRuleCampaign(Base):
    __tablename__ = "recommendation_rule_campaigns"

    rule_id = Column(Integer, ForeignKey("recommendation_rules.id", ondelete="CASCADE"), primary_key=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True)

    rule = relationship("RecommendationRule", back_populates="rule_campaigns", overlaps="campaigns")
    campaign = relationship("Campaign", back_populates="rule_campaigns", overlaps="campaigns")
