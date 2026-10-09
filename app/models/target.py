from sqlalchemy import Column, Integer, String, ForeignKey, Float, CheckConstraint
from app.database import Base

class Target(Base):
    __tablename__ = "targets"
    __table_args__ = (
        CheckConstraint("kind IN ('AUTO_GROUP', 'ASIN', 'CATEGORY', 'NEGATIVE_ASIN')", name="ck_target_kind"),
        CheckConstraint("state IN ('ENABLED', 'PAUSED', 'ARCHIVED')", name="ck_target_state"),
        CheckConstraint("NOT (kind = 'AUTO_GROUP' AND value NOT IN ('CLOSE_MATCH', 'LOOSE_MATCH', 'SUBSTITUTES', 'COMPLEMENTS'))", name="ck_target_auto_value"),
    )

    id = Column(Integer, primary_key=True, index=True)
    ad_group_id = Column(Integer, ForeignKey("ad_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String, nullable=False)
    value = Column(String, nullable=False)
    bid = Column(Float, nullable=True)
    state = Column(String, nullable=False, default="ENABLED")
