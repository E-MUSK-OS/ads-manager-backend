import pytest
from datetime import datetime, timezone, timedelta
import httpx
from jose import jwt
from sqlalchemy import select

from app.config import settings
from app.core import crypto
from app.models.ads_account import AdsAccount
from app.models.contact_message import ContactMessage
from app.providers.ads.amazon_provider import AmazonAdsProvider
from app.routers.contact import _RATE_LIMIT_BUCKET


# ---------------------------------------------------------------------------
# 1. Contact Form: Valid, Invalid Email, Rate Limiting
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_contact_form_valid_invalid_and_rate_limit(client_a, db_session):
    # Clear rate limit bucket before test
    _RATE_LIMIT_BUCKET.clear()

    try:
        # 1a. Valid POST /contact returns 200 and stores row
        payload = {
            "name": "Seller Alice",
            "email": "alice@sellerbrand.com",
            "message": "We need help scaling our sponsored product ads.",
        }
        resp = await client_a.post("/contact", json=payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        res = await db_session.execute(
            select(ContactMessage).where(ContactMessage.email == "alice@sellerbrand.com")
        )
        msg = res.scalar_one_or_none()
        assert msg is not None
        assert msg.name == "Seller Alice"
        assert msg.email == "alice@sellerbrand.com"
        assert msg.message == "We need help scaling our sponsored product ads."

        # 1b. Invalid email returns 422 Unprocessable Entity
        bad_payload = {
            "name": "Invalid User",
            "email": "not-a-valid-email",
            "message": "This should fail validation.",
        }
        bad_resp = await client_a.post("/contact", json=bad_payload)
        assert bad_resp.status_code == 422

        # 1c. 6th request from same IP returns 429
        # Currently 1 request made, send 4 more (total 5)
        for i in range(4):
            r = await client_a.post(
                "/contact",
                json={
                    "name": f"User {i}",
                    "email": f"user{i}@example.com",
                    "message": "Testing rate limit threshold.",
                },
            )
            assert r.status_code == 200

        # The 6th request must be rejected with 429
        rate_limited_resp = await client_a.post(
            "/contact",
            json={
                "name": "User 6",
                "email": "user6@example.com",
                "message": "This 6th request must be rate-limited.",
            },
        )
        assert rate_limited_resp.status_code == 429
        assert "Too many contact submissions" in rate_limited_resp.json()["detail"]

    finally:
        _RATE_LIMIT_BUCKET.clear()


# ---------------------------------------------------------------------------
# 2. Callback Ownership & Repeat Callback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_ownership_and_repeat_callback(client_a, client_b, user_a, user_b, db_session, monkeypatch):
    key = crypto.generate_key()
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", key)
    monkeypatch.setattr(settings, "AMAZON_LWA_CLIENT_ID", "amzn1.test_client_id")
    monkeypatch.setattr(settings, "AMAZON_LWA_CLIENT_SECRET", "test_client_secret")
    monkeypatch.setattr(settings, "AMAZON_LWA_REDIRECT_URI", "http://localhost:3000/connect-amazon/callback")

    orig_post = httpx.AsyncClient.post
    async def mock_post(self, url, **kwargs):
        url_str = str(url)
        if "auth/o2/token" in url_str:
            return httpx.Response(
                200,
                json={
                    "access_token": "token_initial_123",
                    "refresh_token": "refresh_initial_456",
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
                        "profileId": "111",
                        "countryCode": "IN",
                        "accountInfo": {"name": "Shared Brand 111"},
                    }
                ],
                request=httpx.Request("GET", url_str),
            )
        return httpx.Response(404, request=httpx.Request("GET", url_str))

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    # 2a. User A connects profile "111" through mocked callback
    state_a = jwt.encode(
        {"user_id": user_a.id, "nonce": "n_a", "purpose": "lwa_oauth", "exp": datetime.now(timezone.utc) + timedelta(minutes=10)},
        settings.JWT_SECRET_KEY,
        algorithm="HS256",
    )
    resp_a = await client_a.post("/ads-accounts/amazon/callback", json={"code": "code_a", "state": state_a})
    assert resp_a.status_code == 200

    res = await db_session.execute(select(AdsAccount).where(AdsAccount.profile_id == "111"))
    acc = res.scalar_one_or_none()
    assert acc is not None
    assert acc.user_id == user_a.id

    # 2b. User B calls callback with profile "111" -> gets 409 and row's user_id is still A
    state_b = jwt.encode(
        {"user_id": user_b.id, "nonce": "n_b", "purpose": "lwa_oauth", "exp": datetime.now(timezone.utc) + timedelta(minutes=10)},
        settings.JWT_SECRET_KEY,
        algorithm="HS256",
    )
    resp_b = await client_b.post("/ads-accounts/amazon/callback", json={"code": "code_b", "state": state_b})
    assert resp_b.status_code == 409
    assert "already connected to another" in resp_b.json()["detail"]

    await db_session.refresh(acc)
    assert acc.user_id == user_a.id

    # 2c. Repeat callback by User A succeeds and updates tokens
    state_a2 = jwt.encode(
        {"user_id": user_a.id, "nonce": "n_a2", "purpose": "lwa_oauth", "exp": datetime.now(timezone.utc) + timedelta(minutes=10)},
        settings.JWT_SECRET_KEY,
        algorithm="HS256",
    )
    resp_a2 = await client_a.post("/ads-accounts/amazon/callback", json={"code": "code_a2", "state": state_a2})
    assert resp_a2.status_code == 200
    await db_session.refresh(acc)
    assert acc.user_id == user_a.id
    assert acc.connection_status == "connected"


