from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, Literal
from datetime import date
import re
from app.core.marketplace_rules import KEYWORD_MAX_LEN, KEYWORD_MAX_WORDS, KEYWORD_FORBIDDEN, ASIN_RE

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

    @field_validator("value")
    def validate_target_value(cls, v, info):
        kind = info.data.get("kind")
        if kind == "ASIN":
            v = v.strip().upper()
            if not re.match(ASIN_RE, v):
                raise ValueError("ASIN must be 10 alphanumeric characters")
        return v

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
    site: Literal["AMAZON_AND_BEYOND", "AMAZON_BUSINESS"] = "AMAZON_AND_BEYOND"
    placement_top_pct: int = Field(0, ge=0, le=900)
    placement_rest_pct: int = Field(0, ge=0, le=900)
    placement_product_page_pct: int = Field(0, ge=0, le=900)
    product_ids: list[int] = Field(min_length=1)
    ad_groups: list[AdGroupIn] = Field(min_length=1, max_length=1)
    negative_keywords: list[NegativeKeywordIn] = []
    negative_asins: list[str] = []
    negative_brands: list[str] = []

    @field_validator("negative_asins")
    def validate_negative_asins(cls, v):
        cleaned: list[str] = []
        for raw in v:
            a = raw.strip().upper()
            if not re.match(ASIN_RE, a):
                raise ValueError("ASIN must be 10 alphanumeric characters")
            if a not in cleaned:
                cleaned.append(a)
        return cleaned

    @field_validator("negative_brands")
    def validate_negative_brands(cls, v):
        cleaned: list[str] = []
        for raw in v:
            b = " ".join(raw.strip().split())
            if not b or len(b) > 80:
                raise ValueError("Brand name must be 1–80 characters")
            key = b.lower()
            if key not in {c.lower() for c in cleaned}:
                cleaned.append(b)
        return cleaned

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
    site: str = "AMAZON_AND_BEYOND"
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

HEALTH_METRIC_ALLOWLIST = {
    "acos", "roas", "spend", "sales", "orders", "clicks", "impressions",
    "ctr", "cpc", "cvr", "budget_utilization", "age_days", "spend_without_sales"
}

class HealthRuleCondition(BaseModel):
    metric: str
    op: Literal[">", ">=", "<", "<=", "=="]
    value: float | int | Literal["@target_acos"]

    @field_validator("metric")
    def validate_metric(cls, v):
        if v not in HEALTH_METRIC_ALLOWLIST:
            raise ValueError(f"Unknown metric '{v}'. Allowed: {', '.join(sorted(HEALTH_METRIC_ALLOWLIST))}")
        return v

class HealthRule(BaseModel):
    key: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=24)
    color: Literal["red", "amber", "green", "blue", "slate"] = "slate"
    enabled: bool = True
    match: Literal["ALL", "ANY"] = "ALL"
    conditions: list[HealthRuleCondition] = Field(min_length=1, max_length=4)

class UserCampaignSettingsResponse(BaseModel):
    acos_low: float
    acos_high: float
    default_target_acos: float
    bid_limit_acos: float | None = None
    health_rules: list[HealthRule] | None = None

    class Config:
        from_attributes = True

class UserCampaignSettingsUpdate(BaseModel):
    acos_low: float = Field(ge=0, le=1000)
    acos_high: float = Field(ge=0, le=1000)
    default_target_acos: float = Field(gt=0, le=1000)
    bid_limit_acos: float | None = Field(None, gt=0, le=1000)
    health_rules: list[HealthRule] | None = Field(None, max_length=12)

    @model_validator(mode="after")
    def validate_thresholds(self):
        if self.acos_low >= self.acos_high:
            raise ValueError("acos_low must be strictly less than acos_high")
        return self


class BidSuggestionKeywordIn(BaseModel):
    keyword_text: str
    match_type: MatchType = "BROAD"

    @field_validator("keyword_text")
    def validate_keyword(cls, v):
        return clean_keyword_text(v)


class BidSuggestionsRequest(BaseModel):
    ads_account_id: int
    product_ids: list[int] = []
    keywords: list[BidSuggestionKeywordIn] = []


class ForecastRequest(BaseModel):
    ads_account_id: int
    product_ids: list[int] = []
    daily_budget: float | None = None
    default_bid: float | None = None
    targeting_type: Literal["AUTO", "MANUAL"] | None = None
    target_count: int | None = None


class ProductLookupRequest(BaseModel):
    ads_account_id: int
    asins: list[str] = Field(max_length=500)


class CampaignCreateResult(BaseModel):
    id: int
    name: str
    state: str
    status_label: str
    warnings: list = []
