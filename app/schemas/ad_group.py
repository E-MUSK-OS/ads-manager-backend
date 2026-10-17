from pydantic import BaseModel, ConfigDict
from typing import Optional, Literal

class AdGroupBase(BaseModel):
    name: str
    state: str = "ENABLED"
    default_bid: float = 1.0

class AdGroupCreate(AdGroupBase):
    campaign_id: int
    external_id: Optional[str] = None

class AdGroupPatch(BaseModel):
    name: Optional[str] = None
    default_bid: Optional[float] = None
    state: Optional[Literal["ENABLED", "PAUSED", "ARCHIVED"]] = None

class AdGroupResponse(AdGroupBase):
    id: int
    campaign_id: int
    external_id: Optional[str] = None
    
    model_config = ConfigDict(from_attributes=True)
