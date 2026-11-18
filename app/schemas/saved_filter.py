from datetime import datetime
from typing import Any, Dict
from pydantic import BaseModel, Field, ConfigDict


class SavedFilterCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    filters: Dict[str, Any]


class SavedFilterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    filters: Dict[str, Any]
    created_at: datetime
