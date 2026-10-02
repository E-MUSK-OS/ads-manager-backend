from pydantic import BaseModel, ConfigDict
from typing import Optional

class CampaignBase(BaseModel):
    name: str
    state: str = "ENABLED"
    daily_budget: float = 10.0
    targeting_type: str = "MANUAL"
    campaign_type: str = "SPONSORED_PRODUCTS"

class CampaignCreate(CampaignBase):
    ads_account_id: int
    external_id: Optional[str] = None

class CampaignResponse(CampaignBase):
    id: int
    ads_account_id: int
    external_id: Optional[str]
    
    model_config = ConfigDict(from_attributes=True)
