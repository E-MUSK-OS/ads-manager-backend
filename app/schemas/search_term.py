from pydantic import BaseModel, ConfigDict
from datetime import date

class SearchTermBase(BaseModel):
    search_term: str
    campaign_id: int
    ad_group_id: int
    keyword_id: int
    clicks: int = 0
    impressions: int = 0
    spend: float = 0.0
    sales: float = 0.0

class SearchTermResponse(SearchTermBase):
    id: int
    date: date
    
    model_config = ConfigDict(from_attributes=True)
