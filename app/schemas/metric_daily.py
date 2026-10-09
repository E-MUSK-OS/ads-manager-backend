from pydantic import BaseModel, ConfigDict
from typing import Optional
from datetime import date

class MetricDailyResponse(BaseModel):
    id: int
    date: date
    entity_type: str
    entity_id: int
    impressions: int
    clicks: int
    spend: float
    sales: float
    orders: int
    organic_sales: float

    
    model_config = ConfigDict(from_attributes=True)
