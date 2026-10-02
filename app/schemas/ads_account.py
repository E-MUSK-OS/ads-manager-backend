from pydantic import BaseModel, ConfigDict
from typing import Optional
from datetime import datetime

class AdsAccountBase(BaseModel):
    marketplace: str
    is_mock: bool = False

class AdsAccountCreate(AdsAccountBase):
    pass

class AdsAccountResponse(AdsAccountBase):
    id: int
    user_id: int
    created_at: datetime
    
    model_config = ConfigDict(from_attributes=True)
