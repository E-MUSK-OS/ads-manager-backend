import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from app.main import app
from app.config import Settings
from app.core import marketplace_rules
from app.models.user import User
from app.models.ads_account import AdsAccount
from app.models.contact_message import ContactMessage
from app.providers.llm.claude_provider import ClaudeProvider


# ---------------------------------------------------------------------------
# FIX 1d: AI Chat Request Body Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ai_chat_json_body_and_validations(client_a: AsyncClient, monkeypatch):
    # Mock ClaudeProvider.chat so Anthropic API is never called
    mock_chat = AsyncMock(return_value="AI recommendation response")
    monkeypatch.setattr(ClaudeProvider, "chat", mock_chat)

    # 1. Valid JSON body returns 200
    resp = await client_a.post("/ai/chat", json={"message": "hi"})
    assert resp.status_code == 200
    assert resp.json() == {"response": "AI recommendation response"}
    assert mock_chat.await_count == 1

    # 2. Old query-param style with no body returns 422
    old_resp = await client_a.post("/ai/chat?message=hi")
    assert old_resp.status_code == 422

    # 3. Empty message in JSON body returns 422
    empty_resp = await client_a.post("/ai/chat", json={"message": ""})
    assert empty_resp.status_code == 422

    # 4. Whitespace-only message in JSON body returns 422
    blank_resp = await client_a.post("/ai/chat", json={"message": "   "})
    assert blank_resp.status_code == 422


# ---------------------------------------------------------------------------
# FIX 3g: Account Deletion (DELETE /auth/me) & Cascades
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_account_me_validations_and_cascades(
    client_a: AsyncClient,
    client_b: AsyncClient,
    user_a: User,
    user_b: User,
    account_a: AdsAccount,
    account_b: AdsAccount,
    db_session,
):
    # Create contact messages for user_a (different case) and user_b
    msg_a1 = ContactMessage(
        name="User A",
        email="A@TEST.COM",  # uppercase, to test case-insensitive match with a@test.com
        message="Message from user A.",
    )
    msg_a2 = ContactMessage(
        name="User A Lower",
        email="a@test.com",
        message="Second message from user A.",
    )
    msg_b = ContactMessage(
        name="User B",
        email="b@test.com",
        message="Message from user B.",
    )
    db_session.add_all([msg_a1, msg_a2, msg_b])
    await db_session.commit()

    # 1. DELETE /auth/me with wrong confirmation returns 400 and deletes nothing
    bad_resp = await client_a.request("DELETE", "/auth/me", json={"confirm": "NO"})
    assert bad_resp.status_code == 400
    assert "DELETE" in bad_resp.json()["detail"]

    # Verify user_a, account_a, and contact messages still exist
    check_user = await db_session.get(User, user_a.id)
    assert check_user is not None
    check_acc = await db_session.get(AdsAccount, account_a.id)
    assert check_acc is not None
    msgs_a = (
        await db_session.execute(
            select(ContactMessage).where(ContactMessage.email.in_(["A@TEST.COM", "a@test.com"]))
        )
    ).scalars().all()
    assert len(msgs_a) == 2

    # 2. DELETE /auth/me with valid confirmation deletes user_a, their accounts, and their contact messages
    del_resp = await client_a.request("DELETE", "/auth/me", json={"confirm": "DELETE"})
    assert del_resp.status_code == 200
    assert del_resp.json() == {"status": "ok"}

    # Confirm user_a and account_a are gone
    user_a_check = (await db_session.execute(select(User).where(User.id == user_a.id))).scalar_one_or_none()
    assert user_a_check is None
    acc_a_check = (await db_session.execute(select(AdsAccount).where(AdsAccount.id == account_a.id))).scalar_one_or_none()
    assert acc_a_check is None

    # Confirm user_a's contact messages are deleted
    msgs_a_after = (
        await db_session.execute(
            select(ContactMessage).where(ContactMessage.email.in_(["A@TEST.COM", "a@test.com"]))
        )
    ).scalars().all()
    assert len(msgs_a_after) == 0

    # Confirm user_b's data and contact messages remain intact
    assert await db_session.get(User, user_b.id) is not None
    assert await db_session.get(AdsAccount, account_b.id) is not None
    msg_b_after = (
        await db_session.execute(
            select(ContactMessage).where(ContactMessage.email == "b@test.com")
        )
    ).scalar_one_or_none()
    assert msg_b_after is not None

    # 3. Unauthenticated call returns 401
    unauth_resp = await client_a.request(
        "DELETE",
        "/auth/me",
        json={"confirm": "DELETE"},
        headers={"x-test-user": "anonymous"},
    )
    assert unauth_resp.status_code == 401


# ---------------------------------------------------------------------------
# FIX 4: IST Fallback in today_for
# ---------------------------------------------------------------------------

def test_today_for_zoneinfo_fallback(monkeypatch):
    def fake_zoneinfo(tz_name):
        raise KeyError(f"Simulated missing timezone: {tz_name}")

    monkeypatch.setattr(marketplace_rules, "ZoneInfo", fake_zoneinfo)

    expected_ist_today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()
    result = marketplace_rules.today_for("IN")
    assert result == expected_ist_today


# ---------------------------------------------------------------------------
# FIX 5c: Environment and Security Configuration Tests
# ---------------------------------------------------------------------------

def test_config_production_and_strict_validation():
    # 1. Production + short JWT_SECRET_KEY fails (names JWT_SECRET_KEY)
    with pytest.raises(ValueError) as exc1:
        Settings(
            ENVIRONMENT="production",
            ADS_PROVIDER="mock",
            DATABASE_URL="sqlite+aiosqlite://",
            JWT_SECRET_KEY="short_key",
            FRONTEND_URL="https://app.example.com",
        )
    assert "JWT_SECRET_KEY" in str(exc1.value)

    # 2. Production + non-https FRONTEND_URL fails (names FRONTEND_URL)
    with pytest.raises(ValueError) as exc2:
        Settings(
            ENVIRONMENT="production",
            ADS_PROVIDER="mock",
            DATABASE_URL="sqlite+aiosqlite://",
            JWT_SECRET_KEY="a" * 32,
            FRONTEND_URL="http://app.example.com",
        )
    assert "FRONTEND_URL" in str(exc2.value)

    # 3. Production + valid values passes
    prod_settings = Settings(
        ENVIRONMENT="production",
        ADS_PROVIDER="mock",
        DATABASE_URL="sqlite+aiosqlite://",
        JWT_SECRET_KEY="a" * 32,
        FRONTEND_URL="https://app.example.com, https://admin.example.com",
    )
    assert prod_settings.ENVIRONMENT == "production"

    # 4. Development + mock + short secret still passes (for local work and dev tests)
    dev_settings = Settings(
        ENVIRONMENT="development",
        ADS_PROVIDER="mock",
        DATABASE_URL="sqlite+aiosqlite://",
        JWT_SECRET_KEY="dev_short_secret",
        FRONTEND_URL="http://localhost:3000",
    )
    assert dev_settings.ENVIRONMENT == "development"
    assert dev_settings.JWT_SECRET_KEY == "dev_short_secret"
