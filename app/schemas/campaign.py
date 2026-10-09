from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, Literal
from datetime import date
from app.core.marketplace_rules import KEYWORD_MAX_LEN, KEYWORD_MAX_WORDS, KEYWORD_FORBIDDEN

BiddingStrategy = Literal["LEGACY_FOR_SALES", "AUTO_FOR_SALES", "MANUAL"]
MatchType = Literal["EXACT", "PHRASE", "BROAD"]
NegMatchType = Literal["NEGATIVE_EXACT", "NEGATIVE_PHRASE"]

def clean_keyword_text(v: str) -> str:
    v = " ".join(v.strip().lower().split())
    if not v: raise ValueError("Keyword is empty")
    if len(v) > KEYWORD_MAX_LEN: raise ValueError(f"Max {KEYWORD_MAX_LEN} characters")
    if len(v.split()) > KEYWORD_MAX_WORDS: raise ValueError(f"Max {KEYWORD_MAX_WORDS} words")
    if any(ch in KEYWORD_FORBIDDEN for ch in v): raise ValueError("Contains unsupported characters")
    return v

class KeywordIn(BaseModel):
    keyword_text: str
    match_type: MatchType = "BROAD"
    bid: float | None = None
    
    @field_validator("keyword_text")
    def validate_keyword(cls, v): return clean_keyword_text(v)

class NegativeKeywordIn(BaseModel):
    keyword_text: str
    match_type: NegMatchType = "NEGATIVE_EXACT"
    
    @field_validator("keyword_text")
    def validate_keyword(cls, v): return clean_keyword_text(v)

class TargetIn(BaseModel):
    kind: Literal["AUTO_GROUP","ASIN","CATEGORY"]
    value: str
    bid: float | None = None

class AdGroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    default_bid: float
    keywords: list[KeywordIn] = []
    targets: list[TargetIn] = []

class CampaignCreate(BaseModel):
    ads_account_id: int
    name: str = Field(min_length=1, max_length=128)
    campaign_type: Literal["SPONSORED_PRODUCTS"] = "SPONSORED_PRODUCTS"
    targeting_type: Literal["AUTO", "MANUAL"]
    state: Literal["ENABLED", "PAUSED"] = "ENABLED"
    start_date: date
    end_date: date | None = None
    daily_budget: float
    target_acos: float | None = Field(None, gt=0, le=1000)
    bidding_strategy: BiddingStrategy = "AUTO_FOR_SALES"
    placement_top_pct: int = Field(0, ge=0, le=900)
    placement_rest_pct: int = Field(0, ge=0, le=900)
    placement_product_page_pct: int = Field(0, ge=0, le=900)
    product_ids: list[int] = Field(min_length=1)
    ad_groups: list[AdGroupIn] = Field(min_length=1, max_length=1)
    negative_keywords: list[NegativeKeywordIn] = []
    negative_asins: list[str] = []

    @model_validator(mode='after')
    def validate_dates(self):
        if self.end_date and self.start_date > self.end_date:
            raise ValueError("end_date must be >= start_date")
        return self

class CampaignUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    daily_budget: float | None = None
    end_date: date | None = None
    target_acos: float | None = Field(None, gt=0, le=1000)
    bidding_strategy: BiddingStrategy | None = None
    placement_top_pct: int | None = Field(None, ge=0, le=900)
    placement_rest_pct: int | None = Field(None, ge=0, le=900)
    placement_product_page_pct: int | None = Field(None, ge=0, le=900)

class StateUpdate(BaseModel):
    state: Literal["ENABLED", "PAUSED", "ARCHIVED"]

class BulkActionRequest(BaseModel):
    ids: list[int]
    action: Literal["ENABLE", "PAUSE", "ARCHIVE", "SET_BUDGET", "CHANGE_BUDGET_PCT"]
    value: float | None = None

class CampaignResponse(BaseModel):
    id: int
    ads_account_id: int
    name: str
    state: str
    targeting_type: str
    campaign_type: str
    start_date: date
    end_date: date | None
    daily_budget: float
    target_acos: float | None
    bidding_strategy: str
    placement_top_pct: int
    placement_rest_pct: int
    placement_product_page_pct: int
    
    class Config:
        from_attributes = True

class UserCampaignSettingsResponse(BaseModel):
    acos_low: float
    acos_high: float
    default_target_acos: float
    bid_limit_acos: float | None

class UserCampaignSettingsUpdate(BaseModel):
    acos_low: float
    acos_high: float
    default_target_acos: float
    bid_limit_acos: float | None = None
