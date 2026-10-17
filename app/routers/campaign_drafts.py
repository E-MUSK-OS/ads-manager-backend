from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel, Field
from typing import Optional, Any
import json

from app.database import get_db
from app.dependencies import get_current_user, assert_owned_account
from app.models.user import User
from app.models.campaign_draft import CampaignDraft

router = APIRouter(prefix="/campaign-drafts", tags=["campaign-drafts"])

class CampaignDraftCreate(BaseModel):
    ads_account_id: int
    name: str = Field(min_length=1, max_length=128)
    step: int = Field(default=1, ge=1, le=5)
    payload: dict = Field(default_factory=dict)

class CampaignDraftUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    step: Optional[int] = Field(None, ge=1, le=5)
    payload: Optional[dict] = None

@router.get("")
async def list_drafts(
    ads_account_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    await assert_owned_account(db, ads_account_id, current_user)
    result = await db.execute(
        select(CampaignDraft)
        .where(CampaignDraft.user_id == current_user.id, CampaignDraft.ads_account_id == ads_account_id)
        .order_by(CampaignDraft.updated_at.desc())
    )
    return result.scalars().all()

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_draft(
    data: CampaignDraftCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    await assert_owned_account(db, data.ads_account_id, current_user)
    
    # Payload size limit 200 KB
    serialized = json.dumps(data.payload).encode("utf-8")
    if len(serialized) > 200 * 1024:
        raise HTTPException(status_code=413, detail="Draft payload exceeds 200 KB limit")

    draft = CampaignDraft(
        user_id=current_user.id,
        ads_account_id=data.ads_account_id,
        name=data.name,
        step=data.step,
        payload=data.payload
    )
    db.add(draft)
    await db.commit()
    await db.refresh(draft)
    return draft

@router.put("/{id}")
async def update_draft(
    id: int,
    data: CampaignDraftUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(CampaignDraft).where(CampaignDraft.id == id, CampaignDraft.user_id == current_user.id)
    )
    draft = result.scalars().first()
    if not draft:
        raise HTTPException(status_code=404, detail="Campaign draft not found")

    if data.payload is not None:
        serialized = json.dumps(data.payload).encode("utf-8")
        if len(serialized) > 200 * 1024:
            raise HTTPException(status_code=413, detail="Draft payload exceeds 200 KB limit")
        draft.payload = data.payload

    if data.name is not None:
        draft.name = data.name

    if data.step is not None:
        draft.step = data.step

    draft.updated_at = func.now()
    await db.commit()
    await db.refresh(draft)
    return draft

@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_draft(
    id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(CampaignDraft).where(CampaignDraft.id == id, CampaignDraft.user_id == current_user.id)
    )
    draft = result.scalars().first()
    if not draft:
        raise HTTPException(status_code=404, detail="Campaign draft not found")

    await db.delete(draft)
    await db.commit()
    return None
