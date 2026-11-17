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
    profile_id: Optional[str] = None
    region: Optional[str] = None
    amazon_account_name: Optional[str] = None
    last_synced_at: Optional[datetime] = None
    connection_status: str = "connected"
    is_active: bool = True
    created_at: datetime
    
    # Note: Token fields (refresh_token_encrypted, access_token) are strictly excluded
    model_config = ConfigDict(from_attributes=True)

class LWAAuthorizeUrlResponse(BaseModel):
    url: str

class LWACallbackRequest(BaseModel):
    code: str
    state: str
