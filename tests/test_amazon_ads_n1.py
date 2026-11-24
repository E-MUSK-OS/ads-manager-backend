"""Tests for Amazon Ads (Login with Amazon, LWA) OAuth Connection (Phase N1 Backend).

Covers all Phase N1 requirements:
1. POST /amazon-ads/connect returns URL and stores hashed state (SHA-256)
2. GET /amazon-ads/callback success with public access (no auth header)
3. GET /amazon-ads/callback denied consent (error=access_denied)
4. Missing, invalid, expired, and reused state handling
5. Token exchange failure
6. Profile fetch failure (status=PENDING_PROFILES, needs_profile_refresh=1)
7. Select own Indian profile (creates/updates AdsAccount) and reject non-IN profile
8. Select another user's profile returns 403 PROFILE_NOT_OWNED
9. GET /amazon-ads/status for connected and not connected
10. POST /amazon-ads/disconnect (preserves DB rows, clears credentials)
11. User isolation between User A and User B
12. Assert no response or log line leaks secret, token, code, or full state
"""

import hashlib
import json
import logging
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs
import httpx
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.main import app
from app.database import get_db
from app.config import settings
from app.models.user import User
from app.models.ads_account import AdsAccount
from app.models.amazon_ads import AmazonOAuthState, AmazonConnection, AmazonProfile
from app.core.crypto import encrypt, decrypt


from app.routers.amazon_ads import ensure_utc

MOCK_ACCESS_TOKEN = "Atza|IQEBLjAsAhR-mock-access-token-12345"
MOCK_REFRESH_TOKEN = "Atzr|IQEBLjAsAhR-mock-refresh-token-67890"
MOCK_CLIENT_SECRET = "mock_client_secret_xyz_very_secret"
MOCK_AUTH_CODE = "AN_AUTHORIZATION_CODE_123456"

MOCK_PROFILES_DATA = [
    {
        "profileId": "12345678901234",
        "countryCode": "IN",
        "currencyCode": "INR",
        "timezone": "Asia/Kolkata",
        "accountInfo": {
            "marketplaceStringId": "A21TJRUUN4KGV",
            "id": "ENTITY12345",
            "name": "NETU TECHNO Official Store",
            "type": "seller",
        },
    },
    {
        "profileId": "98765432109876",
        "countryCode": "US",
        "currencyCode": "USD",
        "timezone": "America/Los_Angeles",
        "accountInfo": {
            "marketplaceStringId": "ATVPDKIKX0DER",
            "id": "ENTITY98765",
            "name": "NETU TECHNO US Store",
            "type": "seller",
        },
    },
]


@pytest.fixture(autouse=True)
def configure_amazon_settings(monkeypatch):
    """Ensure test settings contain known dummy credentials."""
    from cryptography.fernet import Fernet
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    monkeypatch.setattr(settings, "AMAZON_LWA_CLIENT_ID", "amzn1.application-oa2-client.mockclientid")
    monkeypatch.setattr(settings, "AMAZON_LWA_CLIENT_SECRET", MOCK_CLIENT_SECRET)
    monkeypatch.setattr(settings, "AMAZON_LWA_REDIRECT_URI", "https://api.netutechno.com/api/v1/amazon-ads/callback")
    monkeypatch.setattr(settings, "FRONTEND_BASE_URL", "http://localhost:3000")
    monkeypatch.setattr(settings, "AMAZON_ADS_REGION", "eu")