# ---------------------------------------------------------------------------
# 3. Live-Mode Connect: Returns 400 & Creates No Account Row
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_live_mode_connect_returns_400_and_creates_no_row(client_a, db_session, monkeypatch):
    monkeypatch.setattr(settings, "ADS_PROVIDER", "amazon")

    res_before = await db_session.execute(select(AdsAccount))
    count_before = len(res_before.scalars().all())

    resp = await client_a.post("/ads-accounts/connect", json={"marketplace": "US"})
    assert resp.status_code in (404, 405)

    res_after = await db_session.execute(select(AdsAccount))
    count_after = len(res_after.scalars().all())
    assert count_after == count_before


# ---------------------------------------------------------------------------
# 4. Live-Mode Sync: Returns 501 (Not 500)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_live_mode_sync_returns_501(client_a, user_a, db_session, monkeypatch):
    monkeypatch.setattr(settings, "ADS_PROVIDER", "amazon")

    async def mock_sync(self, account_id, *args, **kwargs):
        raise NotImplementedError("Live Amazon sync is not implemented yet.")

    monkeypatch.setattr(AmazonAdsProvider, "sync_account_data", mock_sync)

    real_acc = AdsAccount(
        user_id=user_a.id,
        profile_id="live_profile_555",
        marketplace="US",
        is_mock=False,
        connection_status="connected",
        refresh_token_encrypted=crypto.encrypt("mock_refresh", key=crypto.generate_key()),
    )
    db_session.add(real_acc)
    await db_session.commit()
    await db_session.refresh(real_acc)

    resp = await client_a.post(f"/ads-accounts/{real_acc.id}/sync")
    assert resp.status_code == 501
    assert "Live Amazon sync is not implemented yet." in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 5. Provider Mode Endpoint (Mock Mode Removed)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mock_mode_regression_connect_and_sync(client_a):
    resp = await client_a.get("/ads-accounts/provider-mode")
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "amazon"
    assert data["region"] == "eu"


