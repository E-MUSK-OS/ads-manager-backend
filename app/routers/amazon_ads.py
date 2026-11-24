"""Amazon Ads (Login with Amazon, LWA) OAuth Connection Router for NETU TECHNO AdsManager.

Follows official Amazon Ads API documentation:
- Authorization Grant: https://advertising.amazon.com/API/docs/en-us/guides/get-started/create-authorization-grant
- Retrieve Access Token: https://advertising.amazon.com/API/docs/en-us/guides/get-started/retrieve-access-token
- Profiles API Reference: https://advertising.amazon.com/API/docs/en-us/reference/2/profiles
"""

from typing import Optional, Any
from datetime import datetime, timezone, timedelta
import secrets
import hashlib
import logging
import urllib.parse
import httpx

from fastapi import APIRouter, Depends, Query, Body, HTTPException, status
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from pydantic import BaseModel

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.models.ads_account import AdsAccount
from app.models.amazon_ads import AmazonOAuthState, AmazonConnection, AmazonProfile
from app.config import settings
from app.core.crypto import encrypt, decrypt

logger = logging.getLogger(__name__)

router = APIRouter(tags=["amazon-ads"])


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class AmazonAdsException(HTTPException):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.message = message


class TokenExchangeError(Exception):
    pass


# ---------------------------------------------------------------------------
# Request / Response Schemas
# ---------------------------------------------------------------------------

class ConnectRequest(BaseModel):
    region: Optional[str] = "EU"


class SelectAccountRequest(BaseModel):
    profileId: str


class DisconnectRequest(BaseModel):
    profileId: Optional[str] = None


# ---------------------------------------------------------------------------
# Amazon Ads Helpers
# ---------------------------------------------------------------------------

