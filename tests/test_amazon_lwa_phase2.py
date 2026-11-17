import pytest
import urllib.parse
from datetime import datetime, timezone, timedelta
import httpx
from jose import jwt
from fastapi import HTTPException
from sqlalchemy import select

from app.config import Settings, settings
from app.core import crypto
from app.models.ads_account import AdsAccount
from app.models.user import User
from app.providers.ads.factory import get_ads_provider
from app.providers.ads.mock_provider import MockAdsProvider
from app.providers.ads.amazon_provider import AmazonAdsProvider


# ---------------------------------------------------------------------------
# 1. Token Encryption Roundtrip & Validation
# ---------------------------------------------------------------------------

def test_token_encryption_roundtrip():
    key = crypto.generate_key()
    original_secret = "Atzr|IwEBIExampleRefreshToken_Secret1234567890!@#"
    
    # Encrypt and decrypt roundtrip
    ciphertext = crypto.encrypt(original_secret, key=key)
    assert ciphertext != original_secret
    assert len(ciphertext) > 20
    
    decrypted = crypto.decrypt(ciphertext, key=key)
    assert decrypted == original_secret

    # Empty string handling
    assert crypto.encrypt("", key=key) == ""
    assert crypto.decrypt("", key=key) == ""

    # Tampered ciphertext fails
    tampered = ciphertext[:-4] + "XXXX"
    with pytest.raises(Exception):
        crypto.decrypt(tampered, key=key)


def test_token_encryption_missing_key(monkeypatch):
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", None)
    with pytest.raises(ValueError, match="TOKEN_ENCRYPTION_KEY is not configured"):
        crypto.encrypt("some_token")


# ---------------------------------------------------------------------------
# 2. Fast-fail Startup Configuration
# ---------------------------------------------------------------------------

def test_fast_fail_when_amazon_provider_missing_credentials():
    # ADS_PROVIDER=amazon without required credentials must raise ValueError
    with pytest.raises(ValueError, match="Fast-fail startup error"):
        Settings(
            ADS_PROVIDER="amazon",
            DATABASE_URL="sqlite+aiosqlite:///:memory:",
            JWT_SECRET_KEY="test_secret_key_at_least_32_chars_long",
            AMAZON_LWA_CLIENT_ID=None,
        )

    # Insecure JWT_SECRET_KEY fails startup
    with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
        Settings(
            ADS_PROVIDER="mock",
            DATABASE_URL="sqlite+aiosqlite:///:memory:",
            JWT_SECRET_KEY="changeme",
        )


# ---------------------------------------------------------------------------
# 3. Provider Factory Choice
# ---------------------------------------------------------------------------

def test_provider_factory_choice(db_session, account_a):
    # Mock account returns MockAdsProvider
    account_a.is_mock = True
    provider = get_ads_provider(db_session, account_a)
    assert isinstance(provider, MockAdsProvider)

    # When ADS_PROVIDER='mock', even real accounts return MockAdsProvider
    account_a.is_mock = False
    provider = get_ads_provider(db_session, account_a)
    assert isinstance(provider, MockAdsProvider)

    # When ADS_PROVIDER='amazon' and account.is_mock is False, returns AmazonAdsProvider
    orig_provider = settings.ADS_PROVIDER
    try:
        settings.ADS_PROVIDER = "amazon"
        provider = get_ads_provider(db_session, account_a)
        assert isinstance(provider, AmazonAdsProvider)
    finally:
        settings.ADS_PROVIDER = orig_provider


# ---------------------------------------------------------------------------
# 4. LWA OAuth Authorize URL
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_amazon_authorize_url(client_a, user_a, monkeypatch):
    monkeypatch.setattr(settings, "AMAZON_LWA_CLIENT_ID", "amzn1.application-oa2-client.test123")
    monkeypatch.setattr(settings, "AMAZON_LWA_REDIRECT_URI", "http://localhost:3000/connect-amazon/callback")
    monkeypatch.setattr(settings, "AMAZON_LWA_SCOPE", "advertising::campaign_management")

    resp = await client_a.get("/ads-accounts/amazon/authorize-url")
    assert resp.status_code == 200
    data = resp.json()
    assert "url" in data

    parsed_url = urllib.parse.urlparse(data["url"])
    params = urllib.parse.parse_qs(parsed_url.query)

    assert params["client_id"] == ["amzn1.application-oa2-client.test123"]
    assert params["redirect_uri"] == ["http://localhost:3000/connect-amazon/callback"]
    assert params["scope"] == ["advertising::campaign_management"]
    assert params["response_type"] == ["code"]

    # Verify state JWT
    state_jwt = params["state"][0]
    decoded = jwt.decode(state_jwt, settings.JWT_SECRET_KEY, algorithms=["HS256"])
    assert decoded["user_id"] == user_a.id
    assert decoded["purpose"] == "lwa_oauth"
    assert "nonce" in decoded
    assert decoded["exp"] > datetime.now(timezone.utc).timestamp()