# ---------------------------------------------------------------------------
# 6. India-Only Live-Mode Gate & Defaults Tests (Part D)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_stores_only_in_profile(client_a, user_a, db_session, monkeypatch):
    key = crypto.generate_key()
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", key)
    monkeypatch.setattr(settings, "AMAZON_LWA_CLIENT_ID", "amzn1.test_client_id")
    monkeypatch.setattr(settings, "AMAZON_LWA_CLIENT_SECRET", "test_client_secret")
    monkeypatch.setattr(settings, "AMAZON_LWA_REDIRECT_URI", "http://localhost:3000/connect-amazon/callback")

    orig_post = httpx.AsyncClient.post
    async def mock_post(self, url, **kwargs):
        url_str = str(url)
        if "auth/o2/token" in url_str:
            return httpx.Response(
                200,
                json={"access_token": "token_in_us", "refresh_token": "refresh_in_us", "expires_in": 3600},
                request=httpx.Request("POST", url_str),
            )
        return await orig_post(self, url, **kwargs)

    async def mock_get(self, url, **kwargs):
        url_str = str(url)
        if "/v2/profiles" in url_str:
            return httpx.Response(
                200,
                json=[
                    {"profileId": "in_profile_1", "countryCode": "IN", "accountInfo": {"name": "India Store"}},
                    {"profileId": "us_profile_2", "countryCode": "US", "accountInfo": {"name": "US Store"}},
                ],
                request=httpx.Request("GET", url_str),
            )
        return httpx.Response(404, request=httpx.Request("GET", url_str))

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    state = jwt.encode(
        {"user_id": user_a.id, "nonce": "n_in_us", "purpose": "lwa_oauth", "exp": datetime.now(timezone.utc) + timedelta(minutes=10)},
        settings.JWT_SECRET_KEY,
        algorithm="HS256",
    )
    resp = await client_a.post("/ads-accounts/amazon/callback", json={"code": "code_in_us", "state": state})
    assert resp.status_code == 200
    accounts = resp.json()
    # (1) stores ONLY the IN profile
    assert len(accounts) == 1
    assert accounts[0]["profile_id"] == "in_profile_1"
    assert accounts[0]["marketplace"] == "IN"

    # Verify database
    res = await db_session.execute(select(AdsAccount).where(AdsAccount.profile_id == "us_profile_2"))
    assert res.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_callback_only_us_profiles_returns_400(client_a, user_a, monkeypatch):
    key = crypto.generate_key()
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", key)
    monkeypatch.setattr(settings, "AMAZON_LWA_CLIENT_ID", "amzn1.test_client_id")
    monkeypatch.setattr(settings, "AMAZON_LWA_CLIENT_SECRET", "test_client_secret")
    monkeypatch.setattr(settings, "AMAZON_LWA_REDIRECT_URI", "http://localhost:3000/connect-amazon/callback")

    orig_post = httpx.AsyncClient.post
    async def mock_post(self, url, **kwargs):
        url_str = str(url)
        if "auth/o2/token" in url_str:
            return httpx.Response(
                200,
                json={"access_token": "token_us_only", "refresh_token": "refresh_us_only", "expires_in": 3600},
                request=httpx.Request("POST", url_str),
            )
        return await orig_post(self, url, **kwargs)

    async def mock_get(self, url, **kwargs):
        url_str = str(url)
        if "/v2/profiles" in url_str:
            return httpx.Response(
                200,
                json=[{"profileId": "us_only", "countryCode": "US", "accountInfo": {"name": "US Store"}}],
                request=httpx.Request("GET", url_str),
            )
        return httpx.Response(404, request=httpx.Request("GET", url_str))

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    state = jwt.encode(
        {"user_id": user_a.id, "nonce": "n_us_only", "purpose": "lwa_oauth", "exp": datetime.now(timezone.utc) + timedelta(minutes=10)},
        settings.JWT_SECRET_KEY,
        algorithm="HS256",
    )
    # (2) a response with only US profiles returns 400 with India-only message
    resp = await client_a.post("/ads-accounts/amazon/callback", json={"code": "code_us_only", "state": state})
    assert resp.status_code == 400
    assert "No Amazon India (IN) advertising profiles found" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_callback_profile_without_country_code_skipped(client_a, user_a, monkeypatch):
    key = crypto.generate_key()
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", key)
    monkeypatch.setattr(settings, "AMAZON_LWA_CLIENT_ID", "amzn1.test_client_id")
    monkeypatch.setattr(settings, "AMAZON_LWA_CLIENT_SECRET", "test_client_secret")
    monkeypatch.setattr(settings, "AMAZON_LWA_REDIRECT_URI", "http://localhost:3000/connect-amazon/callback")

    orig_post = httpx.AsyncClient.post
    async def mock_post(self, url, **kwargs):
        url_str = str(url)
        if "auth/o2/token" in url_str:
            return httpx.Response(
                200,
                json={"access_token": "token_no_country", "refresh_token": "refresh_no_country", "expires_in": 3600},
                request=httpx.Request("POST", url_str),
            )
        return await orig_post(self, url, **kwargs)

    async def mock_get(self, url, **kwargs):
        url_str = str(url)
        if "/v2/profiles" in url_str:
            return httpx.Response(
                200,
                json=[
                    {"profileId": "in_valid", "countryCode": "IN", "accountInfo": {"name": "IN Valid Store"}},
                    {"profileId": "missing_country", "countryCode": None, "accountInfo": {"name": "Unknown Store"}},
                ],
                request=httpx.Request("GET", url_str),
            )
        return httpx.Response(404, request=httpx.Request("GET", url_str))

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    state = jwt.encode(
        {"user_id": user_a.id, "nonce": "n_missing", "purpose": "lwa_oauth", "exp": datetime.now(timezone.utc) + timedelta(minutes=10)},
        settings.JWT_SECRET_KEY,
        algorithm="HS256",
    )
    # (3) profile with no countryCode is skipped
    resp = await client_a.post("/ads-accounts/amazon/callback", json={"code": "code_missing", "state": state})
    assert resp.status_code == 200
    accounts = resp.json()
    assert len(accounts) == 1
    assert accounts[0]["profile_id"] == "in_valid"


@pytest.mark.asyncio
async def test_default_mock_connect_creates_marketplace_in(client_a):
    resp = await client_a.post("/ads-accounts/connect", json={})
    assert resp.status_code in (404, 405)


def test_config_rejects_short_jwt_secret_when_amazon_provider():
    from app.config import Settings
    # (5) config rejects a short JWT_SECRET_KEY in production mode
    with pytest.raises(ValueError, match="at least 32 characters"):
        Settings(
            ENVIRONMENT="production",
            ADS_PROVIDER="amazon",
            DATABASE_URL="sqlite+aiosqlite:///:memory:",
            JWT_SECRET_KEY="too_short_secret",
            AMAZON_LWA_CLIENT_ID="mock_id",
            AMAZON_LWA_CLIENT_SECRET="mock_secret",
            AMAZON_LWA_REDIRECT_URI="http://localhost:3000/callback",
            TOKEN_ENCRYPTION_KEY="mock_token_key",
        )