async def exchange_code(code: str) -> dict[str, Any]:
    """
    Exchange authorization code for LWA access and refresh tokens at the EU regional token URL:
    https://api.amazon.co.uk/auth/o2/token
    """
    token_url = settings.lwa_token_url
    client_id = settings.AMAZON_LWA_CLIENT_ID or ""
    client_secret = settings.AMAZON_LWA_CLIENT_SECRET or ""
    redirect_uri = settings.AMAZON_LWA_REDIRECT_URI or ""

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as c:
        r = await c.post(
            token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if r.status_code != 200:
        err = "unknown"
        try:
            if r.headers.get("content-type", "").startswith("application/json"):
                err = r.json().get("error", "unknown")
        except Exception:
            pass
        # Log only status and Amazon's error field; NEVER the body, NEVER the code, NEVER secrets
        logger.error("Token exchange failed with status %s, error=%s", r.status_code, err)
        raise TokenExchangeError(err)

    body = r.json()
    return {
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "expires_in": int(body.get("expires_in", 3600)),
    }


async def refresh_connection_token(conn: AmazonConnection, db: AsyncSession) -> str:
    """
    Refresh LWA access token for an existing AmazonConnection.
    Treats invalid_grant as REAUTH_REQUIRED per contract A5.
    """
    if not conn.refresh_token_encrypted:
        raise AmazonAdsException(
            status_code=409,
            code="NOT_CONNECTED",
            message="No refresh token configured",
        )

    try:
        plain_refresh_token = decrypt(conn.refresh_token_encrypted)
    except Exception as exc:
        logger.error("Failed to decrypt stored Amazon refresh token: %s", type(exc).__name__)
        raise AmazonAdsException(
            status_code=500,
            code="INTERNAL_ERROR",
            message="Failed to decrypt stored credentials",
        ) from exc

    data = {
        "grant_type": "refresh_token",
        "refresh_token": plain_refresh_token,
        "client_id": settings.AMAZON_LWA_CLIENT_ID or "",
        "client_secret": settings.AMAZON_LWA_CLIENT_SECRET or "",
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as c:
        r = await c.post(
            settings.lwa_token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if r.status_code != 200:
        err = "unknown"
        try:
            if r.headers.get("content-type", "").startswith("application/json"):
                err = r.json().get("error", "unknown")
        except Exception:
            pass

        if err == "invalid_grant" or r.status_code == 400:
            conn.status = "REAUTH_REQUIRED"
            conn.last_error = "Authorization expired or revoked by Amazon"
            await db.commit()
            raise AmazonAdsException(
                status_code=409,
                code="REAUTH_REQUIRED",
                message="Amazon authorization expired. Please reconnect.",
            )
        raise AmazonAdsException(
            status_code=502,
            code="TOKEN_EXCHANGE_FAILED",
            message="Failed to refresh Amazon access token",
        )

    body = r.json()
    access_token = body["access_token"]
    if "refresh_token" in body and body["refresh_token"]:
        conn.refresh_token_encrypted = encrypt(body["refresh_token"])
    expires_in = body.get("expires_in", 3600)
    conn.access_token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    await db.commit()
    return access_token


async def list_profiles(access_token: str) -> list[dict[str, Any]]:
    """
    Fetch advertiser profiles from Amazon Ads API:
    https://advertising-api-eu.amazon.com/v2/profiles
    """
    url = f"{settings.ads_api_base_url}/v2/profiles"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Amazon-Advertising-API-ClientId": settings.AMAZON_LWA_CLIENT_ID or "",
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as c:
        r = await c.get(url, headers=headers)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        return data
    return []


# ---------------------------------------------------------------------------
# Endpoints (Section A4)
# ---------------------------------------------------------------------------

@router.post("/connect")
async def connect_amazon_ads(
    body: Optional[ConnectRequest] = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    A4.1 POST /amazon-ads/connect
    Creates a one-time CSRF state hash, stores it, and returns the Amazon LWA authorization URL.
    """
    req = body or ConnectRequest()
    region_str = (req.region or "EU").strip().upper()

    # Reject non-EU regions per A3 / A4.7 / A9-Q6
    if region_str != "EU":
        raise AmazonAdsException(
            status_code=400,
            code="VALIDATION_ERROR",
            message="Only EU region is supported for India",
        )

    # Lazy cleanup of states older than 24 hours (A7 rule: only state table deletion allowed)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    await db.execute(delete(AmazonOAuthState).where(AmazonOAuthState.created_at < cutoff))

    # Generate 32-byte urlsafe state and hash it with SHA-256
    raw_state = secrets.token_urlsafe(32)
    state_hash = hashlib.sha256(raw_state.encode("utf-8")).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.AMAZON_OAUTH_STATE_TTL_MINUTES)

    oauth_state = AmazonOAuthState(
        user_id=current_user.id,
        region="eu",
        state_hash=state_hash,
        expires_at=expires_at,
    )
    db.add(oauth_state)
    await db.commit()

    # Log at most first 6 characters of the SHA-256 state hash (A1.1)
    logger.info("Created Amazon OAuth state hash %s... for user_id=%s", state_hash[:6], current_user.id)

    # Build Amazon LWA authorization URL (https://eu.account.amazon.com/ap/oa)
    params = {
        "client_id": settings.AMAZON_LWA_CLIENT_ID or "",
        "scope": settings.AMAZON_LWA_SCOPE,
        "response_type": "code",
        "redirect_uri": settings.AMAZON_LWA_REDIRECT_URI or "",
        "state": raw_state,
    }
    authorization_url = f"{settings.lwa_authorize_url}?{urllib.parse.urlencode(params)}"

    return {
        "success": True,
        "message": "Authorization URL generated successfully",
        "data": {
            "authorizationUrl": authorization_url,
            "region": "EU",
        },
    }


@router.get("/callback")
async def amazon_ads_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    scope: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    A4.2 GET /amazon-ads/callback
    Public endpoint called by Amazon LWA via the user's browser.
    Answers strictly with HTTP 302 redirect to the frontend.
    """
    frontend_base = settings.frontend_base_url

    # Step 1: Check for denial / error parameter from Amazon
    if error:
        logger.warning("Amazon OAuth consent error received: %s (description: %s)", error, error_description)
        if state:
            state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
            res = await db.execute(select(AmazonOAuthState).where(AmazonOAuthState.state_hash == state_hash))
            oauth_state = res.scalars().first()
            if oauth_state and not oauth_state.used_at:
                oauth_state.used_at = datetime.now(timezone.utc)
                await db.commit()
        return RedirectResponse(
            url=f"{frontend_base}/connect-amazon?status=error&reason=access_denied",
            status_code=302,
        )

    # Step 2: Missing code or state
    if not code or not state:
        logger.warning("Amazon OAuth callback received missing code or state parameters")
        return RedirectResponse(
            url=f"{frontend_base}/connect-amazon?status=error&reason=invalid_request",
            status_code=302,
        )

    # Step 3: Look up state by SHA-256 hash
    state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
    state_hash_prefix = state_hash[:6]
    res = await db.execute(select(AmazonOAuthState).where(AmazonOAuthState.state_hash == state_hash))
    oauth_state = res.scalars().first()

    now = datetime.now(timezone.utc)
    expires_at_utc = ensure_utc(oauth_state.expires_at) if oauth_state else None
    if not oauth_state or oauth_state.used_at is not None or (expires_at_utc and expires_at_utc < now):
        logger.warning(
            "Amazon OAuth state invalid, expired, or reused: hash_prefix=%s (found=%s, used=%s)",
            state_hash_prefix,
            oauth_state is not None,
            oauth_state.used_at is not None if oauth_state else None,
        )
        return RedirectResponse(
            url=f"{frontend_base}/connect-amazon?status=error&reason=invalid_state",
            status_code=302,
        )

    # Step 4: Mark state used BEFORE token exchange (one-time use, even if subsequent steps fail)
    oauth_state.used_at = now
    await db.commit()
    logger.info("Marked OAuth state used: hash_prefix=%s for user_id=%s", state_hash_prefix, oauth_state.user_id)

    # Step 5: Exchange code at token endpoint
    try:
        token_data = await exchange_code(code)
    except Exception as exc:
        logger.error(
            "OAuth token exchange failed for state hash_prefix=%s: %s",
            state_hash_prefix,
            type(exc).__name__,
        )
        return RedirectResponse(
            url=f"{frontend_base}/connect-amazon?status=error&reason=token_exchange_failed",
            status_code=302,
        )

    # Step 6: Encrypt and store refresh token for user and region
    refresh_token = token_data.get("refresh_token")
    access_token = token_data.get("access_token", "")
    expires_in = token_data.get("expires_in", 3600)
    encrypted_refresh_token = encrypt(refresh_token) if refresh_token else None

    res = await db.execute(
        select(AmazonConnection).where(
            AmazonConnection.user_id == oauth_state.user_id,
            AmazonConnection.region == oauth_state.region,
        )
    )
    conn = res.scalars().first()
    if not conn:
        conn = AmazonConnection(
            user_id=oauth_state.user_id,
            region=oauth_state.region,
            refresh_token_encrypted=encrypted_refresh_token,
            status="CONNECTED",
            connected_at=now,
            last_error=None,
            access_token_expires_at=now + timedelta(seconds=expires_in),
        )
        db.add(conn)
    else:
        conn.refresh_token_encrypted = encrypted_refresh_token
        conn.status = "CONNECTED"
        conn.connected_at = now
        conn.last_error = None
        conn.access_token_expires_at = now + timedelta(seconds=expires_in)
    await db.commit()
    await db.refresh(conn)

    # Step 7: Call GET /v2/profiles and store advertiser profiles
    try:
        profiles_list = await list_profiles(access_token)
    except Exception as exc:
        logger.error(
            "Failed to fetch Amazon profiles during OAuth callback for conn_id=%s: %s",
            conn.id,
            type(exc).__name__,
        )
        conn.status = "PENDING_PROFILES"
        conn.last_error = "Failed to fetch advertiser profiles"
        await db.commit()
        return RedirectResponse(
            url=f"{frontend_base}/connect-amazon?status=success&needs_profile_refresh=1",
            status_code=302,
        )

    # Upsert profiles
    for p in profiles_list:
        p_id = str(p.get("profileId"))
        acc_info = p.get("accountInfo") or {}
        acc_name = acc_info.get("name") or p.get("accountName") or p.get("name") or f"Profile {p_id}"
        country = p.get("countryCode") or "IN"
        currency = p.get("currencyCode") or "INR"
        tz = p.get("timezone") or "Asia/Kolkata"

        res = await db.execute(
            select(AmazonProfile).where(
                AmazonProfile.connection_id == conn.id,
                AmazonProfile.profile_id == p_id,
            )
        )
        prof_obj = res.scalars().first()
        if prof_obj:
            prof_obj.account_name = acc_name
            prof_obj.country_code = country
            prof_obj.currency_code = currency
            prof_obj.timezone = tz
        else:
            prof_obj = AmazonProfile(
                connection_id=conn.id,
                profile_id=p_id,
                account_name=acc_name,
                country_code=country,
                currency_code=currency,
                timezone=tz,
                is_selected=False,
            )
            db.add(prof_obj)
    await db.commit()

    # Step 8: Redirect to success page
    return RedirectResponse(
        url=f"{frontend_base}/connect-amazon?status=success",
        status_code=302,
    )


@router.get("/accounts")
async def list_amazon_accounts(
    refresh: bool = Query(False),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    A4.3 GET /amazon-ads/accounts
    Lists user's Amazon advertiser profiles.
    Only profiles whose countryCode is 'IN' are marked selectable.
    """
    res = await db.execute(
        select(AmazonConnection).where(
            AmazonConnection.user_id == current_user.id,
            AmazonConnection.region == "eu",
        )
    )
    conn = res.scalars().first()
    if not conn or conn.status == "NOT_CONNECTED" or not conn.refresh_token_encrypted:
        raise AmazonAdsException(
            status_code=409,
            code="NOT_CONNECTED",
            message="No connected Amazon Ads account found",
        )

    # Refresh profiles from Amazon if requested
    if refresh:
        access_token = await refresh_connection_token(conn, db)
        try:
            profiles_list = await list_profiles(access_token)
            for p in profiles_list:
                p_id = str(p.get("profileId"))
                acc_info = p.get("accountInfo") or {}
                acc_name = acc_info.get("name") or p.get("accountName") or p.get("name") or f"Profile {p_id}"
                country = p.get("countryCode") or "IN"
                currency = p.get("currencyCode") or "INR"
                tz = p.get("timezone") or "Asia/Kolkata"

                res = await db.execute(
                    select(AmazonProfile).where(
                        AmazonProfile.connection_id == conn.id,
                        AmazonProfile.profile_id == p_id,
                    )
                )
                prof_obj = res.scalars().first()
                if prof_obj:
                    prof_obj.account_name = acc_name
                    prof_obj.country_code = country
                    prof_obj.currency_code = currency
                    prof_obj.timezone = tz
                else:
                    prof_obj = AmazonProfile(
                        connection_id=conn.id,
                        profile_id=p_id,
                        account_name=acc_name,
                        country_code=country,
                        currency_code=currency,
                        timezone=tz,
                        is_selected=False,
                    )
                    db.add(prof_obj)
            if conn.status == "PENDING_PROFILES":
                conn.status = "CONNECTED"
                conn.last_error = None
            await db.commit()
        except AmazonAdsException:
            raise
        except Exception as exc:
            logger.error("Failed to refresh advertiser profiles from Amazon: %s", type(exc).__name__)
            raise AmazonAdsException(
                status_code=502,
                code="AMAZON_API_ERROR",
                message="Failed to refresh advertiser profiles from Amazon",
            )

    res = await db.execute(
        select(AmazonProfile).where(AmazonProfile.connection_id == conn.id)
    )
    profiles = res.scalars().all()

    profiles_data = [
        {
            "profileId": p.profile_id,
            "accountName": p.account_name,
            "countryCode": p.country_code,
            "currencyCode": p.currency_code,
            "timezone": p.timezone,
            "marketplace": p.country_code,
            "isSelected": p.is_selected,
            "isSelectable": (p.country_code.upper() == "IN"),
        }
        for p in profiles
    ]

    msg = (
        "Profiles retrieved successfully"
        if profiles_data
        else "No advertiser profiles found yet. Amazon profiles can take a few minutes to appear after linking."
    )

    return {
        "success": True,
        "message": msg,
        "data": {
            "profiles": profiles_data,
            "count": len(profiles_data),
        },
    }


@router.post("/accounts/select")
async def select_amazon_account(
    req: SelectAccountRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    A4.4 POST /amazon-ads/accounts/select
    Verifies profile ownership under the user's connection, validates India marketplace,
    and updates or creates the active AdsAccount.
    """
    if not req.profileId or not str(req.profileId).strip():
        raise AmazonAdsException(
            status_code=400,
            code="VALIDATION_ERROR",
            message="profileId is required",
        )

    profile_id_str = str(req.profileId).strip()

    # Verify user connection exists
    res = await db.execute(
        select(AmazonConnection).where(
            AmazonConnection.user_id == current_user.id,
            AmazonConnection.status != "NOT_CONNECTED",
        )
    )
    conn = res.scalars().first()
    if not conn or not conn.refresh_token_encrypted:
        raise AmazonAdsException(
            status_code=409,
            code="NOT_CONNECTED",
            message="No connected Amazon Ads account found",
        )

    # Check profile ownership
    res = await db.execute(
        select(AmazonProfile).where(AmazonProfile.profile_id == profile_id_str)
    )
    matching_profiles = res.scalars().all()

    profile = next((p for p in matching_profiles if p.connection_id == conn.id), None)
    if not profile:
        if matching_profiles:
            # Profile exists in system but belongs to another user's connection
            raise AmazonAdsException(
                status_code=403,
                code="PROFILE_NOT_OWNED",
                message="You do not have access to this profile",
            )
        raise AmazonAdsException(
            status_code=400,
            code="VALIDATION_ERROR",
            message="Profile not found in your connection",
        )

    # Enforce India only per A4.3 / A9-Q3
    if profile.country_code.upper() != "IN":
        raise AmazonAdsException(
            status_code=400,
            code="VALIDATION_ERROR",
            message="Only India (IN) profiles can be selected",
        )

    # Check if this profile is already linked to another user in ads_accounts
    res = await db.execute(
        select(AdsAccount).where(
            AdsAccount.profile_id == profile.profile_id,
            AdsAccount.user_id != current_user.id,
        )
    )
    if res.scalars().first():
        raise AmazonAdsException(
            status_code=409,
            code="PROFILE_ALREADY_CONNECTED",
            message="This Amazon Ads profile is already connected to another user",
        )

    # Update selection status on connection profiles
    res = await db.execute(
        select(AmazonProfile).where(AmazonProfile.connection_id == conn.id)
    )
    for p in res.scalars().all():
        p.is_selected = (p.id == profile.id)

    # Upsert into AdsAccount so the rest of the application functions smoothly
    res = await db.execute(
        select(AdsAccount).where(
            AdsAccount.user_id == current_user.id,
            AdsAccount.profile_id == profile.profile_id,
        )
    )
    ads_acc = res.scalars().first()
    now = datetime.now(timezone.utc)

    if not ads_acc:
        ads_acc = AdsAccount(
            user_id=current_user.id,
            profile_id=profile.profile_id,
            amazon_account_name=profile.account_name,
            marketplace=profile.country_code,
            region="eu",
            connection_status="connected",
            refresh_token_encrypted=conn.refresh_token_encrypted,
            access_token_expires_at=conn.access_token_expires_at,
            last_synced_at=now,
            is_mock=False,
            is_active=True,
        )
        db.add(ads_acc)
    else:
        ads_acc.amazon_account_name = profile.account_name
        ads_acc.marketplace = profile.country_code
        ads_acc.region = "eu"
        ads_acc.connection_status = "connected"
        ads_acc.refresh_token_encrypted = conn.refresh_token_encrypted
        ads_acc.access_token_expires_at = conn.access_token_expires_at
        ads_acc.last_synced_at = now
        ads_acc.is_active = True

    await db.commit()

    return {
        "success": True,
        "message": "Profile selected successfully",
        "data": {
            "profileId": profile.profile_id,
            "accountName": profile.account_name,
            "countryCode": profile.country_code,
            "currencyCode": profile.currency_code,
            "timezone": profile.timezone,
            "isSelected": True,
        },
    }


@router.get("/status")
async def get_amazon_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    A4.5 GET /amazon-ads/status
    Returns connection status and selected profile metadata. Never returns any token strings.
    """
    res = await db.execute(
        select(AmazonConnection).where(AmazonConnection.user_id == current_user.id)
    )
    conn = res.scalars().first()
    if not conn or conn.status == "NOT_CONNECTED" or not conn.refresh_token_encrypted:
        return {
            "success": True,
            "message": "No active Amazon Ads connection",
            "data": {
                "connected": False,
                "connectionStatus": "NOT_CONNECTED",
                "amazonAccountName": None,
                "selectedProfileId": None,
                "region": "EU",
                "connectedAt": None,
                "tokenExpiresAt": None,
            },
        }

    res = await db.execute(
        select(AmazonProfile).where(
            AmazonProfile.connection_id == conn.id,
            AmazonProfile.is_selected == True,
        )
    )
    selected_profile = res.scalars().first()

    return {
        "success": True,
        "message": "Connection status retrieved successfully",
        "data": {
            "connected": conn.status == "CONNECTED",
            "connectionStatus": conn.status,
            "amazonAccountName": selected_profile.account_name if selected_profile else None,
            "selectedProfileId": selected_profile.profile_id if selected_profile else None,
            "region": conn.region.upper(),
            "connectedAt": conn.connected_at.isoformat() if conn.connected_at else None,
            "tokenExpiresAt": conn.access_token_expires_at.isoformat() if conn.access_token_expires_at else None,
        },
    }


@router.post("/disconnect")
async def disconnect_amazon_ads(
    body: Optional[DisconnectRequest] = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    A4.6 POST /amazon-ads/disconnect
    Disconnects profile or entire connection without deleting DB history rows.
    Removes encrypted token locally (Amazon LWA does not offer a public token revocation endpoint).
    """
    req = body or DisconnectRequest()

    res = await db.execute(
        select(AmazonConnection).where(AmazonConnection.user_id == current_user.id)
    )
    conn = res.scalars().first()
    if not conn or conn.status == "NOT_CONNECTED":
        return {
            "success": True,
            "message": "Already disconnected",
            "data": {
                "connected": False,
                "connectionStatus": "NOT_CONNECTED",
            },
        }

    logger.info(
        "Disconnecting Amazon Ads connection for user_id=%s (profileId=%s)",
        current_user.id,
        req.profileId,
    )

    if req.profileId:
        res = await db.execute(
            select(AmazonProfile).where(
                AmazonProfile.connection_id == conn.id,
                AmazonProfile.profile_id == req.profileId,
            )
        )
        profile = res.scalars().first()
        if profile:
            profile.is_selected = False

        res = await db.execute(
            select(AdsAccount).where(
                AdsAccount.user_id == current_user.id,
                AdsAccount.profile_id == req.profileId,
            )
        )
        acc = res.scalars().first()
        if acc:
            acc.connection_status = "disconnected"
            acc.refresh_token_encrypted = None
            acc.is_active = False

        # If no selected profiles remain, mark connection NOT_CONNECTED
        res = await db.execute(
            select(AmazonProfile).where(
                AmazonProfile.connection_id == conn.id,
                AmazonProfile.is_selected == True,
            )
        )
        if not res.scalars().first():
            conn.status = "NOT_CONNECTED"
            conn.refresh_token_encrypted = None
            conn.access_token_expires_at = None
    else:
        conn.status = "NOT_CONNECTED"
        conn.refresh_token_encrypted = None
        conn.access_token_expires_at = None

        res = await db.execute(
            select(AmazonProfile).where(AmazonProfile.connection_id == conn.id)
        )
        for p in res.scalars().all():
            p.is_selected = False

        res = await db.execute(
            select(AdsAccount).where(AdsAccount.user_id == current_user.id)
        )
        for acc in res.scalars().all():
            acc.connection_status = "disconnected"
            acc.refresh_token_encrypted = None
            acc.is_active = False

    await db.commit()

    return {
        "success": True,
        "message": "Disconnected successfully",
        "data": {
            "connected": False,
            "connectionStatus": "NOT_CONNECTED",
        },
    }
