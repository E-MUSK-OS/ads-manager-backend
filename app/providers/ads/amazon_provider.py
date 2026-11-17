from typing import Optional, Any
from datetime import datetime, timezone, timedelta
import asyncio
import logging
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException

from app.providers.ads.base import AdsProvider
from app.models.ads_account import AdsAccount
from app.config import settings
from app.core.crypto import decrypt, encrypt

logger = logging.getLogger(__name__)

class AmazonAdsProvider(AdsProvider):
    # In-memory access token cache: account_id -> (access_token, expires_at)
    _access_token_cache: dict[int, tuple[str, datetime]] = {}

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_access_token(self, account: AdsAccount) -> str:
        """
        Decrypt the stored refresh token, exchange for an access token if expired,
        and update connection status on authorization errors.
        """
        if not account.refresh_token_encrypted:
            raise ValueError(f"Account {account.id} has no encrypted refresh token configured.")

        now = datetime.now(timezone.utc)

        # Check in-memory cache with a 5-minute safety buffer
        cached = self._access_token_cache.get(account.id)
        if cached:
            cached_token, cached_expires_at = cached
            if cached_expires_at > now + timedelta(minutes=5):
                return cached_token

        # Check database expiration
        if account.access_token_expires_at and account.access_token_expires_at > now + timedelta(minutes=5) and cached:
            return cached[0]

        # Decrypt refresh token
        try:
            plain_refresh_token = decrypt(account.refresh_token_encrypted)
        except Exception as e:
            logger.error("Failed to decrypt refresh token for ads account %s", account.id)
            raise ValueError("Failed to decrypt stored Amazon refresh token") from e

        # Exchange refresh token for new access token at LWA token URL
        token_url = settings.lwa_token_url
        data = {
            "grant_type": "refresh_token",
            "refresh_token": plain_refresh_token,
            "client_id": settings.AMAZON_LWA_CLIENT_ID,
            "client_secret": settings.AMAZON_LWA_CLIENT_SECRET,
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    token_url,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

                if resp.status_code in (400, 401):
                    resp_json = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                    error_type = resp_json.get("error", "")
                    if error_type in ("invalid_grant", "unauthorized_client") or "invalid_grant" in resp.text:
                        account.connection_status = "reauth_required"
                        await self.db.commit()
                        logger.warning("LWA refresh failed for account %s: marked as reauth_required", account.id)
                        raise HTTPException(
                            status_code=401,
                            detail="Amazon authorization has expired or been revoked. Please reconnect your account."
                        )
                
                resp.raise_for_status()
                token_data = resp.json()

        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Network or HTTP error during LWA token refresh for account %s: %s", account.id, exc)
            raise HTTPException(
                status_code=502,
                detail="Failed to refresh Amazon access token due to an upstream service error"
            ) from exc

        access_token = token_data["access_token"]
        expires_in = token_data.get("expires_in", 3600)
        new_expires_at = now + timedelta(seconds=expires_in)

        # Update account in DB
        account.access_token_expires_at = new_expires_at
        account.connection_status = "connected"

        # Rotate refresh token if a new one was returned
        if "refresh_token" in token_data and token_data["refresh_token"]:
            account.refresh_token_encrypted = encrypt(token_data["refresh_token"])

        self._access_token_cache[account.id] = (access_token, new_expires_at)
        await self.db.commit()

        return access_token

    async def request_api(
        self,
        account: AdsAccount,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        json: Optional[Any] = None,
        max_retries: int = 3,
        timeout: float = 30.0,
    ) -> httpx.Response:
        """
        Shared helper to execute authenticated requests to Amazon Advertising API
        with profile scope, client headers, exponential backoff for 429/5xx, and safe logging.
        """
        access_token = await self.get_access_token(account)
        base_url = settings.ads_api_base_url.rstrip("/")
        url = f"{base_url}/{path.lstrip('/')}"

        headers = {
            "Amazon-Advertising-API-ClientId": settings.AMAZON_LWA_CLIENT_ID or "",
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        if account.profile_id:
            headers["Amazon-Advertising-API-Scope"] = str(account.profile_id)

        # Retry loop with exponential backoff for 429 (Rate Limit) and 5xx (Server Error)
        last_response: Optional[httpx.Response] = None
        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(max_retries):
                try:
                    resp = await client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        params=params,
                        json=json,
                    )
                    last_response = resp

                    if resp.status_code in (429, 500, 502, 503, 504):
                        if attempt < max_retries - 1:
                            delay = 0.5 * (2 ** attempt)
                            logger.warning(
                                "Amazon Ads API returned %s on %s %s. Retrying in %.1fs (attempt %d/%d)",
                                resp.status_code, method, path, delay, attempt + 1, max_retries
                            )
                            await asyncio.sleep(delay)
                            continue

                    return resp

                except (httpx.ConnectError, httpx.TimeoutException) as exc:
                    if attempt < max_retries - 1:
                        delay = 0.5 * (2 ** attempt)
                        logger.warning(
                            "Network exception calling %s %s: %s. Retrying in %.1fs",
                            method, path, exc, delay
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise HTTPException(
                        status_code=504,
                        detail="Upstream Amazon Ads API timeout"
                    ) from exc

        if last_response is not None:
            return last_response
        raise HTTPException(status_code=502, detail="Failed to communicate with Amazon Ads API")

    async def generate_for_new_connection(self, ads_account_id: int):
        # TODO(verify against official Amazon Ads API docs: initial sync and profile hydration)
        raise NotImplementedError(
            "Amazon Ads API generate_for_new_connection is not implemented yet. "
            "Profile data is populated during the LWA OAuth callback. Full sync is deferred."
        )

    async def sync_account_data(self, ads_account_id: int):
        # TODO(verify against official Amazon Ads API docs: sync /sp/campaigns and /reporting/reports)
        raise NotImplementedError(
            "Amazon Ads API sync_account_data is not implemented yet. "
            "Sponsored Products campaigns and reporting synchronization will be implemented once API access is approved."
        )

    def search_brands(self, q: str = "") -> list[str]:
        # TODO(verify against official Amazon Ads API docs: negative brand search endpoint /sp/brands)
        return []