# ---------------------------------------------------------------------------
# 5. LWA Callback CSRF State Validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_amazon_callback_invalid_state(client_a, user_b):
    # Invalid JWT signature
    resp = await client_a.post("/ads-accounts/amazon/callback", json={"code": "c123", "state": "tampered.jwt.token"})
    assert resp.status_code == 400

    # Expired state JWT
    expired_payload = {
        "user_id": 1,
        "nonce": "n1",
        "purpose": "lwa_oauth",
        "exp": datetime.now(timezone.utc) - timedelta(minutes=5),
    }
    expired_state = jwt.encode(expired_payload, settings.JWT_SECRET_KEY, algorithm="HS256")
    resp = await client_a.post("/ads-accounts/amazon/callback", json={"code": "c123", "state": expired_state})
    assert resp.status_code == 400

    # Cross-user state (user B's state submitted by user A)
    user_b_payload = {
        "user_id": user_b.id,
        "nonce": "n2",
        "purpose": "lwa_oauth",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    cross_user_state = jwt.encode(user_b_payload, settings.JWT_SECRET_KEY, algorithm="HS256")
    resp = await client_a.post("/ads-accounts/amazon/callback", json={"code": "c123", "state": cross_user_state})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 6. LWA Callback Code Exchange & Never Leaking Tokens
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_amazon_callback_code_exchange_success_and_no_token_leak(
    client_a, user_a, db_session, monkeypatch
):
    key = crypto.generate_key()
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", key)
    monkeypatch.setattr(settings, "AMAZON_LWA_CLIENT_ID", "amzn1.test_client_id")
    monkeypatch.setattr(settings, "AMAZON_LWA_CLIENT_SECRET", "test_client_secret")
    monkeypatch.setattr(settings, "AMAZON_LWA_REDIRECT_URI", "http://localhost:3000/connect-amazon/callback")

    # Generate valid state for user_a
    state_payload = {
        "user_id": user_a.id,
        "nonce": "nonce123",
        "purpose": "lwa_oauth",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
    }
    valid_state = jwt.encode(state_payload, settings.JWT_SECRET_KEY, algorithm="HS256")

    orig_post = httpx.AsyncClient.post
    orig_get = httpx.AsyncClient.get

    # Mock httpx responses for token exchange and profiles
    async def mock_post(self, url, **kwargs):
        if "token" in str(url):
            return httpx.Response(
                200,
                json={
                    "access_token": "Atza|test_mock_access_token_12345",
                    "refresh_token": "Atzr|test_mock_refresh_token_67890",
                    "token_type": "bearer",
                    "expires_in": 3600,
                },
                request=httpx.Request("POST", str(url)),
            )
        return await orig_post(self, url, **kwargs)

    async def mock_get(self, url, **kwargs):
        if "/v2/profiles" in str(url):
            return httpx.Response(
                200,
                json=[
                    {
                        "profileId": 1234567890,
                        "countryCode": "US",
                        "currencyCode": "USD",
                        "timezone": "America/Los_Angeles",
                        "accountInfo": {
                            "marketplaceStringId": "ATVPDKIKX0DER",
                            "id": "ENTITY_US_1",
                            "type": "seller",
                            "name": "US Storefront",
                        },
                    },
                    {
                        "profileId": 9876543210,
                        "countryCode": "IN",
                        "currencyCode": "INR",
                        "timezone": "Asia/Kolkata",
                        "accountInfo": {
                            "marketplaceStringId": "A21TJRUUN4KGV",
                            "id": "ENTITY_IN_1",
                            "type": "seller",
                            "name": "India Storefront",
                        },
                    },
                ],
                request=httpx.Request("GET", str(url)),
            )
        return await orig_get(self, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    resp = await client_a.post("/ads-accounts/amazon/callback", json={"code": "auth_code_123", "state": valid_state})
    assert resp.status_code == 200
    accounts = resp.json()
    # Only IN profile ingested (US profile filtered by India-only live gate)
    assert len(accounts) == 1
    assert accounts[0]["marketplace"] == "IN"

    # CRITICAL SECURITY CHECK: Ensure token fields NEVER appear in response JSON
    for acc in accounts:
        assert "refresh_token" not in acc
        assert "refresh_token_encrypted" not in acc
        assert "access_token" not in acc
        assert acc["is_mock"] is False
        assert acc["connection_status"] == "connected"

    # Verify database persistence
    res = await db_session.execute(select(AdsAccount).where(AdsAccount.user_id == user_a.id, AdsAccount.is_mock == False))
    db_accounts = res.scalars().all()
    assert len(db_accounts) == 1

    for acc in db_accounts:
        assert acc.refresh_token_encrypted is not None
        # Must be encrypted: cannot match raw plaintext
        assert acc.refresh_token_encrypted != "Atzr|test_mock_refresh_token_67890"
        # Must successfully decrypt to original refresh token
        decrypted = crypto.decrypt(acc.refresh_token_encrypted, key=key)
        assert decrypted == "Atzr|test_mock_refresh_token_67890"


@pytest.mark.asyncio
async def test_amazon_callback_code_exchange_failure(client_a, user_a, monkeypatch):
    monkeypatch.setattr(settings, "AMAZON_LWA_CLIENT_ID", "amzn1.test_client_id")
    monkeypatch.setattr(settings, "AMAZON_LWA_CLIENT_SECRET", "test_client_secret")
    monkeypatch.setattr(settings, "AMAZON_LWA_REDIRECT_URI", "http://localhost:3000/connect-amazon/callback")

    state_payload = {
        "user_id": user_a.id,
        "nonce": "n",
        "purpose": "lwa_oauth",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
    }
    valid_state = jwt.encode(state_payload, settings.JWT_SECRET_KEY, algorithm="HS256")

    orig_post = httpx.AsyncClient.post

    async def mock_post_fail(self, url, **kwargs):
        if "token" in str(url):
            return httpx.Response(
                400,
                json={"error": "invalid_grant", "error_description": "The authorization code is invalid"},
                request=httpx.Request("POST", str(url)),
            )
        return await orig_post(self, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post_fail)

    resp = await client_a.post("/ads-accounts/amazon/callback", json={"code": "bad_code", "state": valid_state})
    assert resp.status_code == 400
    assert "Failed to exchange authorization code" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 7. Token Refresh in AmazonAdsProvider
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_amazon_provider_token_refresh_and_reauth(db_session, user_a, monkeypatch):
    key = crypto.generate_key()
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", key)
    monkeypatch.setattr(settings, "AMAZON_LWA_CLIENT_ID", "client_123")
    monkeypatch.setattr(settings, "AMAZON_LWA_CLIENT_SECRET", "secret_456")

    # Create real Amazon account with encrypted refresh token
    encrypted_token = crypto.encrypt("valid_refresh_token_abc", key=key)
    account = AdsAccount(
        user_id=user_a.id,
        profile_id="999888",
        marketplace="US",
        is_mock=False,
        refresh_token_encrypted=encrypted_token,
        connection_status="connected",
    )
    db_session.add(account)
    await db_session.commit()
    await db_session.refresh(account)

    provider = AmazonAdsProvider(db_session)

    # 1. Successful token refresh
    async def mock_refresh_success(self, url, **kwargs):
        return httpx.Response(
            200,
            json={"access_token": "fresh_access_token_xyz", "expires_in": 3600},
            request=httpx.Request("POST", str(url)),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_refresh_success)
    token = await provider.get_access_token(account)
    assert token == "fresh_access_token_xyz"
    assert account.connection_status == "connected"
    assert account.access_token_expires_at is not None

    # Clear cache for testing failure
    AmazonAdsProvider._access_token_cache.clear()
    account.access_token_expires_at = None

    # 2. Revoked token (invalid_grant) triggers connection_status = reauth_required
    async def mock_refresh_revoked(self, url, **kwargs):
        return httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": "Token expired or revoked"},
            request=httpx.Request("POST", str(url)),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_refresh_revoked)
    with pytest.raises(HTTPException) as exc_info:
        await provider.get_access_token(account)

    assert exc_info.value.status_code == 401
    await db_session.refresh(account)
    assert account.connection_status == "reauth_required"


# ---------------------------------------------------------------------------
# 8. Disconnect Account & Token Purging
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disconnect_account(client_a, db_session, user_a):
    acc = AdsAccount(
        user_id=user_a.id,
        marketplace="US",
        profile_id="temp_profile_123",
        refresh_token_encrypted="encrypted_value",
        connection_status="connected",
        is_mock=False,
    )
    db_session.add(acc)
    await db_session.commit()
    await db_session.refresh(acc)

    # User A disconnects the account
    resp = await client_a.delete(f"/ads-accounts/{acc.id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # Account is permanently removed
    res = await db_session.execute(select(AdsAccount).where(AdsAccount.id == acc.id))
    assert res.scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# 9. Multi-Tenant Profile Isolation (409 Conflict on Profile Takeover Attempt)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_amazon_callback_profile_already_linked_to_another_user(client_b, user_a, user_b, db_session, monkeypatch):
    """
    If an Amazon profile is already linked to User A, User B attempting to
    link that profile must receive HTTP 409 Conflict, and User A's ownership
    must not be reassigned.
    """
    key = crypto.generate_key()
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", key)
    monkeypatch.setattr(settings, "AMAZON_LWA_CLIENT_ID", "amzn1.test_client_id")
    monkeypatch.setattr(settings, "AMAZON_LWA_CLIENT_SECRET", "test_client_secret")
    monkeypatch.setattr(settings, "AMAZON_LWA_REDIRECT_URI", "http://localhost:3000/connect-amazon/callback")

    # 1. User A owns profile_id="claimed_profile_999"
    claimed_acc = AdsAccount(
        user_id=user_a.id,
        profile_id="claimed_profile_999",
        marketplace="IN",
        is_mock=False,
        connection_status="connected",
        refresh_token_encrypted=crypto.encrypt("refresh_a"),
    )
    db_session.add(claimed_acc)
    await db_session.commit()
    await db_session.refresh(claimed_acc)

    # 2. User B tries to ingest the same profile via callback
    state_payload = {
        "user_id": user_b.id,
        "nonce": "nonce_b",
        "purpose": "lwa_oauth",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
    }
    state = jwt.encode(state_payload, settings.JWT_SECRET_KEY, algorithm="HS256")

    orig_post = httpx.AsyncClient.post
    async def mock_post(self, url, **kwargs):
        url_str = str(url)
        if "auth/o2/token" in url_str:
            return httpx.Response(
                200,
                json={
                    "access_token": "mock_access_token_b",
                    "refresh_token": "mock_refresh_token_b",
                    "expires_in": 3600,
                },
                request=httpx.Request("POST", url_str),
            )
        return await orig_post(self, url, **kwargs)

    async def mock_get(self, url, **kwargs):
        url_str = str(url)
        if "/v2/profiles" in url_str:
            return httpx.Response(
                200,
                json=[
                    {
                        "profileId": "claimed_profile_999",
                        "countryCode": "IN",
                        "accountInfo": {"name": "User A Brand"},
                    }
                ],
                request=httpx.Request("GET", url_str),
            )
        return httpx.Response(404, request=httpx.Request("GET", url_str))

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    resp = await client_b.post(
        "/ads-accounts/amazon/callback",
        json={"code": "valid_code_b", "state": state},
    )
    assert resp.status_code == 409
    assert "already connected to another" in resp.json()["detail"]

    # Verify User A still owns the profile
    await db_session.refresh(claimed_acc)
    assert claimed_acc.user_id == user_a.id


# ---------------------------------------------------------------------------
# 10. Direct Connect Guard (Rejects Non-Mock Provider with 400)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_amazon_rejects_non_mock_provider(client_a):
    orig = settings.ADS_PROVIDER
    try:
        settings.ADS_PROVIDER = "amazon"
        resp = await client_a.post("/ads-accounts/connect", json={"marketplace": "US"})
        assert resp.status_code == 400
        assert "Live mode: connect through Login with Amazon" in resp.json()["detail"]
    finally:
        settings.ADS_PROVIDER = orig


# ---------------------------------------------------------------------------
# 11. Sync Converts NotImplementedError to 501 Not Implemented
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_account_converts_not_implemented_to_501(client_a, db_session, account_a, monkeypatch):
    from app.providers.ads.mock_provider import MockAdsProvider
    async def mock_sync(self, account_id):
        raise NotImplementedError("Live Amazon sync is not implemented yet.")

    monkeypatch.setattr(MockAdsProvider, "sync_account_data", mock_sync)
    resp = await client_a.post(f"/ads-accounts/{account_a.id}/sync")
    assert resp.status_code == 501
    assert "Live Amazon sync is not implemented yet." in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 12. Contact Form Secure Persistence & Rate Limiting (No PII Logging)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_contact_form_submission_and_persistence(client_a, db_session, caplog):
    import logging
    from app.models.contact_message import ContactMessage

    caplog.set_level(logging.INFO)
    payload = {
        "name": "Seller Jane",
        "email": "jane@sellerbrand.com",
        "message": "We would like to inquire about automated bidding for Amazon India.",
    }

    resp = await client_a.post("/contact", json=payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # Confirm message was stored in database
    res = await db_session.execute(
        select(ContactMessage).where(ContactMessage.email == "jane@sellerbrand.com")
    )
    msg = res.scalar_one_or_none()
    assert msg is not None
    assert msg.name == "Seller Jane"
    assert "automated bidding" in msg.message

    # Confirm name, email, and message text are NOT logged in log records
    for record in caplog.records:
        if "contact" in record.name:
            assert "Seller Jane" not in record.message
            assert "jane@sellerbrand.com" not in record.message
            assert "automated bidding" not in record.message

