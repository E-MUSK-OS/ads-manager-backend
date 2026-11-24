from typing import Optional
from datetime import datetime, timezone, timedelta
import secrets
import logging
import urllib.parse
import httpx
from jose import jwt, JWTError

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.database import get_db
from app.dependencies import get_current_user, assert_owned_account
from app.models.user import User
from app.models.ads_account import AdsAccount
from app.models.sync_run import SyncRun
from app.schemas.ads_account import (
    AdsAccountResponse,
    LWAAuthorizeUrlResponse,
    LWACallbackRequest,
)
from app.providers.ads.factory import get_ads_provider
from app.core.marketplace_rules import MARKETPLACE_RULES, marketplace_or_in
from app.config import settings
from app.core.crypto import encrypt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ads-accounts", tags=["ads-accounts"])

@router.get("/provider-mode")
async def get_provider_mode():
    """
    Return the ads provider configuration.
    """
    return {
        "provider": "amazon",
        "region": settings.AMAZON_ADS_REGION.lower(),
    }


@router.get("", response_model=list[AdsAccountResponse])
async def list_accounts(
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    stmt = select(AdsAccount).where(AdsAccount.user_id == current_user.id)
    if not include_inactive:
        stmt = stmt.where(
            AdsAccount.is_active == True,
            AdsAccount.connection_status == "connected",
        )
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/{account_id}/rules")
async def get_account_rules(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    account = await assert_owned_account(db, account_id, current_user)
    return marketplace_or_in(account.marketplace)


@router.post("/{account_id}/sync")
async def sync_account(
    account_id: int,
    force: bool = False,
    days_back: int = 30,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    account = await assert_owned_account(db, account_id, current_user)
    if not account.is_active or account.connection_status != "connected":
        raise HTTPException(
            status_code=400,
            detail="Cannot sync an inactive or disconnected ads account.",
        )
    provider = get_ads_provider(db, account)
    try:
        res = await provider.sync_account_data(account.id, days_back=days_back, force=force)
    except NotImplementedError:
        raise HTTPException(
            status_code=501,
            detail="Live Amazon sync is not implemented yet.",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Sync error: {str(exc)}"
        )

    try:
        from app.recommendations.engine import run_engine
        await run_engine(db, account, trigger="sync")
    except Exception as e:
        logger.warning("Failed to run recommendations on sync: %s", e)

    return res


@router.get("/{account_id}/sync-status")
async def get_sync_status(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    account = await assert_owned_account(db, account_id, current_user)
    
    recent_run_res = await db.execute(
        select(SyncRun)
        .where(SyncRun.ads_account_id == account_id)
        .order_by(SyncRun.started_at.desc())
        .limit(1)
    )
    last_run = recent_run_res.scalars().first()

    return {
        "account_id": account.id,
        "sync_status": account.sync_status,
        "connection_status": account.connection_status,
        "last_synced_at": account.last_synced_at,
        "last_run": {
            "id": last_run.id,
            "status": last_run.status,
            "sync_type": last_run.sync_type,
            "started_at": last_run.started_at,
            "completed_at": last_run.completed_at,
            "campaigns_count": last_run.campaigns_count,
            "metrics_rows_count": last_run.metrics_rows_count,
            "error_message": last_run.error_message,
        } if last_run else None
    }


@router.delete("/{account_id}")
async def disconnect_account(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Disconnect an ads account and revoke stored credentials while preserving historical reporting.
    """
    account = await assert_owned_account(db, account_id, current_user)
    account.refresh_token_encrypted = None
    account.access_token_expires_at = None
    account.connection_status = "disconnected"
    account.is_active = False
    await db.commit()
    return {"status": "ok", "message": f"Account {account_id} disconnected and stored tokens deleted"}


# =========================================================================
# Login with Amazon (LWA) OAuth 2.0 Endpoints (DEPRECATED)
# Note: These legacy routes (/ads-accounts/amazon/*) are deprecated in favor of
# /amazon-ads/connect and /amazon-ads/callback (Section A4 contract).
# Retained for existing accounts until full migration is confirmed.
# =========================================================================

@router.get("/amazon/authorize-url", response_model=LWAAuthorizeUrlResponse, deprecated=True)
async def get_amazon_authorize_url(
    current_user: User = Depends(get_current_user)
):
    """
    [DEPRECATED] Generate the LWA authorization URL with a signed, short-lived CSRF state JWT.
    New integrations should use POST /amazon-ads/connect.
    """
    if not settings.AMAZON_LWA_CLIENT_ID or not settings.AMAZON_LWA_REDIRECT_URI:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Amazon LWA client ID and redirect URI must be configured"
        )

    # State JWT contains user ID, random nonce, and 10-minute expiry
    state_payload = {
        "user_id": current_user.id,
        "nonce": secrets.token_hex(16),
        "purpose": "lwa_oauth",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
    }
    state = jwt.encode(state_payload, settings.JWT_SECRET_KEY, algorithm="HS256")

    # TODO(verify against official Amazon Ads API docs: verify query parameter names and scopes)
    params = {
        "client_id": settings.AMAZON_LWA_CLIENT_ID,
        "scope": settings.AMAZON_LWA_SCOPE,
        "response_type": "code",
        "redirect_uri": settings.AMAZON_LWA_REDIRECT_URI,
        "state": state,
    }
    authorize_url = f"{settings.lwa_authorize_url}?{urllib.parse.urlencode(params)}"
    return LWAAuthorizeUrlResponse(url=authorize_url)


@router.post("/amazon/callback", response_model=list[AdsAccountResponse], deprecated=True)
async def handle_amazon_callback(
    payload: LWACallbackRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    [DEPRECATED] Handle legacy LWA authorization-code callback via frontend POST.
    New flow uses the public browser GET /amazon-ads/callback directly.
    1. Validate signed state JWT and user ownership.
    2. Exchange authorization code for LWA access & refresh tokens.
    3. Encrypt and persist the refresh token.
    4. Fetch available Amazon Advertising profiles (/v2/profiles) and create/update AdsAccounts.
    """
    # 1. Verify CSRF state token
    try:
        decoded_state = jwt.decode(payload.state, settings.JWT_SECRET_KEY, algorithms=["HS256"])
        if decoded_state.get("purpose") != "lwa_oauth":
            raise HTTPException(status_code=400, detail="Invalid OAuth state purpose")
        if decoded_state.get("user_id") != current_user.id:
            raise HTTPException(status_code=403, detail="OAuth state does not match authenticated user")
    except JWTError as exc:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state parameter") from exc

    if not settings.AMAZON_LWA_CLIENT_ID or not settings.AMAZON_LWA_CLIENT_SECRET or not settings.AMAZON_LWA_REDIRECT_URI:
        raise HTTPException(status_code=500, detail="Amazon LWA credentials not configured")

    # 2. Exchange authorization code for tokens at LWA token URL
    token_url = settings.lwa_token_url
    token_request_body = {
        "grant_type": "authorization_code",
        "code": payload.code,
        "client_id": settings.AMAZON_LWA_CLIENT_ID,
        "client_secret": settings.AMAZON_LWA_CLIENT_SECRET,
        "redirect_uri": settings.AMAZON_LWA_REDIRECT_URI,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            token_resp = await client.post(
                token_url,
                data=token_request_body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if token_resp.status_code != 200:
                logger.error("LWA code exchange failed with status %d: %s", token_resp.status_code, token_resp.text)
                raise HTTPException(status_code=400, detail="Failed to exchange authorization code with Amazon")
            
            token_data = token_resp.json()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Exception connecting to LWA token endpoint: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to reach Amazon OAuth service") from exc

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in", 3600)

    if not refresh_token:
        raise HTTPException(status_code=400, detail="Amazon OAuth did not return a refresh token")

    # 3. Encrypt the refresh token
    encrypted_refresh = encrypt(refresh_token)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    # 4. Fetch profiles from Amazon Advertising API /v2/profiles
    # TODO(verify against official Amazon Ads API docs: verify /v2/profiles response structure and regional endpoints)
    profiles_url = f"{settings.ads_api_base_url.rstrip('/')}/v2/profiles"
    profile_headers = {
        "Amazon-Advertising-API-ClientId": settings.AMAZON_LWA_CLIENT_ID,
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            profiles_resp = await client.get(profiles_url, headers=profile_headers)
            if profiles_resp.status_code != 200:
                logger.error("Amazon /v2/profiles failed (%d): %s", profiles_resp.status_code, profiles_resp.text)
                raise HTTPException(status_code=502, detail="Failed to fetch advertising profiles from Amazon")
            profiles = profiles_resp.json()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Exception connecting to Amazon Ads API profiles: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to reach Amazon Ads API") from exc

    if not isinstance(profiles, list) or len(profiles) == 0:
        raise HTTPException(status_code=400, detail="No active Amazon Advertising profiles found for this account")

    profiles = [
        p for p in profiles
        if (p.get("countryCode") or "").upper() in settings.supported_marketplaces
    ]
    if not profiles:
        raise HTTPException(
            status_code=400,
            detail="No Amazon India (IN) advertising profiles found. AdsManager currently supports Amazon India only.",
        )

    # Pre-check: refuse the whole callback if any profile belongs to another user.
    profile_ids = [str(p.get("profileId")) for p in profiles if p.get("profileId") is not None]
    if not profile_ids:
        raise HTTPException(status_code=400, detail="No active Amazon Advertising profiles found for this account")

    existing_rows = await db.execute(
        select(AdsAccount).where(AdsAccount.profile_id.in_(profile_ids))
    )
    existing_by_profile = {a.profile_id: a for a in existing_rows.scalars().all()}

    for pid, existing_acc in existing_by_profile.items():
        if existing_acc.user_id != current_user.id:
            raise HTTPException(
                status_code=409,
                detail="This Amazon Ads profile is already connected to another AdsManager account.",
            )

    connected_accounts: list[AdsAccount] = []
    now = datetime.now(timezone.utc)

    for p in profiles:
        if p.get("profileId") is None or not p.get("countryCode"):
            continue
        profile_id = str(p["profileId"])
        country_code = str(p["countryCode"]).upper()
        account_info = p.get("accountInfo") or {}
        account_name = account_info.get("name") or f"Amazon Ads {country_code} ({profile_id})"

        acc = existing_by_profile.get(profile_id)
        if acc:
            # same owner: refresh credentials (do NOT touch acc.user_id)
            acc.refresh_token_encrypted = encrypted_refresh
            acc.access_token_expires_at = expires_at
            acc.region = settings.AMAZON_ADS_REGION
            acc.amazon_account_name = account_name
            acc.marketplace = country_code
            acc.is_mock = False
            acc.is_active = True
            acc.connection_status = "connected"
            acc.last_synced_at = now
        else:
            acc = AdsAccount(
                user_id=current_user.id,
                profile_id=profile_id,
                refresh_token_encrypted=encrypted_refresh,
                access_token_expires_at=expires_at,
                region=settings.AMAZON_ADS_REGION,
                amazon_account_name=account_name,
                marketplace=country_code,
                is_mock=False,
                is_active=True,
                connection_status="connected",
                last_synced_at=now,
            )
            db.add(acc)
        connected_accounts.append(acc)

    await db.commit()
    for acc in connected_accounts:
        await db.refresh(acc)

    return connected_accounts
