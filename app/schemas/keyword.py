from pydantic import BaseModel, ConfigDict
from typing import Optional

class KeywordBase(BaseModel):
    keyword_text: str
    match_type: str = "BROAD"
    state: str = "ENABLED"
    bid: Optional[float] = None

class KeywordCreate(KeywordBase):
    ad_group_id: int
    external_id: Optional[str] = None

class KeywordResponse(KeywordBase):
    id: int
    ad_group_id: int
    external_id: Optional[str]
    
    model_config = ConfigDict(from_attributes=True)
