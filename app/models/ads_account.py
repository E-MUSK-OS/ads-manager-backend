from sqlalchemy import Column, String, Integer, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from app.database import Base

class AdsAccount(Base):
    __tablename__ = "ads_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    profile_id = Column(String, unique=True, index=True)
    marketplace = Column(String)
    is_active = Column(Boolean, default=True)

    user = relationship("User", backref="ads_accounts")