def create_mock_transport(
    token_status: int = 200,
    profiles_status: int = 200,
    profiles_data: list = None,
):
    """Create an httpx MockTransport handler for Amazon LWA and Profiles API."""
    if profiles_data is None:
        profiles_data = MOCK_PROFILES_DATA

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        # Token exchange / refresh
        if "auth/o2/token" in url_str:
            if token_status != 200:
                return httpx.Response(
                    token_status,
                    json={"error": "invalid_grant", "error_description": "The authorization code is invalid."},
                    headers={"content-type": "application/json"},
                )
            return httpx.Response(
                200,
                json={
                    "access_token": MOCK_ACCESS_TOKEN,
                    "refresh_token": MOCK_REFRESH_TOKEN,
                    "token_type": "bearer",
                    "expires_in": 3600,
                },
                headers={"content-type": "application/json"},
            )
        # Profiles call
        if "/v2/profiles" in url_str:
            if profiles_status != 200:
                return httpx.Response(profiles_status, text="Server Error", headers={"content-type": "text/plain"})
            return httpx.Response(
                200,
                json=profiles_data,
                headers={"content-type": "application/json"},
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


_orig_async_client = httpx.AsyncClient

def patch_mock_client(monkeypatch, transport):
    def _client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return _orig_async_client(*args, **kwargs)
    monkeypatch.setattr("app.routers.amazon_ads.httpx.AsyncClient", _client_factory)


# ===========================================================================
# 1. POST /amazon-ads/connect
# ===========================================================================

@pytest.mark.asyncio
async def test_connect_returns_url_and_stores_state_hash(client_a: AsyncClient, db_session: AsyncSession, user_a: User):
    """Connect endpoint generates valid EU auth URL and stores SHA-256 state hash."""
    resp = await client_a.post("/amazon-ads/connect", json={"region": "EU"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["region"] == "EU"
    auth_url = data["authorizationUrl"]

    parsed = urlparse(auth_url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "eu.account.amazon.com"
    assert parsed.path == "/ap/oa"

    qs = parse_qs(parsed.query)
    assert qs["client_id"][0] == settings.AMAZON_LWA_CLIENT_ID
    assert qs["response_type"][0] == "code"
    assert qs["scope"][0] == "advertising::campaign_management"
    assert qs["redirect_uri"][0] == settings.AMAZON_LWA_REDIRECT_URI
    raw_state = qs["state"][0]
    assert len(raw_state) >= 32

    # Verify state is stored ONLY as SHA-256 hash in DB
    expected_hash = hashlib.sha256(raw_state.encode("utf-8")).hexdigest()
    res = await db_session.execute(
        select(AmazonOAuthState).where(AmazonOAuthState.state_hash == expected_hash)
    )
    saved = res.scalars().first()
    assert saved is not None
    assert saved.user_id == user_a.id
    assert saved.region == "eu"
    assert saved.used_at is None
    assert ensure_utc(saved.expires_at) > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_connect_rejects_non_eu_region(client_a: AsyncClient):
    """India is EU region only; NA/FE regions must be rejected with 400."""
    resp = await client_a.post("/amazon-ads/connect", json={"region": "NA"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["code"] == "VALIDATION_ERROR"
    assert "Only EU region is supported" in body["message"]


@pytest.mark.asyncio
async def test_connect_requires_auth(client_a: AsyncClient):
    """Connect endpoint without valid SaaS token returns 401 UNAUTHORIZED."""
    resp = await client_a.post("/amazon-ads/connect", json={"region": "EU"}, headers={"x-test-user": "0"})
    assert resp.status_code == 401
    body = resp.json()
    assert body["success"] is False
    assert body["code"] == "UNAUTHORIZED"


# ===========================================================================
# 2. GET /amazon-ads/callback (Public, 302 Redirect)
# ===========================================================================

@pytest.mark.asyncio
async def test_callback_success_public_access(client_a: AsyncClient, db_session: AsyncSession, user_a: User, monkeypatch):
    """
    Callback endpoint is public (no Auth header), completes LWA exchange,
    creates AmazonConnection and profiles, and returns HTTP 302 to frontend.
    """
    transport = create_mock_transport()
    patch_mock_client(monkeypatch, transport)

    # 1. Initiate connect
    resp_conn = await client_a.post("/amazon-ads/connect", json={"region": "EU"})
    auth_url = resp_conn.json()["data"]["authorizationUrl"]
    raw_state = parse_qs(urlparse(auth_url).query)["state"][0]

    # 2. Call callback anonymously (no x-test-user, simulating Amazon browser redirect)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as public_client:
        resp = await public_client.get(
            f"/amazon-ads/callback?code={MOCK_AUTH_CODE}&state={raw_state}&scope=advertising::campaign_management",
            follow_redirects=False,
        )

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location == "http://localhost:3000/connect-amazon?status=success"

    # Verify state marked used
    state_hash = hashlib.sha256(raw_state.encode("utf-8")).hexdigest()
    res = await db_session.execute(
        select(AmazonOAuthState).where(AmazonOAuthState.state_hash == state_hash)
    )
    oauth_state = res.scalars().first()
    assert oauth_state.used_at is not None

    # Verify AmazonConnection created
    res = await db_session.execute(
        select(AmazonConnection).where(AmazonConnection.user_id == user_a.id)
    )
    conn = res.scalars().first()
    assert conn is not None
    assert conn.status == "CONNECTED"
    assert conn.refresh_token_encrypted is not None
    assert decrypt(conn.refresh_token_encrypted) == MOCK_REFRESH_TOKEN

    # Verify profiles created
    res = await db_session.execute(
        select(AmazonProfile).where(AmazonProfile.connection_id == conn.id)
    )
    profiles = res.scalars().all()
    assert len(profiles) == 2
    in_profile = next(p for p in profiles if p.country_code == "IN")
    assert in_profile.account_name == "NETU TECHNO Official Store"
    assert in_profile.currency_code == "INR"
    assert in_profile.timezone == "Asia/Kolkata"


@pytest.mark.asyncio
async def test_callback_works_on_api_v1_prefix(client_a: AsyncClient, db_session: AsyncSession, monkeypatch):
    """Verify alias /api/v1/amazon-ads/callback functions identically."""
    transport = create_mock_transport()
    patch_mock_client(monkeypatch, transport)

    resp_conn = await client_a.post("/amazon-ads/connect", json={"region": "EU"})
    raw_state = parse_qs(urlparse(resp_conn.json()["data"]["authorizationUrl"]).query)["state"][0]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as public_client:
        resp = await public_client.get(
            f"/api/v1/amazon-ads/callback?code={MOCK_AUTH_CODE}&state={raw_state}",
            follow_redirects=False,
        )
    assert resp.status_code == 302
    assert "status=success" in resp.headers["location"]


# ===========================================================================
# 3. Denied consent & error checks
# ===========================================================================

@pytest.mark.asyncio
async def test_callback_denied_consent(client_a: AsyncClient, db_session: AsyncSession):
    """When user denies consent, redirect 302 to error page with reason=access_denied."""
    resp_conn = await client_a.post("/amazon-ads/connect", json={"region": "EU"})
    raw_state = parse_qs(urlparse(resp_conn.json()["data"]["authorizationUrl"]).query)["state"][0]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as public_client:
        resp = await public_client.get(
            f"/amazon-ads/callback?error=access_denied&error_description=User+refused+grant&state={raw_state}",
            follow_redirects=False,
        )
    assert resp.status_code == 302
    assert resp.headers["location"] == "http://localhost:3000/connect-amazon?status=error&reason=access_denied"

    # State should be marked used
    state_hash = hashlib.sha256(raw_state.encode("utf-8")).hexdigest()
    res = await db_session.execute(
        select(AmazonOAuthState).where(AmazonOAuthState.state_hash == state_hash)
    )
    assert res.scalars().first().used_at is not None


# ===========================================================================
# 4. Missing, invalid, expired, and reused state
# ===========================================================================

@pytest.mark.asyncio
async def test_callback_missing_params(client_a: AsyncClient):
    """Missing code or state redirects to error page with reason=invalid_request."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as public_client:
        resp = await public_client.get("/amazon-ads/callback?code=foo", follow_redirects=False)
        assert resp.status_code == 302
        assert "reason=invalid_request" in resp.headers["location"]

        resp2 = await public_client.get("/amazon-ads/callback?state=bar", follow_redirects=False)
        assert resp2.status_code == 302
        assert "reason=invalid_request" in resp2.headers["location"]


@pytest.mark.asyncio
async def test_callback_invalid_state(client_a: AsyncClient):
    """Unknown state redirects to error page with reason=invalid_state."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as public_client:
        resp = await public_client.get(
            "/amazon-ads/callback?code=somecode&state=nonexistentstate123456789012345",
            follow_redirects=False,
        )
    assert resp.status_code == 302
    assert "reason=invalid_state" in resp.headers["location"]


@pytest.mark.asyncio
async def test_callback_expired_state(client_a: AsyncClient, db_session: AsyncSession, user_a: User):
    """Expired state redirects to error page with reason=invalid_state."""
    raw_state = "expired_raw_state_12345678901234"
    state_hash = hashlib.sha256(raw_state.encode("utf-8")).hexdigest()
    expired = AmazonOAuthState(
        user_id=user_a.id,
        region="eu",
        state_hash=state_hash,
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db_session.add(expired)
    await db_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as public_client:
        resp = await public_client.get(
            f"/amazon-ads/callback?code=somecode&state={raw_state}",
            follow_redirects=False,
        )
    assert resp.status_code == 302
    assert "reason=invalid_state" in resp.headers["location"]


@pytest.mark.asyncio
async def test_callback_reused_state_rejected(client_a: AsyncClient, db_session: AsyncSession, monkeypatch):
    """Reused state (replay attack) is rejected with reason=invalid_state and does NOT call Amazon."""
    calls = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "b", "expires_in": 3600})

    patch_mock_client(monkeypatch, httpx.MockTransport(mock_handler))

    # 1. Connect and use once
    resp_conn = await client_a.post("/amazon-ads/connect", json={"region": "EU"})
    raw_state = parse_qs(urlparse(resp_conn.json()["data"]["authorizationUrl"]).query)["state"][0]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as public_client:
        resp1 = await public_client.get(f"/amazon-ads/callback?code=code1&state={raw_state}", follow_redirects=False)
        assert resp1.status_code == 302
        assert "status=success" in resp1.headers["location"]

        initial_calls_count = len(calls)
        assert initial_calls_count > 0

        # 2. Replay with same state
        resp2 = await public_client.get(f"/amazon-ads/callback?code=code2&state={raw_state}", follow_redirects=False)
        assert resp2.status_code == 302
        assert "reason=invalid_state" in resp2.headers["location"]

        # Ensure Amazon was NOT called again
        assert len(calls) == initial_calls_count


# ===========================================================================
# 5. Token exchange failure
# ===========================================================================

@pytest.mark.asyncio
async def test_callback_token_exchange_failure(client_a: AsyncClient, db_session: AsyncSession, monkeypatch):
    """When token exchange fails, redirect to error page and ensure state was still marked used."""
    transport = create_mock_transport(token_status=400)
    patch_mock_client(monkeypatch, transport)

    resp_conn = await client_a.post("/amazon-ads/connect", json={"region": "EU"})
    raw_state = parse_qs(urlparse(resp_conn.json()["data"]["authorizationUrl"]).query)["state"][0]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as public_client:
        resp = await public_client.get(f"/amazon-ads/callback?code=invalid_code&state={raw_state}", follow_redirects=False)

    assert resp.status_code == 302
    assert "reason=token_exchange_failed" in resp.headers["location"]

    # Verify state was marked used even though exchange failed
    state_hash = hashlib.sha256(raw_state.encode("utf-8")).hexdigest()
    res = await db_session.execute(select(AmazonOAuthState).where(AmazonOAuthState.state_hash == state_hash))
    assert res.scalars().first().used_at is not None


# ===========================================================================
# 6. Profile fetch failure
# ===========================================================================

@pytest.mark.asyncio
async def test_callback_profile_fetch_failure(client_a: AsyncClient, db_session: AsyncSession, user_a: User, monkeypatch):
    """When token exchange succeeds but profiles API fails, set status PENDING_PROFILES and redirect with needs_profile_refresh=1."""
    transport = create_mock_transport(token_status=200, profiles_status=500)
    patch_mock_client(monkeypatch, transport)

    resp_conn = await client_a.post("/amazon-ads/connect", json={"region": "EU"})
    raw_state = parse_qs(urlparse(resp_conn.json()["data"]["authorizationUrl"]).query)["state"][0]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as public_client:
        resp = await public_client.get(f"/amazon-ads/callback?code=code1&state={raw_state}", follow_redirects=False)

    assert resp.status_code == 302
    assert "status=success&needs_profile_refresh=1" in resp.headers["location"]

    res = await db_session.execute(select(AmazonConnection).where(AmazonConnection.user_id == user_a.id))
    conn = res.scalars().first()
    assert conn.status == "PENDING_PROFILES"


# ===========================================================================
# 7. GET /amazon-ads/accounts & POST /amazon-ads/accounts/select
# ===========================================================================

@pytest.mark.asyncio
async def test_accounts_list_and_select_own_profile(client_a: AsyncClient, db_session: AsyncSession, user_a: User, monkeypatch):
    """List profiles filters Indian profiles as selectable and selection creates active AdsAccount."""
    transport = create_mock_transport()
    patch_mock_client(monkeypatch, transport)

    # Connect
    resp_conn = await client_a.post("/amazon-ads/connect", json={"region": "EU"})
    raw_state = parse_qs(urlparse(resp_conn.json()["data"]["authorizationUrl"]).query)["state"][0]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as public_client:
        await public_client.get(f"/amazon-ads/callback?code=c&state={raw_state}", follow_redirects=False)

    # 1. GET /amazon-ads/accounts
    resp_accs = await client_a.get("/amazon-ads/accounts")
    assert resp_accs.status_code == 200
    body = resp_accs.json()
    assert body["success"] is True
    profiles = body["data"]["profiles"]
    assert len(profiles) == 2

    in_prof = next(p for p in profiles if p["countryCode"] == "IN")
    us_prof = next(p for p in profiles if p["countryCode"] == "US")
    assert in_prof["isSelectable"] is True
    assert us_prof["isSelectable"] is False

    # 2. Select non-IN profile is rejected
    resp_bad = await client_a.post("/amazon-ads/accounts/select", json={"profileId": us_prof["profileId"]})
    assert resp_bad.status_code == 400
    assert resp_bad.json()["code"] == "VALIDATION_ERROR"
    assert "Only India (IN) profiles" in resp_bad.json()["message"]

    # 3. Select IN profile succeeds
    resp_sel = await client_a.post("/amazon-ads/accounts/select", json={"profileId": in_prof["profileId"]})
    assert resp_sel.status_code == 200
    sel_body = resp_sel.json()
    assert sel_body["success"] is True
    assert sel_body["data"]["isSelected"] is True
    assert sel_body["data"]["profileId"] == in_prof["profileId"]

    # 4. Verify AdsAccount row exists and is CONNECTED
    res = await db_session.execute(
        select(AdsAccount).where(
            AdsAccount.user_id == user_a.id,
            AdsAccount.profile_id == in_prof["profileId"],
        )
    )
    acc = res.scalars().first()
    assert acc is not None
    assert acc.connection_status == "connected"
    assert acc.marketplace == "IN"


# ===========================================================================
# 8. Select another user's profile returns 403
# ===========================================================================

@pytest.mark.asyncio
async def test_select_another_users_profile_returns_403(
    client_a: AsyncClient,
    client_b: AsyncClient,
    db_session: AsyncSession,
    user_a: User,
    user_b: User,
    monkeypatch,
):
    """User B cannot select a profile that belongs to User A's connection."""
    transport = create_mock_transport()
    patch_mock_client(monkeypatch, transport)

    # Connect User A
    resp_a = await client_a.post("/amazon-ads/connect", json={"region": "EU"})
    state_a = parse_qs(urlparse(resp_a.json()["data"]["authorizationUrl"]).query)["state"][0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as public_client:
        await public_client.get(f"/amazon-ads/callback?code=cA&state={state_a}", follow_redirects=False)

    # Connect User B with a different profile list
    b_profiles = [
        {
            "profileId": "99999999999999",
            "countryCode": "IN",
            "currencyCode": "INR",
            "timezone": "Asia/Kolkata",
            "accountInfo": {"name": "User B Store"},
        }
    ]
    transport_b = create_mock_transport(profiles_data=b_profiles)
    patch_mock_client(monkeypatch, transport_b)

    resp_b = await client_b.post("/amazon-ads/connect", json={"region": "EU"})
    state_b = parse_qs(urlparse(resp_b.json()["data"]["authorizationUrl"]).query)["state"][0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as public_client:
        await public_client.get(f"/amazon-ads/callback?code=cB&state={state_b}", follow_redirects=False)

    # User B tries to select User A's profile ("12345678901234")
    resp_steal = await client_b.post("/amazon-ads/accounts/select", json={"profileId": "12345678901234"})
    assert resp_steal.status_code == 403
    body = resp_steal.json()
    assert body["success"] is False
    assert body["code"] == "PROFILE_NOT_OWNED"


# ===========================================================================
# 9. GET /amazon-ads/status
# ===========================================================================

@pytest.mark.asyncio
async def test_status_endpoint(client_a: AsyncClient, monkeypatch):
    """Status endpoint returns accurate metadata and never includes raw tokens."""
    # Before connect
    resp0 = await client_a.get("/amazon-ads/status")
    assert resp0.status_code == 200
    assert resp0.json()["data"]["connected"] is False
    assert resp0.json()["data"]["connectionStatus"] == "NOT_CONNECTED"

    # Connect & select
    transport = create_mock_transport()
    patch_mock_client(monkeypatch, transport)

    resp_conn = await client_a.post("/amazon-ads/connect", json={"region": "EU"})
    raw_state = parse_qs(urlparse(resp_conn.json()["data"]["authorizationUrl"]).query)["state"][0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as public_client:
        await public_client.get(f"/amazon-ads/callback?code=c&state={raw_state}", follow_redirects=False)

    await client_a.post("/amazon-ads/accounts/select", json={"profileId": "12345678901234"})

    # Check status
    resp1 = await client_a.get("/amazon-ads/status")
    assert resp1.status_code == 200
    data = resp1.json()["data"]
    assert data["connected"] is True
    assert data["connectionStatus"] == "CONNECTED"
    assert data["selectedProfileId"] == "12345678901234"
    assert data["amazonAccountName"] == "NETU TECHNO Official Store"
    assert data["region"] == "EU"

    # Verify no tokens in json
    raw_resp_text = resp1.text
    assert "token" not in raw_resp_text or "tokenExpiresAt" in raw_resp_text
    assert MOCK_ACCESS_TOKEN not in raw_resp_text
    assert MOCK_REFRESH_TOKEN not in raw_resp_text


# ===========================================================================
# 10. POST /amazon-ads/disconnect
# ===========================================================================

@pytest.mark.asyncio
async def test_disconnect_preserves_db_rows_and_nulls_credentials(client_a: AsyncClient, db_session: AsyncSession, user_a: User, monkeypatch):
    """Disconnect sets status NOT_CONNECTED and nulls encrypted token without deleting DB rows."""
    transport = create_mock_transport()
    patch_mock_client(monkeypatch, transport)

    # Connect & select
    resp_conn = await client_a.post("/amazon-ads/connect", json={"region": "EU"})
    raw_state = parse_qs(urlparse(resp_conn.json()["data"]["authorizationUrl"]).query)["state"][0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as public_client:
        await public_client.get(f"/amazon-ads/callback?code=c&state={raw_state}", follow_redirects=False)

    await client_a.post("/amazon-ads/accounts/select", json={"profileId": "12345678901234"})

    # Record row counts
    count_conn_before = (await db_session.execute(select(func.count(AmazonConnection.id)))).scalar()
    count_prof_before = (await db_session.execute(select(func.count(AmazonProfile.id)))).scalar()
    count_acc_before = (await db_session.execute(select(func.count(AdsAccount.id)))).scalar()

    # Disconnect
    resp_disc = await client_a.post("/amazon-ads/disconnect", json={})
    assert resp_disc.status_code == 200
    assert resp_disc.json()["data"]["connected"] is False

    # Check rows are still in DB (rule A1.2: never delete DB rows)
    count_conn_after = (await db_session.execute(select(func.count(AmazonConnection.id)))).scalar()
    count_prof_after = (await db_session.execute(select(func.count(AmazonProfile.id)))).scalar()
    count_acc_after = (await db_session.execute(select(func.count(AdsAccount.id)))).scalar()

    assert count_conn_after == count_conn_before
    assert count_prof_after == count_prof_before
    assert count_acc_after == count_acc_before

    # Verify credentials cleared
    res = await db_session.execute(select(AmazonConnection).where(AmazonConnection.user_id == user_a.id))
    conn = res.scalars().first()
    assert conn.status == "NOT_CONNECTED"
    assert conn.refresh_token_encrypted is None

    res = await db_session.execute(select(AdsAccount).where(AdsAccount.user_id == user_a.id))
    acc = res.scalars().first()
    assert acc.connection_status == "disconnected"
    assert acc.refresh_token_encrypted is None
    assert acc.is_active is False


# ===========================================================================
# 11. User Isolation
# ===========================================================================

@pytest.mark.asyncio
async def test_two_users_isolated(client_a: AsyncClient, client_b: AsyncClient, monkeypatch):
    """User A and User B cannot see each other's accounts or connection states."""
    transport = create_mock_transport()
    patch_mock_client(monkeypatch, transport)

    # Connect User A
    resp_a = await client_a.post("/amazon-ads/connect", json={"region": "EU"})
    state_a = parse_qs(urlparse(resp_a.json()["data"]["authorizationUrl"]).query)["state"][0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as public_client:
        await public_client.get(f"/amazon-ads/callback?code=cA&state={state_a}", follow_redirects=False)

    # User A status is CONNECTED
    assert (await client_a.get("/amazon-ads/status")).json()["data"]["connected"] is True

    # User B status is NOT_CONNECTED
    assert (await client_b.get("/amazon-ads/status")).json()["data"]["connected"] is False

    # User B calling /amazon-ads/accounts gets 409 NOT_CONNECTED
    resp_b_accs = await client_b.get("/amazon-ads/accounts")
    assert resp_b_accs.status_code == 409
    assert resp_b_accs.json()["code"] == "NOT_CONNECTED"


# ===========================================================================
# 12. Security: Assert no leaks in responses or logs
# ===========================================================================

@pytest.mark.asyncio
async def test_no_secrets_tokens_or_codes_in_responses_or_logs(client_a: AsyncClient, caplog, monkeypatch):
    """Ensure client secret, raw refresh tokens, access tokens, and codes are never logged or returned."""
    caplog.set_level(logging.DEBUG)
    transport = create_mock_transport()
    patch_mock_client(monkeypatch, transport)

    # 1. Connect
    resp1 = await client_a.post("/amazon-ads/connect", json={"region": "EU"})
    raw_state = parse_qs(urlparse(resp1.json()["data"]["authorizationUrl"]).query)["state"][0]

    # 2. Callback
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as public_client:
        resp2 = await public_client.get(f"/amazon-ads/callback?code={MOCK_AUTH_CODE}&state={raw_state}", follow_redirects=False)

    # 3. Accounts
    resp3 = await client_a.get("/amazon-ads/accounts")

    # 4. Select
    resp4 = await client_a.post("/amazon-ads/accounts/select", json={"profileId": "12345678901234"})

    # 5. Status
    resp5 = await client_a.get("/amazon-ads/status")

    # 6. Disconnect
    resp6 = await client_a.post("/amazon-ads/disconnect", json={})

    all_responses = [resp1.text, str(resp2.headers), resp3.text, resp4.text, resp5.text, resp6.text]
    joined_responses = " ".join(all_responses)
    app_log_messages = [record.getMessage() for record in caplog.records if record.name.startswith("app")]
    joined_logs = " ".join(app_log_messages)

    # Assertions
    assert MOCK_CLIENT_SECRET not in joined_responses
    assert MOCK_CLIENT_SECRET not in joined_logs

    assert MOCK_ACCESS_TOKEN not in joined_responses
    assert MOCK_ACCESS_TOKEN not in joined_logs

    assert MOCK_REFRESH_TOKEN not in joined_responses
    assert MOCK_REFRESH_TOKEN not in joined_logs

    assert MOCK_AUTH_CODE not in joined_responses
    assert MOCK_AUTH_CODE not in joined_logs

    # State logging must only be at most 6 characters of hash
    state_hash = hashlib.sha256(raw_state.encode("utf-8")).hexdigest()
    assert state_hash not in joined_logs  # full hash must not be logged
    assert raw_state not in joined_logs   # raw state must not be logged
    assert state_hash[:6] in joined_logs  # first 6 characters allowed


@pytest.mark.asyncio
async def test_reconnecting_same_profile_reactivates_same_row(client_a, db_session, monkeypatch):
    """
    Disconnecting soft-disconnects the account (tokens wiped, is_active=False).
    Reconnecting the same profile must reactivate the same AdsAccount row (same ID),
    preserving historical metrics and campaign relationships.
    """
    transport = create_mock_transport()
    patch_mock_client(monkeypatch, transport)

    # 1. Connect and callback
    resp1 = await client_a.post("/amazon-ads/connect", json={"region": "EU"})
    raw_state = parse_qs(urlparse(resp1.json()["data"]["authorizationUrl"]).query)["state"][0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as pub:
        await pub.get(f"/amazon-ads/callback?code={MOCK_AUTH_CODE}&state={raw_state}")

    # 2. Select profile
    sel_resp = await client_a.post("/amazon-ads/accounts/select", json={"profileId": "12345678901234"})
    assert sel_resp.status_code == 200

    # Verify active in DB and in /ads-accounts list
    res = await db_session.execute(select(AdsAccount).where(AdsAccount.profile_id == "12345678901234"))
    acc = res.scalars().first()
    assert acc is not None
    original_id = acc.id
    assert acc.is_active is True
    assert acc.connection_status == "connected"

    list_resp = await client_a.get("/ads-accounts")
    assert any(a["id"] == original_id for a in list_resp.json())

    # 3. Disconnect
    disc_resp = await client_a.post("/amazon-ads/disconnect", json={})
    assert disc_resp.status_code == 200

    await db_session.refresh(acc)
    assert acc.id == original_id
    assert acc.is_active is False
    assert acc.connection_status == "disconnected"
    assert acc.refresh_token_encrypted is None

    # Hidden from /ads-accounts list
    list_resp2 = await client_a.get("/ads-accounts")
    assert not any(a["id"] == original_id for a in list_resp2.json())

    # 4. Reconnect flow
    resp2 = await client_a.post("/amazon-ads/connect", json={"region": "EU"})
    raw_state2 = parse_qs(urlparse(resp2.json()["data"]["authorizationUrl"]).query)["state"][0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as pub:
        await pub.get(f"/amazon-ads/callback?code={MOCK_AUTH_CODE}&state={raw_state2}")

    # Re-select the same profile
    sel_resp2 = await client_a.post("/amazon-ads/accounts/select", json={"profileId": "12345678901234"})
    assert sel_resp2.status_code == 200

    # Assert it reactivated the SAME row ID
    await db_session.refresh(acc)
    assert acc.id == original_id
    assert acc.is_active is True
    assert acc.connection_status == "connected"
    assert acc.refresh_token_encrypted is not None

    # Now visible in /ads-accounts list again
    list_resp3 = await client_a.get("/ads-accounts")
    assert any(a["id"] == original_id for a in list_resp3.json())

