from pydantic import BaseModel, ConfigDict
from typing import Optional, Literal
from app.schemas.campaign import MatchType

class KeywordBase(BaseModel):
    keyword_text: str
    match_type: MatchType = "BROAD"
    state: Literal["ENABLED", "PAUSED", "ARCHIVED"] = "ENABLED"
    bid: Optional[float] = None

class KeywordCreate(BaseModel):
    ad_group_id: int
    keyword_text: str
    match_type: MatchType = "BROAD"
    bid: Optional[float] = None

class KeywordPatch(BaseModel):
    bid: Optional[float] = None
    state: Optional[Literal["ENABLED", "PAUSED", "ARCHIVED"]] = None

class KeywordResponse(BaseModel):
    id: int
    ad_group_id: int
    keyword_text: str
    match_type: str
    state: str
    bid: Optional[float] = None
    external_id: Optional[str] = None
    
    model_config = ConfigDict(from_attributes=True)
